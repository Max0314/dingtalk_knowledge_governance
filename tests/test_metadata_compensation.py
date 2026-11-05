import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker

from app import audit_bridge,audit_pull,metadata_compensation,api_usage
from app.config import Settings
from app.db import (Base,Document,FileAuditEvent,MetadataCompensationItem,MetadataCompensationRun,
                    ReviewJob,Notification,utcnow)


@pytest.fixture
def compensation(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'compensation.db'))
    Base.metadata.create_all(engine);sessions=sessionmaker(engine,autoflush=False)
    s=Settings(_env_file=None,metadata_only=True,review_enabled=False,notify_enabled=False,
        content_extract_enabled=False,model_allow_content_transfer=False,demo_mode=False,
        dingtalk_rate_dir=str(tmp_path/'rate'))
    monkeypatch.setattr(api_usage,'SessionLocal',sessions)
    yield sessions,s
    engine.dispose()


def event(key,age=2,**overrides):
    values=dict(event_key=key,biz_id=key,gmt_create=int((utcnow()-timedelta(days=age)).timestamp()*1000),
        action_view='创建文档',module_view='团队空间',resource='synthetic.adoc',extension='adoc')
    values.update(overrides)
    return FileAuditEvent(**values)


def test_incident_enrollment_is_bounded_frozen_idempotent_and_preserves_prior_attempts(compensation):
    sessions,s=compensation;run_id=str(uuid4());now=utcnow()
    since=int((now-timedelta(days=3)).timestamp()*1000);until=int(now.timestamp()*1000)
    with sessions.begin() as db:
        db.add(event('eligible',processed=True,resolution='metadata_event_expired',locate_attempts=2))
        db.add(event('ignored',processed=True,resolution='ignored_action',action_view='知识库添加成员'))
        db.add(event('old',age=20,processed=True,resolution='metadata_event_expired'))
        db.add(event('done',metadata_applied_at=now,processed=True,resolution='metadata_applied_review_paused'))
    with sessions.begin() as db:
        assert metadata_compensation.prepare(db,s,run_id,since,until)['status']=='preview'
        assert db.get(MetadataCompensationRun,run_id) is None
        metadata_compensation.prepare(db,s,run_id,since,until,apply=True)
    with sessions.begin() as db:db.add(event('after-freeze'))
    with sessions() as db:
        assert metadata_compensation.advance(db,s,limit=2)==1
        cursor=db.get(MetadataCompensationRun,run_id).cursor_id
    with sessions() as db:
        assert metadata_compensation.advance(db,s,limit=2)==0
        run=db.get(MetadataCompensationRun,run_id)
        assert run.status=='completed' and run.enrolled==1 and run.cursor_id>=cursor
        row=db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='eligible'))
        assert not row.processed and row.retry_started_at and row.locate_attempts==0
        saved=db.get(MetadataCompensationItem,row.id)
        assert saved.prior_attempts==2 and saved.prior_resolution=='metadata_event_expired'
        assert db.scalar(select(FileAuditEvent).where(FileAuditEvent.event_key=='after-freeze')).retry_started_at is None
        assert metadata_compensation.prepare(db,s,run_id,since,until,apply=True)['status']=='completed'
        with pytest.raises(ValueError):metadata_compensation.prepare(db,s,run_id,since+1,until,apply=True)
        assert db.scalars(select(ReviewJob)).all()==[] and db.scalars(select(Notification)).all()==[]


def test_metadata_eligibility_survives_quota_wait_without_replaying_old_history(compensation,monkeypatch):
    sessions,s=compensation
    with sessions.begin() as db:
        accepted=event('accepted',age=3,retry_started_at=utcnow()-timedelta(days=3))
        old=event('old',age=3)
        db.add_all([accepted,old])
    with sessions() as db:
        rows={e.event_key:e for e in db.scalars(select(FileAuditEvent))}
        assert not audit_bridge._metadata_event_expired(db,s,rows['accepted'])
        assert audit_bridge._metadata_event_expired(db,s,rows['old'])
        eligible=set(db.scalars(select(FileAuditEvent.event_key).where(*audit_bridge._metadata_window(s))))
        assert eligible=={'accepted'}


def test_fresh_ingestion_enrolls_once_but_overlap_does_not_extend_or_reopen(compensation):
    sessions,s=compensation;stamp=int(utcnow().timestamp()*1000)
    payload={'bizId':'synthetic','gmtCreate':stamp,'actionView':'创建文档','resource':'synthetic.adoc'}
    with sessions() as db:
        audit_pull._ingest(db,[payload],include_reads=False,retain_metadata=True);db.commit()
        row=db.scalar(select(FileAuditEvent));first=row.retry_started_at
        row.processed=True;row.resolution='dead_letter_locator_limit';row.locate_attempts=3;db.commit()
        audit_pull._ingest(db,[payload],include_reads=False,retain_metadata=True);db.commit()
        assert row.retry_started_at==first and row.processed and row.locate_attempts==3


@pytest.mark.parametrize('kind',['delete','restore'])
def test_old_compensated_local_action_cannot_override_newer_mirror(compensation,kind):
    sessions,s=compensation
    with sessions.begin() as db:
        doc=Document(node_id='synthetic-node',workspace_id='synthetic-space',name='synthetic',
            metadata_applied_at=utcnow(),is_deleted=kind=='restore')
        db.add(doc)
        row=event('local',matched_node_id='synthetic-node',match_status='confirmed',retry_started_at=utcnow())
        db.add(row)
        db.flush()
        audit_bridge._handle_delete_restore(db,s,row,kind,{})
        assert doc.is_deleted==(kind=='restore') and row.resolution=='metadata_superseded'


def test_enrolled_locator_still_stops_after_three_real_failures(compensation,monkeypatch):
    sessions,s=compensation;s=s.model_copy(update={'bridge_locator_enabled':True})
    class Client:
        def __init__(self,*a):pass
        async def search_dentries(self,*a,**k):return []
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Client)
    with sessions.begin() as db:db.add(event('retry',age=3,retry_started_at=utcnow()-timedelta(days=3),locate_attempts=2))
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        row=db.scalar(select(FileAuditEvent))
        assert row.locate_attempts==3 and row.processed and row.resolution=='dead_letter_locator_limit'


@pytest.mark.parametrize('confirmed',[False,True])
def test_crash_after_last_reservation_reaches_observable_terminal_without_http(compensation,monkeypatch,confirmed):
    sessions,s=compensation
    with sessions.begin() as db:
        db.add(event('interrupted',age=3,retry_started_at=utcnow()-timedelta(days=3),locate_attempts=3,
            match_status='confirmed' if confirmed else '',matched_node_id='synthetic' if confirmed else ''))
    class Forbidden:
        def __init__(self,*a):raise AssertionError('exhausted reservation must not call HTTP')
    monkeypatch.setattr(audit_bridge,'DingtalkClient',Forbidden)
    with sessions() as db:
        audit_bridge.process_audit_events(db,s,drain_walks=False)
        row=db.scalar(select(FileAuditEvent))
        assert row.processed and row.resolution=='dead_letter_interrupted_limit' and row.locate_attempts==3


@pytest.mark.parametrize('reverse',[False,True])
def test_delayed_delete_restore_use_source_order_not_processing_clock(compensation,reverse):
    sessions,s=compensation
    source=utcnow()-timedelta(hours=2)
    with sessions.begin() as db:
        doc=Document(node_id='ordered',workspace_id='synthetic',name='synthetic',metadata_applied_at=source-timedelta(hours=1))
        db.add(doc);db.flush()
        actions=[('delete',source),('restore',source+timedelta(minutes=1))]
        if reverse:actions.reverse()
        for kind,stamp in actions:
            row=event('ordered-'+kind,matched_node_id='ordered',match_status='confirmed',
                gmt_create=int(stamp.timestamp()*1000),retry_started_at=utcnow())
            db.add(row);db.flush()
            audit_bridge._handle_delete_restore(db,s,row,kind,{})
        assert not doc.is_deleted


def test_mysql_watermark_preserves_observation_microseconds():
    from sqlalchemy.dialects.mysql import dialect
    from sqlalchemy.schema import CreateTable
    from app.db import MetadataEventWatermark
    ddl=str(CreateTable(MetadataEventWatermark.__table__).compile(dialect=dialect()))
    assert 'observed_at DATETIME(6)' in ddl


def test_source_order_survives_a_new_session(compensation):
    sessions,s=compensation;source=utcnow()-timedelta(hours=2)
    with sessions.begin() as db:
        db.add(Document(node_id='persisted-order',workspace_id='synthetic',name='synthetic',metadata_applied_at=source-timedelta(hours=1)))
    for kind,stamp in [('delete',source),('restore',source+timedelta(minutes=1))]:
        with sessions.begin() as db:
            row=event('persisted-'+kind,matched_node_id='persisted-order',match_status='confirmed',
                gmt_create=int(stamp.timestamp()*1000),retry_started_at=utcnow())
            db.add(row);db.flush()
            audit_bridge._handle_delete_restore(db,s,row,kind,{})
    with sessions() as db:assert not db.get(Document,'persisted-order').is_deleted
