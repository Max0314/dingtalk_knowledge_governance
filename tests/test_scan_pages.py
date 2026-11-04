import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker

from app import api_usage, scan_scheduler, service
from app.api_usage import scan_budget
from app.config import Settings
from app.db import ApiUsage, Base, Document, ScanTask, SyncRun, Workspace, utcnow
from app.integrations import IntegrationError
from app.scan_models import ScanDirectory, ScanProgress, ScanSeen


def node(key, **kwargs):
    return {'node_id': key, 'name': 'Synthetic', 'type': 'file', 'has_children': False,
            'creator_id': 'synthetic-person', 'created_at': '2026-11-01T09:00:00+08:00', **kwargs}


@pytest.fixture
def local(tmp_path, monkeypatch):
    bind = create_engine('sqlite:///' + str(tmp_path/'scan.db'))
    Base.metadata.create_all(bind)
    sessions = sessionmaker(bind=bind, autoflush=False)
    settings = Settings(_env_file=None, metadata_only=True, content_extract_enabled=False,
                        full_scan_enabled=True, scan_slice_pages=1, watch_workspaces='*', scan_days='1')
    monkeypatch.setattr(api_usage, 'SessionLocal', sessions)
    with sessions.begin() as db:
        db.add(Workspace(workspace_id='synthetic-space', name='C-Synthetic', watch_seeded=True))
    yield sessions, settings
    bind.dispose()


def run(sessions, settings, key=None):
    with sessions() as db, scan_budget('2026-11'):
        return asyncio.run(service.watch_workspace(db, settings, 'synthetic-space',
            {'workspace_id': 'synthetic-space', 'name': 'C-Synthetic', 'root_node_id': 'root'}, resume_run_id=key))


def finish(sessions, settings, key):
    for _ in range(20):
        result = run(sessions, settings, key)
        if result.status != 'running':
            return result
    pytest.fail('scan did not complete')


def test_page_checkpoint_survives_failure_without_repeating_good_page(local, monkeypatch):
    sessions, settings = local
    calls, failing = [], [True]
    class Client:
        def __init__(self, *_): pass
        async def list_nodes(self, ws, operator, parent, token, **kwargs):
            # Verify another writer can commit during every remote request.
            with sessions.begin() as db:
                assert db.get(Workspace, ws).name == 'C-Synthetic'
            calls.append(token)
            if token == 'second' and failing[0]:
                raise IntegrationError('synthetic_network', 'synthetic', 503)
            return {'items': [node('first' if not token else 'second')], 'next_token': 'second' if not token else ''}
    monkeypatch.setattr(service, 'DingtalkClient', Client)
    first = run(sessions, settings)
    assert first.status == 'running'
    failed = run(sessions, settings, first.run_id)
    assert failed.status == 'failed'
    with sessions() as db:
        assert db.get(Document, 'first')
        assert db.get(ScanDirectory, (first.run_id, 'root')).next_token == 'second'
    failing[0] = False
    assert finish(sessions, settings, first.run_id).status == 'succeeded'
    assert calls == ['', 'second', 'second']
    with sessions() as db:
        assert db.get(SyncRun, first.run_id).documents_new == 2
        assert len(db.scalars(select(ScanSeen)).all()) == 2


def test_newer_incremental_metadata_wins_and_partial_walk_cannot_delete(local, monkeypatch):
    sessions, settings = local
    with sessions.begin() as db:
        db.add(Document(node_id='unseen', workspace_id='synthetic-space', name='Synthetic', watch_misses=1))
    class Client:
        def __init__(self, *_): pass
        async def list_nodes(self, ws, operator, parent, token, **kwargs):
            if not token:
                return {'items': [node('existing')], 'next_token': 'second'}
            with sessions.begin() as db:
                d = db.get(Document, 'existing')
                d.name, d.metadata_applied_at = 'Newer metadata', utcnow()+timedelta(seconds=1)
                other = db.get(Document, 'unseen')
                other.metadata_applied_at = utcnow()+timedelta(seconds=1)
            return {'items': [node('existing', name='Stale metadata')], 'next_token': ''}
    monkeypatch.setattr(service, 'DingtalkClient', Client)
    first = run(sessions, settings)
    with sessions() as db:
        assert not db.get(Document, 'unseen').is_deleted
        assert db.get(Document, 'unseen').watch_misses == 1
    assert finish(sessions, settings, first.run_id).status == 'succeeded'
    with sessions() as db:
        assert db.get(Document, 'existing').name == 'Newer metadata'
        assert not db.get(Document, 'unseen').is_deleted


def test_cycle_in_tokens_fails_without_absence_reconciliation(local, monkeypatch):
    sessions, settings = local
    with sessions.begin() as db:
        db.add(Document(node_id='unseen', workspace_id='synthetic-space', name='Synthetic', watch_misses=1))
    class Client:
        def __init__(self, *_): pass
        async def list_nodes(self, *args, **kwargs):
            return {'items': [], 'next_token': 'repeat'}
    monkeypatch.setattr(service, 'DingtalkClient', Client)
    first = run(sessions, settings)
    result = run(sessions, settings, first.run_id)
    assert result.status == 'failed' and result.error_code.startswith('scan_pagination_cycle')
    with sessions() as db:
        assert db.get(Document, 'unseen').watch_misses == 1


def test_slice_and_boot_recovery_do_not_consume_retry_attempts(local, monkeypatch):
    sessions, settings = local
    class Client:
        def __init__(self, *_): pass
        async def list_nodes(self, *args, **kwargs): return {'items': [node('synthetic')], 'next_token': ''}
    async def targets(*args, **kwargs):
        return {'resolved': [{'workspace_id': 'synthetic-space', 'name': 'C-Synthetic', 'space': {'root_node_id': 'root'}}]}
    monkeypatch.setattr(service, 'DingtalkClient', Client)
    monkeypatch.setattr(scan_scheduler, 'resolve_watch_targets', targets)
    for _ in range(8):
        with sessions() as db:
            service.sweep_stale_runs(db, review_jobs=False, bridge_walks=False)
            result = scan_scheduler.run_scan_slice(db, settings)
        if result['status'] == 'completed': break
    assert result['status'] == 'completed'
    with sessions() as db:
        assert db.scalars(select(ScanTask)).one().attempts == 1


def test_daily_reserve_protects_incremental_calls_atomically(local):
    sessions, _ = local
    settings = Settings(_env_file=None, dingtalk_daily_limit=10, scan_daily_reserve=2)
    with scan_budget('2026-11'):
        for _ in range(8): api_usage.reserve_call(settings, 'wiki.nodes')
        with pytest.raises(IntegrationError, match='本日补采'):
            api_usage.reserve_call(settings, 'wiki.nodes')
    api_usage.reserve_call(settings, 'exclusive.fileAuditLogs')
    api_usage.reserve_call(settings, 'exclusive.fileAuditLogs')
    with sessions() as db:
        day = api_usage.datetime.now(api_usage.CST).strftime('day:%Y-%m-%d')
        assert db.get(ApiUsage, (day, 'total')).calls == 10


@pytest.mark.parametrize('lose_ack', [False, True])
def test_completed_checkpoint_survives_client_close_and_commit_ack_loss(local, monkeypatch, lose_ack):
    from contextlib import asynccontextmanager
    from sqlalchemy.orm import Session
    sessions, settings = local
    settings = settings.model_copy(update={'scan_slice_pages': 20})
    calls, raised = [], [False]
    class Client:
        def __init__(self, *_): pass
        @asynccontextmanager
        async def reuse_http(self):
            yield
            raise RuntimeError('synthetic close failure')
        async def list_nodes(self, *args, **kwargs):
            calls.append(1)
            return {'items':[node('only')], 'next_token':''}
    def after_commit(db):
        if lose_ack and not raised[0] and any(isinstance(r, ScanProgress) and r.phase=='complete' for r in db.identity_map.values()):
            raised[0] = True
            raise RuntimeError('synthetic lost commit acknowledgement')
    event.listen(Session, 'after_commit', after_commit)
    monkeypatch.setattr(service, 'DingtalkClient', Client)
    try:
        result = run(sessions, settings)
    finally:
        event.remove(Session, 'after_commit', after_commit)
    assert result.status == 'succeeded' and raised[0] == lose_ack
    assert run(sessions, settings, result.run_id).status == 'succeeded'
    assert calls == [1]


def test_page_commit_failure_does_not_advance_cursor_or_double_count(local, monkeypatch):
    from sqlalchemy.orm import Session
    sessions,settings=local
    settings=settings.model_copy(update={'scan_slice_pages':20})
    fail=[True]
    class Client:
        def __init__(self,*_):pass
        async def list_nodes(self,*args,**kwargs):return {'items':[node('only')],'next_token':''}
    def before_commit(db):
        if fail[0] and any(isinstance(r,Document) for r in db.new):
            fail[0]=False
            raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(service,'DingtalkClient',Client)
    event.listen(Session,'before_commit',before_commit)
    try: first=run(sessions,settings)
    finally:event.remove(Session,'before_commit',before_commit)
    assert first.status=='failed'
    with sessions() as db:
        assert db.get(Document,'only') is None
        assert not db.get(ScanDirectory,(first.run_id,'root')).done
    result=finish(sessions,settings,first.run_id)
    assert result.status=='succeeded' and result.documents_new==1


def test_cancelled_http_keeps_checkpoint_and_no_open_sql_transaction(local, monkeypatch):
    sessions, settings = local
    bind=sessions.kw['bind']; active=set(); cancelled=[False]
    def begin(conn): active.add(id(conn))
    def end(conn): active.discard(id(conn))
    event.listen(bind,'begin',begin);event.listen(bind,'commit',end);event.listen(bind,'rollback',end)
    class Client:
        def __init__(self,*_):pass
        async def list_nodes(self,ws,operator,parent,token,**kwargs):
            assert not active, 'HTTP cannot retain a SQL transaction'
            if token and not cancelled[0]:
                cancelled[0]=True
                raise asyncio.CancelledError()
            return {'items':[node('second' if token else 'first')],'next_token':'' if token else 'second'}
    monkeypatch.setattr(service,'DingtalkClient',Client)
    try:
        first=run(sessions,settings)
        with pytest.raises(asyncio.CancelledError): run(sessions,settings,first.run_id)
        assert finish(sessions,settings,first.run_id).status=='succeeded'
    finally:
        event.remove(bind,'begin',begin);event.remove(bind,'commit',end);event.remove(bind,'rollback',end)


def test_completed_checkpoint_cleanup_is_bounded_and_retains_audit(local):
    from app.scan_pages import cleanup_completed
    from app.db import ScanCycle
    sessions,_=local
    with sessions.begin() as db:
        db.add(ScanCycle(month='2026-10',status='completed'))
        db.add(SyncRun(run_id='old',workspace_id='synthetic-space',status='succeeded',finished_at=utcnow()-timedelta(days=8)))
        db.add(ScanProgress(run_id='old',workspace_id='synthetic-space',cycle_month='2026-10',phase='complete'))
        db.add(ScanProgress(run_id='unfinished',workspace_id='synthetic-space',cycle_month='2026-10',phase='walk'))
        db.add_all([ScanSeen(run_id='old',node_id=str(n)) for n in range(7)])
    with sessions() as db:
        assert cleanup_completed(db,limit=3)==3
        assert len(db.scalars(select(ScanSeen)).all())==4
        assert db.get(SyncRun,'old') and db.get(ScanProgress,'unfinished')


def test_month_end_preflight_refuses_crossing_expiring_cycle(local, monkeypatch):
    from datetime import datetime
    from app import scan_recovery
    sessions,settings=local
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,11,30,tzinfo=tz)
    monkeypatch.setattr(scan_recovery,'datetime',Clock)
    settings=settings.model_copy(update={'dingtalk_daily_limit':6,'scan_daily_reserve':1})
    with sessions() as db:
        result=scan_recovery.budget_preflight(db,settings,[])
        assert result['estimatedMinimumDays']==2 and not result['approved'] and result['daysLeftInMonth']==1
