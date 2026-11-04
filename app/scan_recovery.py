"""Administrator-triggered, budgeted recovery of one durable monthly cycle."""
import argparse
import json
import math
import os
from datetime import datetime
from uuid import UUID
from sqlalchemy import case, func, select
from .api_usage import CST, usage_status
from .bi_models import ScanRecoveryRun
from .db import AuditState, Document, HistoricalFileNode, ScanCycle, ScanTask, SessionLocal, Workspace, utcnow
from .metadata_mode import naive
from .metrics import primary_snapshot_id
from .scan_scheduler import scan_lock
from .service import workspace_name_is_ignored


def budget_preflight(db, settings, ids):
    # Once per explicit recovery, never in a worker/idle loop. Conservative:
    # count both local inventories, including overlap, plus empty folder reads.
    estimate = 5
    for model in (Document, HistoricalFileNode):
        conditions = [model.workspace_id.in_(ids)]
        if model is HistoricalFileNode:
            conditions.append(model.snapshot_id == primary_snapshot_id(db))
        folder = model.is_folder.is_(True) if model is Document else func.lower(model.node_type) == 'folder'
        rows = db.execute(select(model.workspace_id, model.parent_node_id, func.count(), func.sum(case((folder, 1), else_=0))).where(*conditions)
                          .group_by(model.workspace_id, model.parent_node_id))
        for _, _, count, folders in rows:
            estimate += math.ceil(count / 30) + int(folders or 0)
    estimate = math.ceil((estimate + len(ids)) * 1.2)
    usage = usage_status(db, settings)
    calls = {(r['period'], r['interface']): r['calls'] for r in usage['items']}
    now = datetime.now(CST)
    day, month = now.strftime('day:%Y-%m-%d'), now.strftime('month:%Y-%m')
    remaining = min(settings.dingtalk_daily_limit - calls.get((day, 'total'), 0),
                    settings.dingtalk_monthly_limit - calls.get((month, 'total'), 0),
                    settings.dingtalk_nodes_monthly_limit - calls.get((month, 'wiki.nodes'), 0),
                    settings.scan_max_api_calls - calls.get((now.strftime('scan:%Y-%m'), 'total'), 0))
    return {'estimatedCalls': estimate, 'remainingCalls': max(0, remaining), 'approved': estimate <= remaining}


def prepare_recovery(db, settings, recovery_id, reason):
    if str(UUID(recovery_id)) != recovery_id or not reason or len(reason) > 255:
        raise ValueError('valid recovery ID and reason required')
    existing = db.get(ScanRecoveryRun, recovery_id)
    if existing:
        return {'status': 'resumed', 'month': existing.month, 'recoveryId': recovery_id}
    month = datetime.now(CST).strftime('%Y-%m')
    tokens = [t.strip() for t in settings.watch_workspaces.split(',') if t.strip()]
    if not tokens:
        raise ValueError('watch scope is unconfigured')
    spaces = [w for w in db.scalars(select(Workspace)).all() if w.is_active and w.metadata_confirmed_at is not None]
    selected = {}
    for token in tokens:
        matches = spaces if token == '*' else ([w for w in spaces if w.workspace_id == token]
                  or [w for w in spaces if w.name == token] or [w for w in spaces if token in w.name])
        if not matches:
            raise ValueError('watch scope contains unresolved workspace')
        selected.update({w.workspace_id:w for w in matches if not workspace_name_is_ignored(settings,w.name)})
    ids = sorted(selected)
    if not ids:
        raise ValueError('workspace registry is not ready')
    budget = budget_preflight(db, settings, ids)
    if not budget['approved']:
        return {'status': 'budget_insufficient', **budget, 'workspaceCount': len(ids)}
    now = utcnow()
    cycle = db.get(ScanCycle, month)
    if cycle and cycle.status == 'running' and cycle.recovery_id:
        raise ValueError('another recovery already running')
    tasks = {t.workspace_id: t for t in db.scalars(select(ScanTask).where(ScanTask.month == month))}
    prior = {'cycle': {'status': cycle.status, 'recoveryId': cycle.recovery_id} if cycle else None,
             'tasks': [{'workspaceId': t.workspace_id, 'status': t.status, 'attempts': t.attempts,
                        'runId': t.run_id, 'errorCode': t.error_code} for t in tasks.values()]}
    db.add(ScanRecoveryRun(recovery_id=recovery_id, month=month, requested_at=now,
                          requested_by='deployment-administrator', reason=reason,
                          target_workspace_ids_json=json.dumps(ids), prior_tasks_json=json.dumps(prior)))
    if not cycle:
        cycle = ScanCycle(month=month)
        db.add(cycle)
    cycle.recovery_id, cycle.status, cycle.started_at, cycle.finished_at = recovery_id, 'running', now, None
    cycle.target_workspace_ids_json = json.dumps(ids)
    for ws_id in ids:
        task = tasks.get(ws_id)
        if task is None:
            task = ScanTask(month=month, workspace_id=ws_id, workspace_name=db.get(Workspace, ws_id).name)
            db.add(task)
        task.recovery_id, task.status, task.attempts = recovery_id, 'pending', 0
        task.next_attempt_at, task.error_code, task.run_id = None, '', ''
    return {'status': 'prepared', 'month': month, 'recoveryId': recovery_id, 'workspaceCount': len(ids), **budget}


def main():
    from .config import get_settings
    p = argparse.ArgumentParser()
    p.add_argument('--recovery-id', required=True)
    p.add_argument('--reason', required=True)
    args = p.parse_args()
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise SystemExit('deployment administrator required')
    settings = get_settings()
    with SessionLocal() as db:
        with scan_lock(db) as got:
            if not got:
                raise SystemExit('scan busy')
            result = prepare_recovery(db, settings, args.recovery_id, args.reason)
            db.commit()
            print(json.dumps(result))


if __name__ == '__main__':
    main()
