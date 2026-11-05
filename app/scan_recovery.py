"""Administrator-triggered, budgeted recovery of one durable monthly cycle."""
import argparse
import json
import math
import calendar
import os
from datetime import datetime
from uuid import UUID
from sqlalchemy import case, func, select, union_all
from .api_usage import CST, usage_status
from .bi_models import ScanRecoveryRun
from .db import AuditState, Document, HistoricalFileNode, ScanCycle, ScanTask, SyncRun, SessionLocal, Workspace, utcnow
from .metadata_mode import naive
from .metrics import primary_snapshot_id
from .scan_scheduler import scan_lock
from .service import workspace_name_is_ignored
from .scan_models import ScanProgress


def budget_preflight(db, settings, ids):
    # Once per explicit recovery, never in a worker/idle loop. Deduplicate the
    # overlapping inventories before estimating directory pages/empty folders.
    estimate = 5
    live = select(Document.workspace_id.label('ws'),Document.parent_node_id.label('parent'),
                  Document.is_folder.label('folder')).where(Document.workspace_id.in_(ids))
    h=HistoricalFileNode
    base=select(h.workspace_id,h.parent_node_id,(func.lower(h.node_type)=='folder').label('folder')).where(
        h.snapshot_id==primary_snapshot_id(db),h.workspace_id.in_(ids),
        ~select(Document.node_id).where(Document.node_id==h.node_id).exists())
    nodes=union_all(live,base).subquery()
    groups={ws:{'pages':0,'parents':0,'folders':0} for ws in ids}
    for ws,parent,count,folders in db.execute(select(nodes.c.ws,nodes.c.parent,func.count(),func.sum(case((nodes.c.folder,1),else_=0))).group_by(nodes.c.ws,nodes.c.parent)):
        g=groups[ws];g['pages']+=math.ceil(count/30);g['parents']+=1;g['folders']+=int(folders or 0)
    estimate=math.ceil((estimate+sum(g['pages']+max(0,g['folders']+1-g['parents']) for g in groups.values()))*1.2)
    usage = usage_status(db, settings)
    calls = {(r['period'], r['interface']): r['calls'] for r in usage['items']}
    now = datetime.now(CST)
    day, month = now.strftime('day:%Y-%m-%d'), now.strftime('month:%Y-%m')
    daily_remaining = max(0, settings.dingtalk_daily_limit - settings.scan_daily_reserve - calls.get((day, 'total'), 0))
    remaining = min(settings.dingtalk_monthly_limit - settings.scan_monthly_reserve - calls.get((month, 'total'), 0),
                    settings.dingtalk_nodes_monthly_limit - calls.get((month, 'wiki.nodes'), 0),
                    settings.scan_max_api_calls - calls.get((now.strftime('scan:%Y-%m'), 'total'), 0))
    capacity = max(1, settings.dingtalk_daily_limit - settings.scan_daily_reserve)
    minimum_days = 1 + math.ceil(max(0, estimate - daily_remaining)/capacity)
    return {'estimatedCalls': estimate, 'remainingCalls': max(0, remaining), 'dailyRemainingCalls': daily_remaining,
            'estimatedMinimumDays': minimum_days, 'daysLeftInMonth': calendar.monthrange(now.year, now.month)[1] - now.day + 1,
            'approved': estimate <= remaining and daily_remaining > 0 and minimum_days <= calendar.monthrange(now.year, now.month)[1] - now.day + 1}


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


def resume_failed(db, settings, recovery_id, resume_id, reason, *, expected_failed, expected_success, apply=False):
    """Preview by default. Applying requires a fresh, explicit authorization ID."""
    if str(UUID(resume_id)) != resume_id or not reason or len(reason) > 255:
        raise ValueError('valid resume ID and reason required')
    recovery = db.get(ScanRecoveryRun, recovery_id)
    month = datetime.now(CST).strftime('%Y-%m')
    if not recovery or recovery.month != month:
        raise ValueError('current original recovery required')
    prior = json.loads(recovery.prior_tasks_json)
    history = prior.get('resumptions', [])
    if any(r['resumeId'] == resume_id for r in history):
        return {'status': 'already_applied', 'month': month, 'resumeId': resume_id}
    cycle = db.get(ScanCycle, month)
    if not cycle or cycle.status != 'completed_with_errors' or cycle.recovery_id != recovery_id:
        raise ValueError('original failed cycle must be stopped')
    tasks = list(db.scalars(select(ScanTask).where(ScanTask.month == month).order_by(ScanTask.workspace_id)))
    original = set(json.loads(recovery.target_workspace_ids_json))
    if set(json.loads(cycle.target_workspace_ids_json)) != original or {t.workspace_id for t in tasks} != original:
        raise ValueError('frozen workspace scope mismatch')
    failed = [t for t in tasks if t.status == 'dead_letter']
    succeeded = [t for t in tasks if t.status == 'succeeded']
    if len(failed) != expected_failed or len(succeeded) != expected_success or len(failed)+len(succeeded) != len(tasks):
        raise ValueError('failed/success counts changed')
    runs = {r.run_id: r for r in db.scalars(select(SyncRun).where(SyncRun.run_id.in_([t.run_id for t in tasks])))}
    checkpoints = {r.run_id: r for r in db.scalars(select(ScanProgress).where(ScanProgress.run_id.in_([t.run_id for t in tasks])))}
    for task in tasks:
        run, progress = runs.get(task.run_id), checkpoints.get(task.run_id)
        if task.recovery_id != recovery_id or not run or not progress or run.workspace_id != task.workspace_id or progress.workspace_id != task.workspace_id or progress.cycle_month != month:
            raise ValueError('missing or conflicting original checkpoint')
        if task.status == 'succeeded' and (run.status != 'succeeded' or progress.phase != 'complete'):
            raise ValueError('success proof mismatch')
        if task.status == 'dead_letter' and progress.phase == 'complete':
            raise ValueError('completed checkpoint needs reconciliation first')
    result = {'status': 'ready_to_resume' if not apply else 'resume_applied', 'month': month,
              'failedCount': len(failed), 'preservedSuccessCount': len(succeeded), 'resumeId': resume_id,
              'keepsOriginalRunIds': True, 'noInventoryEstimate': True}
    if not apply:
        return result
    if not settings.metadata_only or any((settings.review_enabled, settings.notify_enabled, settings.content_extract_enabled, settings.model_allow_content_transfer)):
        raise ValueError('metadata-only paused review/notification mode required')
    history.append({'resumeId': resume_id, 'requestedAt': utcnow().isoformat(), 'reason': reason,
                    'failedTasks': [{'workspaceId': t.workspace_id, 'runId': t.run_id, 'attempts': t.attempts, 'errorCode': t.error_code} for t in failed]})
    prior['resumptions'] = history
    recovery.prior_tasks_json, recovery.finished_at = json.dumps(prior), None
    for task in failed:
        cooling_down = task.error_code.startswith('dingtalk_rate_limited') and task.next_attempt_at and naive(task.next_attempt_at) > naive(utcnow())
        task.status, task.attempts = 'pending', 0
        if not cooling_down:
            task.error_code, task.next_attempt_at = '', None
    cycle.status, cycle.finished_at = 'running', None
    return result


def main():
    from .config import get_settings
    p = argparse.ArgumentParser()
    p.add_argument('--recovery-id', required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('--resume-failed', action='store_true')
    p.add_argument('--resume-id')
    p.add_argument('--expected-failed', type=int)
    p.add_argument('--expected-success', type=int)
    p.add_argument('--apply', action='store_true', help='Actually reopen only failed tasks after user approval')
    args = p.parse_args()
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise SystemExit('deployment administrator required')
    settings = get_settings()
    with SessionLocal() as db:
        with scan_lock(db) as got:
            if not got:
                raise SystemExit('scan busy')
            if args.resume_failed:
                if not args.resume_id or args.expected_failed is None or args.expected_success is None:
                    p.error('--resume-failed needs --resume-id, --expected-failed and --expected-success')
                result = resume_failed(db, settings, args.recovery_id, args.resume_id, args.reason,
                                       expected_failed=args.expected_failed, expected_success=args.expected_success, apply=args.apply)
            else:
                if args.apply:
                    p.error('--apply is only valid with --resume-failed')
                result = prepare_recovery(db, settings, args.recovery_id, args.reason)
            db.commit()
            print(json.dumps(result))


if __name__ == '__main__':
    main()
