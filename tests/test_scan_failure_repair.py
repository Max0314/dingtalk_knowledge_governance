import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DataError
from app import integrations, scan_recovery
from app.config import Settings
from app.db import Base, Document, FileAuditEvent, ScanCycle, ScanTask, SyncRun, utcnow
from app.scan_models import ScanDirectory, ScanProgress, ScanSeen
from app.bi_models import ScanRecoveryRun
from app.dingtalk_errors import from_response, diagnostic_detail
from scripts.migrate_size_bytes import migrate


@pytest.mark.parametrize('status,provider,expected,retryable',[
    (403,'Forbidden.UserPermissionDenied','dingtalk_permission_denied',False),
    (403,'Forbidden.Unknown','dingtalk_forbidden',True),
    (403,'Forbidden.PermissionCheckServiceUnavailable','dingtalk_forbidden',True),
    (403,'Throttling.User','dingtalk_rate_limited',True),
    (429,None,'dingtalk_rate_limited',True),
    (503,'SystemError','dingtalk_service_unavailable',True),
    (401,'InvalidAccessToken','dingtalk_authentication_failed',False),
])
def test_provider_error_keeps_safe_code_and_does_not_assume_all_403_are_permission(status,provider,expected,retryable):
    response=httpx.Response(status,json={'code':provider,'message':'synthetic-token synthetic-person phone=13800000000',
        'requestId':'synthetic-request-id'},headers={'Retry-After':'1200'})
    exc=from_response(response,endpoint='wiki.nodes',secrets=('synthetic-token',))
    assert exc.code==expected and exc.retryable==retryable
    detail=json.loads(diagnostic_detail(exc))
    assert detail['retryAfter']==1200
    if provider in __import__('app.dingtalk_errors',fromlist=['SAFE_CODES']).SAFE_CODES:
        assert detail['providerCode']==provider
    elif provider:
        assert detail['providerCode'] is None and detail['providerCodeHash']
    assert detail['requestId'] is None and detail['requestIdHash']
    assert 'synthetic-token' not in diagnostic_detail(exc) and 'synthetic-person' not in diagnostic_detail(exc)


def test_html_and_untrusted_error_identifiers_never_leak_payload():
    exc=from_response(httpx.Response(403,text='<html>synthetic-secret</html>'),endpoint='wiki.nodes')
    assert exc.code=='dingtalk_forbidden' and json.loads(diagnostic_detail(exc))['responseKind']=='non_json'
    exc=from_response(httpx.Response(403,json={'code':'Forbidden.synthetic-secret','requestId':'synthetic-secret'}),endpoint='wiki.nodes',secrets=('synthetic-secret',))
    assert 'synthetic-secret' not in diagnostic_detail(exc)
    exc=from_response(httpx.Response(403,json={'code':'Forbidden.13800000000','requestId':'13800000000'}),endpoint='wiki.nodes')
    detail=diagnostic_detail(exc)
    assert '13800000000' not in detail and json.loads(detail)['providerCodeHash']


def test_database_error_preserves_errno_and_column_without_sql_or_parameters():
    exc=DataError('INSERT synthetic-private-data',{'name':'synthetic-person'},Exception(1264,"Out of range value for column 'size' at row 1"))
    value=json.loads(diagnostic_detail(exc))
    assert value=={'kind':'database','exception':'DataError','errno':1264,'column':'size'}


@pytest.mark.parametrize('wrapped',[True,False])
def test_workspace_detail_unwraps_provider_workspace_and_keeps_owner(monkeypatch,wrapped):
    item={'workspaceId':'synthetic-space','rootNodeId':'synthetic-root','permissionRole':'OWNER'}
    payload={'workspace':item} if wrapped else item
    monkeypatch.setattr(integrations,'reserve_call',lambda *a:None)
    async def token(*a):return 'synthetic-token'
    monkeypatch.setattr(integrations.DingtalkClient,'_token_value',token)
    monkeypatch.setattr(integrations,'outbound_client',lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))))
    row=asyncio.run(integrations.DingtalkClient(Settings(_env_file=None)).workspace_detail('synthetic-space','synthetic-operator'))
    assert row['root_node_id']=='synthetic-root' and row['permission_role']=='OWNER'


def test_exact_big_sizes_and_legacy_fallback_for_documents_and_audit(tmp_path):
    bind=create_engine('sqlite:///'+str(tmp_path/'bytes.db'));Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind)
    with sessions.begin() as db:
        db.add(Document(node_id='big',workspace_id='synthetic',name='synthetic',size=30483771330))
        db.add(Document(node_id='legacy',workspace_id='synthetic',name='synthetic',_legacy_size=123))
        db.add(FileAuditEvent(event_key='synthetic-audit',biz_id='synthetic',gmt_create=1,size=30483771330))
    with sessions() as db:
        doc=db.get(Document,'big')
        assert doc.size==30483771330 and doc._legacy_size==-1
        assert db.scalar(select(Document.size).where(Document.node_id=='big'))==30483771330
        assert db.get(Document,'legacy').size==123
        assert db.scalar(select(FileAuditEvent.size))==30483771330
    # A rollback writer knows only the legacy column. Its subsequent update,
    # including zero, must win over a stale extended-size column on rollforward.
    with bind.begin() as c:c.execute(text("UPDATE documents SET size=0 WHERE node_id='big'"))
    with sessions() as db:
        assert db.get(Document,'big').size==0
        assert db.scalar(select(Document.size).where(Document.node_id=='big'))==0
    with bind.begin() as c:
        c.execute(text('ALTER TABLE documents DROP COLUMN size_bytes'))
        c.execute(text('ALTER TABLE file_audit_events DROP COLUMN size_bytes'))
    assert migrate(bind)['added_size_bytes']==['documents','file_audit_events']
    assert migrate(bind)['added_size_bytes']==[]
    with sessions() as db:assert db.get(Document,'legacy').size==123
    bind.dispose()


def test_failed_only_resume_is_dry_by_default_and_preserves_successes_and_checkpoints(tmp_path):
    bind=create_engine('sqlite:///'+str(tmp_path/'resume.db'));Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind,expire_on_commit=False)
    month=datetime.now(scan_recovery.CST).strftime('%Y-%m');rid=str(uuid4());nonce=str(uuid4())
    ids=['good-a','good-b','bad']
    with sessions.begin() as db:
        db.add(ScanRecoveryRun(recovery_id=rid,month=month,requested_by='synthetic',reason='synthetic',
              target_workspace_ids_json=json.dumps(ids),prior_tasks_json='{"tasks":[]}'))
        db.add(ScanCycle(month=month,status='completed_with_errors',recovery_id=rid,target_workspace_ids_json=json.dumps(ids)))
        for ws in ids:
            good=ws.startswith('good')
            db.add(ScanTask(month=month,workspace_id=ws,status='succeeded' if good else 'dead_letter',attempts=3,
                           recovery_id=rid,run_id=ws,error_code='' if good else 'old-error'))
            db.add(SyncRun(run_id=ws,workspace_id=ws,status='succeeded' if good else 'failed',documents_seen=20))
            db.add(ScanProgress(run_id=ws,workspace_id=ws,cycle_month=month,phase='complete' if good else 'walk'))
        db.add(ScanDirectory(run_id='bad',parent_id='next-folder',next_token='saved-token'))
        db.add(ScanSeen(run_id='bad',node_id='already-seen'))
    s=Settings(_env_file=None,metadata_only=True,content_extract_enabled=False)
    def resume(db,apply=False,key=nonce,failed=1):
        return scan_recovery.resume_failed(db,s,rid,key,'approved synthetic',expected_failed=failed,expected_success=2,apply=apply)
    with sessions.begin() as db:
        assert resume(db)['status']=='ready_to_resume'
        assert db.get(ScanTask,(month,'bad')).status=='dead_letter'
        assert db.get(ScanCycle,month).status=='completed_with_errors'
        with pytest.raises(ValueError,match='counts'):resume(db,failed=2)
        bad=db.get(ScanTask,(month,'bad'))
        bad.error_code='dingtalk_rate_limited:429'
        bad.next_attempt_at=utcnow()+timedelta(hours=1)
    with sessions.begin() as db:
        assert resume(db,True)['status']=='resume_applied'
        for ws in ids[:2]:
            row=db.get(ScanTask,(month,ws));assert row.status=='succeeded' and row.attempts==3 and row.run_id==ws
        row=db.get(ScanTask,(month,'bad'));assert row.status=='pending' and row.attempts==0 and row.run_id=='bad'
        assert row.error_code=='dingtalk_rate_limited:429'
        assert row.next_attempt_at.replace(tzinfo=None)>utcnow().replace(tzinfo=None)
        assert db.get(ScanDirectory,('bad','next-folder')).next_token=='saved-token'
        assert db.get(ScanSeen,('bad','already-seen'))
        assert db.get(ScanCycle,month).recovery_id==rid
        assert len(json.loads(db.get(ScanRecoveryRun,rid).prior_tasks_json)['resumptions'])==1
        row.attempts=1
    with sessions.begin() as db:
        assert resume(db,True)['status']=='already_applied'
        assert db.get(ScanTask,(month,'bad')).attempts==1
        with pytest.raises(ValueError,match='stopped'):resume(db,True,str(uuid4()))
    bind.dispose()
