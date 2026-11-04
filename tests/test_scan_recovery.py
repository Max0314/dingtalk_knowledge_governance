import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.config import Settings
from app.db import AuditState, Base, ScanCycle, ScanTask, SyncRun, Workspace, utcnow
from app.bi_models import ScanRecoveryRun
from app import scan_recovery


def test_recovery_id_is_idempotent_and_preserves_previous_attempts(tmp_path, monkeypatch):
    e = create_engine('sqlite:///' + str(tmp_path / 'recovery.db'))
    Base.metadata.create_all(e)
    sessions = sessionmaker(bind=e)
    month = scan_recovery.datetime.now(scan_recovery.CST).strftime('%Y-%m')
    monkeypatch.setattr(scan_recovery, 'budget_preflight', lambda *a: {'approved':True,'estimatedCalls':10,'remainingCalls':100})
    with sessions.begin() as db:
        db.add_all([Workspace(workspace_id='synthetic-space',name='C-Synthetic',metadata_confirmed_at=datetime.now(timezone.utc)),
                    ScanCycle(month=month,status='completed'),ScanTask(month=month,workspace_id='synthetic-space',status='succeeded',attempts=3)])
    recovery_id = str(uuid4())
    with sessions.begin() as db:
        result = scan_recovery.prepare_recovery(db,Settings(_env_file=None,watch_workspaces='*'),recovery_id,'migration recovery')
        assert result['status'] == 'prepared'
    with sessions.begin() as db:
        saved = db.get(ScanRecoveryRun,recovery_id)
        assert json.loads(saved.prior_tasks_json)['tasks'][0]['attempts'] == 3
        task = db.get(ScanTask,(month,'synthetic-space'))
        assert task.attempts == 0
        task.status, task.attempts = 'succeeded',1
    with sessions.begin() as db:
        assert scan_recovery.prepare_recovery(db,Settings(_env_file=None,watch_workspaces='*'),recovery_id,'same recovery')['status'] == 'resumed'
        assert db.get(ScanTask,(month,'synthetic-space')).status == 'succeeded'
        assert db.get(ScanTask,(month,'synthetic-space')).attempts == 1
    e.dispose()


def test_insufficient_budget_does_not_reset_cycle(tmp_path,monkeypatch):
    e=create_engine('sqlite:///'+str(tmp_path/'budget.db'))
    Base.metadata.create_all(e)
    sessions=sessionmaker(bind=e)
    monkeypatch.setattr(scan_recovery,'budget_preflight',lambda *a:{'approved':False,'estimatedCalls':20,'remainingCalls':10})
    with sessions.begin() as db:
        db.add(Workspace(workspace_id='synthetic-space',name='C-Synthetic',metadata_confirmed_at=datetime.now(timezone.utc)))
    with sessions.begin() as db:
        assert scan_recovery.prepare_recovery(db,Settings(_env_file=None,watch_workspaces='*'),str(uuid4()),'recovery')['status']=='budget_insufficient'
        assert not db.scalars(select(ScanRecoveryRun)).all()
        assert not db.scalars(select(ScanCycle)).all()
    e.dispose()


def test_complete_requires_frozen_targets_and_real_current_runs(tmp_path):
    from app.scan_scheduler import complete_cycle
    e=create_engine('sqlite:///'+str(tmp_path/'complete.db'))
    Base.metadata.create_all(e)
    sessions=sessionmaker(bind=e,autoflush=False)
    now=utcnow()
    with sessions.begin() as db:
        cycle=ScanCycle(month='2026-11',status='completed',started_at=now,finished_at=now,target_workspace_ids_json='["synthetic-a","synthetic-b"]')
        run=SyncRun(run_id='synthetic-run',workspace_id='synthetic-a',status='succeeded',collection_started_at=now)
        task=ScanTask(month='2026-11',workspace_id='synthetic-a',run_id=run.run_id,status='succeeded')
        db.add_all([cycle,run,task]);db.flush()
        complete_cycle(db,cycle,[task])
        assert cycle.status=='completed_with_errors'
        assert db.get(AuditState,1) is None
        cycle.target_workspace_ids_json='["synthetic-a"]'
        cycle.status='completed'
        complete_cycle(db,cycle,[task]);db.flush()
        assert db.get(AuditState,1).inventory_reconciled_at.replace(tzinfo=timezone.utc)==now
    e.dispose()
