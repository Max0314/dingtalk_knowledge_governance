"""Production collection never uses title/inventory scans for confirmation."""
import asyncio
from datetime import timedelta
import httpx
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from app import audit_bridge, integrations
from app.config import Settings
from app.db import Base, Document, FileAuditEvent, ReviewJob, Workspace, utcnow
from scripts.migrate_collector_lookup_index import migrate


@pytest.fixture
def collector(tmp_path):
    bind=create_engine('sqlite:///'+(tmp_path/'collector.db').as_posix())
    Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind,autoflush=False,expire_on_commit=False)
    settings=Settings(_env_file=None,demo_mode=False,metadata_only=True,content_extract_enabled=False,
                      bridge_enabled=True,dingtalk_sync_operator_id='synthetic-operator')
    with sessions.begin() as db:
        db.add(Workspace(workspace_id='synthetic-space',name='C-Synthetic'))
    yield sessions,bind,settings
    bind.dispose()


def add_event(sessions, key='event', **overrides):
    values=dict(event_key=key,biz_id='123456',gmt_create=int(utcnow().timestamp()*1000),
                resource='same-title.adoc',extension='adoc',action_view='创建文档',module_view='团队空间')
    values.update(overrides)
    with sessions.begin() as db:db.add(FileAuditEvent(**values))


@pytest.mark.parametrize('storage', ['', 'synthetic-storage'])
def test_local_only_backlog_cannot_starve_upload_locator_or_confirmed_refresh(collector, monkeypatch, storage):
    sessions, _, s = collector
    s = s.model_copy(update={'wiki_storage_space_id': storage})
    now = int(utcnow().timestamp()*1000)
    with sessions.begin() as db:
        for i in range(45):
            db.add(FileAuditEvent(event_key=f'local-{i}', biz_id=f'local-{i}', gmt_create=now+i,
                action_view='知识库删除文件' if i%2 else '知识库恢复文件', module_view='团队空间',
                match_status='confirmed' if i<25 else '', matched_node_id=f'missing-{i}' if i<25 else ''))
        # All local-only rows have never been attempted and would win the old LIMIT.
        db.add(FileAuditEvent(event_key='upload', biz_id='upload', gmt_create=now-1000,
            action_view='创建文档', module_view='团队空间', resource='synthetic-upload', extension='ADOC',
            locate_attempts=1, last_attempt_at=utcnow()-timedelta(minutes=20)))
        db.add(FileAuditEvent(event_key='confirmed-upload', biz_id='confirmed-upload', gmt_create=now-1000,
            action_view='知识库修改文件', module_view='团队空间', resource='synthetic-known', extension='adoc',
            match_status='confirmed', matched_node_id='synthetic-known', locate_attempts=1,
            last_attempt_at=utcnow()-timedelta(minutes=20)))
    searches=[]; refreshed=[]
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self, name, *a, **k):searches.append(name);return []
        async def batch_query_wiki_nodes(self, ids, *a):refreshed.extend(ids);return []
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        upload=db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='upload'))
        assert upload.locate_attempts==2 and searches==['synthetic-upload', 'synthetic-upload.ADOC']
        assert refreshed==['synthetic-known']
        local=db.scalars(select(FileAuditEvent).where(FileAuditEvent.event_key.like('local-%'))).all()
        assert all(e.locate_attempts==0 and not e.processed for e in local)


def test_idle_and_repeated_unresolved_events_do_not_read_document_inventory(collector,monkeypatch):
    sessions,bind,s=collector
    calls=[];sql=[]
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,*a,**k):calls.append(a[0]);return []
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    def forbidden(*a,**k):raise AssertionError('production must not enumerate or title-match documents')
    monkeypatch.setattr(audit_bridge,'_governed_workspaces',forbidden)
    monkeypatch.setattr(audit_bridge,'_provisional_match',forbidden)
    def record(_,__,statement,*a):sql.append(statement.lower())
    event.listen(bind,'before_cursor_execute',record)
    try:
        with sessions() as db:audit_bridge.process_audit_events(db,s,drain_walks=False)
        with sessions.begin() as db:
            db.add(Document(node_id='old-same-title',workspace_id='synthetic-space',name='same-title.adoc'))
        add_event(sessions)
        for _ in range(3):
            with sessions() as db:audit_bridge.process_audit_events(db,s,drain_walks=False)
        assert calls==['same-title.adoc']
        assert not any(' from documents' in q or ' from historical_file_nodes' in q or ' join documents' in q for q in sql if q.startswith('select'))
    finally:event.remove(bind,'before_cursor_execute',record)
    with sessions() as db:
        e=db.scalar(select(FileAuditEvent))
        assert e.locate_attempts==1 and not e.processed and not e.matched_node_id


@pytest.mark.parametrize('failure',[httpx.ReadTimeout('synthetic timeout'),httpx.ConnectError('synthetic disconnect'),
    integrations.IntegrationError('dingtalk_dentry_search_failed','synthetic rate limit',429),ValueError('synthetic malformed JSON')])
def test_failed_locator_retries_are_bounded_without_local_title_fallback(collector,monkeypatch,failure):
    sessions,_,s=collector
    calls=[]
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,*a,**k):calls.append(1);raise failure
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    add_event(sessions)
    for attempt in range(1,4):
        with sessions() as db:
            result=audit_bridge.process_audit_events(db,s,drain_walks=False)
            e=db.scalar(select(FileAuditEvent))
            assert e.locate_attempts==attempt
            assert e.processed==(attempt==3)
            if attempt<3:
                before=len(calls)
                audit_bridge.process_audit_events(db,s,drain_walks=False)
                assert len(calls)==before
                e.next_locate_at=utcnow()-timedelta(seconds=1);db.commit()
    assert len(calls)==3
    with sessions() as db:
        assert db.scalar(select(FileAuditEvent)).resolution=='dead_letter_locator_limit'
        assert not db.scalars(select(Document)).all()


def test_exact_remote_proof_handles_rename_and_rejects_old_same_title(collector,monkeypatch):
    sessions,_,s=collector
    stamp=int(utcnow().timestamp()*1000)
    add_event(sessions,'created',gmt_create=stamp,resource='old-title.adoc')
    add_event(sessions,'renamed',gmt_create=stamp+1,resource='new-title.adoc',action_view='知识库重命名')
    calls=[]
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,name,*a,**k):
            calls.append(name)
            return [{'dentry_uuid':n,'name':name} for n in ['old-node','new-node']]
        async def batch_query_wiki_nodes(self,ids,*a):
            return [{'node_id':'old-node','workspace_id':'synthetic-space','name':'new-title.adoc','created_at':'2025-01-01T00:00:00Z','has_children':False,'extension':'adoc'},
                    {'node_id':'new-node','workspace_id':'synthetic-space','name':'new-title.adoc','created_at':utcnow().isoformat(),'has_children':False,'extension':'adoc'}]
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        rows=db.scalars(select(FileAuditEvent)).all()
        assert all(e.processed and e.match_status=='confirmed' and e.matched_node_id=='new-node' for e in rows)
        assert db.get(Document,'new-node').name=='new-title.adoc'
        assert not db.scalars(select(ReviewJob)).all()
    assert set(calls)=={'new-title.adoc'}


def test_old_provisional_delete_and_duplicate_download_keys_cannot_delete_arbitrary_doc(collector):
    sessions,_,s=collector
    with sessions.begin() as db:
        for n,key in [('old-candidate','other'),('a','123456'),('b','123456')]:
            db.add(Document(node_id=n,workspace_id='synthetic-space',name='same-title.adoc',storage_dentry_id=key))
    add_event(sessions,match_status='provisional',matched_node_id='old-candidate',action_view='知识库删除')
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        assert not any(d.is_deleted for d in db.scalars(select(Document)))
        e=db.scalar(select(FileAuditEvent));assert not e.processed


def test_governed_registry_preserves_empty_and_inactive_semantics_with_indexed_exists(collector):
    sessions,_,_=collector
    with sessions.begin() as db:
        db.add_all([Workspace(workspace_id='empty',name='C-Empty'),Workspace(workspace_id='inactive',name='C-Inactive',is_active=False),
                    Document(node_id='a',workspace_id='synthetic-space',name='synthetic'),Document(node_id='b',workspace_id='inactive',name='synthetic')])
    with sessions() as db:assert audit_bridge._governed_workspaces(db)==['synthetic-space']


def test_download_lookup_migration_is_idempotent_and_lookup_is_indexed(collector):
    _,bind,_=collector
    with bind.begin() as c:c.execute(text('DROP INDEX ix_documents_storage_dentry_id'))
    assert migrate(bind)['created']
    assert not migrate(bind)['created']
    with bind.connect() as c:
        plan=' '.join(str(r) for r in c.execute(text("EXPLAIN QUERY PLAN SELECT node_id FROM documents WHERE storage_dentry_id='123456' LIMIT 2")))
        assert 'ix_documents_storage_dentry_id' in plan and 'SEARCH' in plan


@pytest.mark.parametrize('payload',[[],{'items':'bad','nodes':'bad'}, {'items':[None],'nodes':[None]},
    {'items':[{'dentryUuid':[]}],'nodes':[{'nodeId':[]}]},
    {'items':[{'name':[]}],'nodes':[{'statisticalInfo':[1]}]}])
def test_malformed_dingtalk_success_is_a_retryable_contract_error(monkeypatch,payload):
    s=Settings(_env_file=None)
    monkeypatch.setattr(integrations,'reserve_call',lambda *a:None)
    async def token(*a):return 'synthetic-token'
    monkeypatch.setattr(integrations.DingtalkClient,'_token_value',token)
    monkeypatch.setattr(integrations,'outbound_client',lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))))
    for method,args in [('search_dentries',('synthetic','synthetic-operator')),('batch_query_wiki_nodes',(['synthetic-node'],'synthetic-operator'))]:
        with pytest.raises(integrations.IntegrationError) as e:asyncio.run(getattr(integrations.DingtalkClient(s),method)(*args))
        assert e.value.status_code==502


def test_lifecycle_rejects_conflict_after_repeated_same_node_proofs(collector):
    sessions,_,_=collector
    for i,node in enumerate(['a','a','b']):
        add_event(sessions,'proof-'+str(i),matched_node_id=node,match_status='confirmed',processed=True)
    add_event(sessions,'unresolved',action_view='知识库删除')
    with sessions() as db:
        unresolved=db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='unresolved'))
        assert audit_bridge._confirmed_lifecycle_node(db,unresolved)==''


def test_batch_deadline_keeps_successful_nodes_and_unstarted_events_do_not_spend_attempts(collector,monkeypatch):
    sessions,_,s=collector
    monkeypatch.setattr(audit_bridge,'LOCATE_TIME_BUDGET_SECONDS',.03)
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,name,*a,**k):
            if name.startswith('slow'):await asyncio.sleep(1)
            return [{'dentry_uuid':'good-node','name':name}]
        async def batch_query_wiki_nodes(self,*a):
            return [{'node_id':'good-node','workspace_id':'synthetic-space','name':'good.adoc',
                     'created_at':utcnow().isoformat(),'extension':'adoc','has_children':False}]
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    add_event(sessions,'slow',resource='slow.adoc',biz_id='123')
    add_event(sessions,'good',resource='good.adoc',biz_id='456')
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        assert db.get(Document,'good-node') is not None
        assert db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='good')).processed
        assert not db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='slow')).processed
    # With one slot, a slow event occupies the whole cycle. The next event must
    # remain immediately eligible, with its retry allowance intact.
    monkeypatch.setattr(audit_bridge,'WIKI_LOCATE_CONCURRENCY',1)
    stamp=int(utcnow().timestamp()*1000)
    add_event(sessions,'slow-first',resource='slow-first.adoc',biz_id='789',gmt_create=stamp+1)
    add_event(sessions,'waiting',resource='waiting.adoc',biz_id='987',gmt_create=stamp)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        waiting=db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='waiting'))
        assert waiting.locate_attempts==0 and waiting.next_locate_at is None and not waiting.processed


@pytest.mark.parametrize('payload',[{'items':[],'nodes':[]},{'items':None,'nodes':None}])
def test_empty_dingtalk_results_keep_legacy_null_compatibility(monkeypatch,payload):
    s=Settings(_env_file=None)
    monkeypatch.setattr(integrations,'reserve_call',lambda *a:None)
    async def token(*a):return 'synthetic-token'
    monkeypatch.setattr(integrations.DingtalkClient,'_token_value',token)
    monkeypatch.setattr(integrations,'outbound_client',lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))))
    c=integrations.DingtalkClient(s)
    assert asyncio.run(c.search_dentries('synthetic','synthetic-operator'))==[]
    assert asyncio.run(c.batch_query_wiki_nodes(['synthetic-node'],'synthetic-operator'))==[]


@pytest.mark.parametrize('path,expected',[
    ({'longPath':'synthetic/root','path':'synthetic:root','url':'https://synthetic.invalid/private'},'synthetic/root'),
    ({'path':'synthetic:root'},'synthetic:root'), ({},''), (None,''), ('synthetic/root','synthetic/root'),
    ({'longPath':'s'*1100},'s'*1024),
])
def test_storage_search_accepts_official_path_object_without_storing_url(monkeypatch,path,expected):
    monkeypatch.setattr(integrations,'reserve_call',lambda *a:None)
    async def token(*a):return 'synthetic-token'
    monkeypatch.setattr(integrations.DingtalkClient,'_token_value',token)
    payload={'items':[{'dentryUuid':'synthetic-node','name':'synthetic','path':path}]}
    monkeypatch.setattr(integrations,'outbound_client',lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))))
    result=asyncio.run(integrations.DingtalkClient(Settings(_env_file=None)).search_dentries('synthetic','synthetic-operator'))
    assert result==[{'dentry_uuid':'synthetic-node','name':'synthetic','path':expected}]


def test_official_search_payload_flows_to_metadata_mirror_without_review(collector,monkeypatch):
    sessions,_,s=collector
    s=s.model_copy(update={'dingtalk_sync_operator_id':'synthetic-operator'})
    stamp=int(utcnow().timestamp()*1000)
    add_event(sessions,gmt_create=stamp)
    monkeypatch.setattr(integrations,'reserve_call',lambda *a:None)
    async def token(*a):return 'synthetic-token'
    monkeypatch.setattr(integrations.DingtalkClient,'_token_value',token)
    paths=[]
    def provider(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/search'):
            return httpx.Response(200,json={'items':[{'dentryUuid':'synthetic-node','name':'same-title.adoc',
                'path':{'longPath':'synthetic/root','url':'https://synthetic.invalid/private'}}]})
        return httpx.Response(200,json={'nodes':[{'nodeId':'synthetic-node','workspaceId':'synthetic-space',
            'name':'same-title.adoc','extension':'adoc','size':30483771330,
            'createTime':utcnow().isoformat(),'hasChildren':False}]})
    monkeypatch.setattr(integrations,'outbound_client',lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    with sessions() as db:
        result=audit_bridge.process_audit_events(db,s,drain_walks=False)
        row=db.scalar(select(FileAuditEvent))
        doc=db.get(Document,'synthetic-node')
        assert result.get('direct_upserts')==1 and row.processed and row.metadata_applied_at is not None, (str(result),paths)
        assert doc.path=='synthetic/root' and doc.size==30483771330
        assert db.scalars(select(ReviewJob)).all()==[]


@pytest.mark.parametrize('confirmed',[False,True])
@pytest.mark.parametrize('code',['dingtalk_budget_exhausted','dingtalk_budget_unavailable','dingtalk_rate_unavailable'])
def test_quota_race_restores_prior_attempts_and_survives_new_session(collector,monkeypatch,confirmed,code):
    sessions,_,s=collector
    original_time=utcnow()-timedelta(minutes=20)
    retry_at=utcnow()+timedelta(minutes=5)
    calls=[]
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,*a,**k):
            calls.append('search')
            exc=integrations.IntegrationError(code,'synthetic quota',429)
            exc.retry_at=retry_at
            raise exc
        async def batch_query_wiki_nodes(self,*a,**k):
            calls.append('batch')
            exc=integrations.IntegrationError(code,'synthetic quota',429)
            exc.retry_at=retry_at
            raise exc
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    add_event(sessions,locate_attempts=2,last_attempt_at=original_time,
              matched_node_id='known' if confirmed else '',match_status='confirmed' if confirmed else '')
    for _ in range(2):
        with sessions() as db:
            result=audit_bridge.process_audit_events(db,s,drain_walks=False)
            e=db.scalar(select(FileAuditEvent))
            assert e.locate_attempts==2 and not e.processed and not e.resolution
            assert e.last_attempt_at.replace(tzinfo=None)==original_time.replace(tzinfo=None)
            assert e.next_locate_at.replace(tzinfo=None)==retry_at.replace(tzinfo=None)
    assert len(calls)==1
    # Eligibility resumes without changing the allowance; a genuine failure
    # can still spend the remaining third attempt and reach its original end.
    async def network_failure(*a,**k):raise httpx.ConnectError('synthetic disconnect')
    monkeypatch.setattr(Client,'search_dentries',network_failure)
    monkeypatch.setattr(Client,'batch_query_wiki_nodes',network_failure)
    with sessions.begin() as db:db.scalar(select(FileAuditEvent)).next_locate_at=utcnow()-timedelta(seconds=1)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        e=db.scalar(select(FileAuditEvent))
        assert e.locate_attempts==3 and e.processed
        assert e.resolution==('dead_letter_metadata_unavailable' if confirmed else 'dead_letter_locator_limit')


@pytest.mark.parametrize('confirmed',[False,True])
def test_known_exhaustion_never_calls_provider_or_writes_events(collector,monkeypatch,confirmed):
    from app.db import ApiUsage
    from app.api_usage import CST
    from datetime import datetime
    sessions,bind,s=collector
    interface='wiki.batchQuery' if confirmed else 'storage.search'
    s=s.model_copy(update={'dingtalk_daily_limit':3,'dingtalk_search_daily_limit':3})
    with sessions.begin() as db:
        db.add(ApiUsage(period=datetime.now(CST).strftime('day:%Y-%m-%d'),interface=interface,calls=3))
    class Client:
        def __init__(self,*a):raise AssertionError('blocked quota must skip provider')
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    add_event(sessions,locate_attempts=2,matched_node_id='known' if confirmed else '',match_status='confirmed' if confirmed else '')
    writes=[]
    def observe(_,__,statement,*a):
        if statement.lower().startswith('update file_audit_events'):writes.append(statement)
    event.listen(bind,'before_cursor_execute',observe)
    try:
        for _ in range(2):
            with sessions() as db:
                result=audit_bridge.process_audit_events(db,s,drain_walks=False)
                e=db.scalar(select(FileAuditEvent))
                assert result.get('budget_wait_until')
                assert e.locate_attempts==2 and not e.processed and e.next_locate_at is None
        assert writes==[]
    finally:event.remove(bind,'before_cursor_execute',observe)


def test_quota_after_successful_search_keeps_other_completed_event(collector,monkeypatch):
    sessions,_,s=collector
    retry_at=utcnow()+timedelta(minutes=5)
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,name,*a,**k):return [{'dentry_uuid':name,'name':name}]
        async def batch_query_wiki_nodes(self,ids,*a):
            if ids==['waiting.adoc']:
                exc=integrations.IntegrationError('dingtalk_budget_exhausted','synthetic quota',429)
                exc.retry_at=retry_at
                raise exc
            return [{'node_id':'good.adoc','workspace_id':'synthetic-space','name':'good.adoc',
                     'created_at':utcnow().isoformat(),'extension':'adoc','has_children':False}]
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    add_event(sessions,'good',resource='good.adoc',biz_id='11')
    add_event(sessions,'waiting',resource='waiting.adoc',biz_id='22',locate_attempts=2)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        rows={r.event_key:r for r in db.scalars(select(FileAuditEvent))}
        assert rows['good'].processed and db.get(Document,'good.adoc') is not None
        assert not rows['waiting'].processed and rows['waiting'].locate_attempts==2
        assert rows['waiting'].next_locate_at.replace(tzinfo=None)==retry_at.replace(tzinfo=None)


def test_quota_waiters_cannot_starve_confirmed_local_delete(collector,monkeypatch):
    from app.db import ApiUsage
    from app.api_usage import CST
    from datetime import datetime
    sessions,_,s=collector
    s=s.model_copy(update={'dingtalk_daily_limit':1})
    monkeypatch.setattr(audit_bridge,'BATCH',2)
    monkeypatch.setattr(audit_bridge,'CONFIRM_FINISH_BUDGET',2)
    with sessions.begin() as db:
        db.add(ApiUsage(period=datetime.now(CST).strftime('day:%Y-%m-%d'),interface='total',calls=1))
        db.add(Document(node_id='delete-me',workspace_id='synthetic-space',name='synthetic'))
    stamp=int(utcnow().timestamp()*1000)
    for i in range(5):
        add_event(sessions,'wait-'+str(i),biz_id='',gmt_create=stamp,match_status='confirmed',matched_node_id='waiting')
    add_event(sessions,'delete',biz_id='',gmt_create=stamp+1,action_view='知识库删除',
              match_status='confirmed',matched_node_id='delete-me',last_attempt_at=utcnow())
    class Client:
        def __init__(self,*a):raise AssertionError('local delete must not call remote')
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        e=db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='delete'))
        assert e.processed and e.resolution=='deleted' and db.get(Document,'delete-me').is_deleted
        waiters=db.scalars(select(FileAuditEvent).where(FileAuditEvent.event_key.like('wait-%'))).all()
        assert all(not r.processed and r.last_attempt_at is None and r.locate_attempts==0 for r in waiters)
