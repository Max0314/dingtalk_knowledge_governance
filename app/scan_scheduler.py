"""One durable calibration per calendar month; failed libraries retry alone."""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone
from uuid import uuid4
import threading

from sqlalchemy import select, text

from .api_usage import CST, scan_budget
from .db import ApiUsage, AuditState, ScanCycle, ScanTask, SyncRun, utcnow
from .integrations import IntegrationError
from .service import resolve_watch_targets, watch_workspace
from .scan_models import ScanProgress

_local_lock = threading.Lock()


@contextmanager
def scan_lock(db):
    # Shared with API/manual runs. Keep this dedicated connection alive until
    # the entire slice finishes; process death automatically releases MySQL lock.
    if db.bind.dialect.name == "mysql":
        with db.bind.connect() as conn:
            got = conn.execute(text("SELECT GET_LOCK('kg_monthly_scan', 0)")).scalar()
            conn.commit()
            try:
                yield bool(got)
            finally:
                if got:
                    conn.execute(text("SELECT RELEASE_LOCK('kg_monthly_scan')"))
                    conn.commit()
    else:
        got = _local_lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                _local_lock.release()


async def run_scan_slice_async(db, settings, *, force=False, expected_month=None) -> dict:
    if not settings.full_scan_enabled:
        return {"status": "disabled", "walked": []}
    if not settings.watch_workspaces:
        return {"status": "unconfigured", "walked": []}
    today = datetime.now(CST).date()
    month = today.strftime("%Y-%m")
    # Legacy comma lists cannot create a second calibration in the same month.
    days = [int(x.strip()) for x in settings.scan_days.split(",") if x.strip().isdigit() and 1 <= int(x.strip()) <= 28]
    due_day = min(days) if days else 10
    with scan_lock(db) as got:
        if not got:
            return {"status": "busy", "walked": []}
        if expected_month:
            if month != expected_month or datetime.now(CST).strftime('%Y-%m') != expected_month:
                return {'status': 'cycle_outside_month', 'walked': []}
            requested = db.get(ScanCycle, expected_month)
            if requested is None or requested.status != 'running':
                return {'status': 'cycle_not_running', 'walked': []}
        from .scan_pages import cleanup_completed
        cleanup_completed(db)
        cycle = db.get(ScanCycle, month)
        for old in db.scalars(select(ScanCycle).where(ScanCycle.month < month, ScanCycle.status == "running")).all():
            old.status, old.finished_at = "expired", utcnow()
        db.commit()
        if cycle is None and not force and today.day < due_day:
            return {"status": "idle", "walked": []}
        if cycle is not None and cycle.status != "running":
            return {"status": cycle.status, "month": month, "walked": []}
        used = db.get(ApiUsage, (datetime.now(CST).strftime('day:%Y-%m-%d'), 'total'))
        if (used.calls if used else 0) >= max(0, settings.dingtalk_daily_limit - settings.scan_daily_reserve):
            db.rollback()
            return {'status': 'daily_budget_wait', 'month': month, 'walked': []}
        with scan_budget(month):
            if cycle is None:
                db.commit()
                targets = await resolve_watch_targets(settings, force=True)
                if not targets["resolved"]:
                    return {"status": "no_targets", "walked": []}
                cycle = ScanCycle(month=month, target_workspace_ids_json=json.dumps([t['workspace_id'] for t in targets['resolved']]))
                db.add(cycle)
                for target in targets["resolved"]:
                    space = target.get("space") or {}
                    db.add(ScanTask(month=month, workspace_id=target["workspace_id"],
                                    root_node_id=space.get("root_node_id", ""), workspace_name=target["name"]))
                db.commit()
            task_query = select(ScanTask).where(ScanTask.month == month)
            if cycle.recovery_id:
                task_query = task_query.where(ScanTask.recovery_id == cycle.recovery_id)
            tasks = db.scalars(task_query
                               .order_by(ScanTask.workspace_id)).all()
            now = utcnow().replace(tzinfo=None)
            for task in tasks:
                if task.status == "running":
                    # We own the global lock: previous owner was interrupted.
                    task.status = "pending" if settings.metadata_only and task.run_id else ("dead_letter" if task.attempts >= settings.scan_max_attempts else "pending")
                    task.error_code = "interrupted_by_restart"
                    task.next_attempt_at = None if settings.metadata_only else now + timedelta(seconds=settings.scan_retry_seconds)
            db.commit()
            walked = []
            for task in tasks:
                if task.status != "pending":
                    continue
                if task.next_attempt_at and task.next_attempt_at.replace(tzinfo=None) > now:
                    continue
                prior_run = db.get(SyncRun, task.run_id) if settings.metadata_only and task.run_id else None
                continuation = bool(settings.metadata_only and task.run_id and (prior_run is None or prior_run.status in ('running', 'paused')))
                task.status = 'running'
                if not continuation:
                    task.attempts += 1
                if settings.metadata_only and not task.run_id:
                    task.run_id = str(uuid4())
                db.commit()  # Reserve attempt durably BEFORE touching DingTalk.
                space = {"workspace_id": task.workspace_id, "name": task.workspace_name,
                         "root_node_id": task.root_node_id}
                run = None
                try:
                    options = {'resume_run_id': task.run_id} if settings.metadata_only else {}
                    run = await watch_workspace(db, settings, task.workspace_id, space, mode="watch", **options)
                    task.run_id, task.error_code = run.run_id, run.error_code
                    success = run.status == "succeeded"
                except IntegrationError as exc:
                    db.rollback()
                    task.error_code = exc.code
                    success = False
                except Exception:
                    db.rollback()
                    task.error_code = "scan_execution_failed"
                    success = False
                in_progress = bool(settings.metadata_only and run and run.run_id == task.run_id and run.status in ('running', 'paused'))
                task.status = 'pending' if in_progress else ('succeeded' if success else ('dead_letter' if task.attempts >= settings.scan_max_attempts else 'pending'))
                task.next_attempt_at = None if success or in_progress else utcnow() + timedelta(seconds=settings.scan_retry_seconds * 2 ** max(0, task.attempts - 1))
                if task.error_code.startswith('dingtalk_scan_daily_reserved'):
                    tomorrow = datetime.now(CST).replace(hour=0, minute=1, second=0, microsecond=0) + timedelta(days=1)
                    task.next_attempt_at = tomorrow.astimezone(timezone.utc).replace(tzinfo=None)
                walked.append({"workspace_id": task.workspace_id, "status": task.status,
                               "attempts": task.attempts, "error_code": task.error_code})
                if task.error_code.startswith("dingtalk_budget_"):
                    cycle.status, cycle.finished_at = "budget_exhausted", utcnow()
                db.commit()
                if cycle.status != "running" or len(walked) >= max(1, settings.watch_slice_size):
                    break
            if cycle.status == "running" and all(t.status in ("succeeded", "dead_letter") for t in tasks):
                cycle.status = "completed_with_errors" if any(t.status == "dead_letter" for t in tasks) else "completed"
                cycle.finished_at = utcnow()
                complete_cycle(db, cycle, tasks)
                db.commit()
            return {"status": cycle.status, "month": month, "walked": walked,
                    "remaining": sum(t.status in ("pending", "running") for t in tasks)}


def run_scan_slice(db, settings, *, expected_month=None):
    return asyncio.run(run_scan_slice_async(db, settings, expected_month=expected_month))


def complete_cycle(db, cycle, tasks):
    from .bi_models import ScanRecoveryRun
    from .metadata_mode import naive
    recovery = db.get(ScanRecoveryRun, cycle.recovery_id) if cycle.recovery_id else None
    expected = set(json.loads(recovery.target_workspace_ids_json if recovery else cycle.target_workspace_ids_json or '[]'))
    requested = recovery.requested_at if recovery else cycle.started_at
    runs = {r.run_id:r for r in db.scalars(select(SyncRun).where(SyncRun.run_id.in_([t.run_id for t in tasks])))}
    complete = bool(expected) and expected == {t.workspace_id for t in tasks} and all(
        t.status == 'succeeded' and t.run_id in runs and runs[t.run_id].status == 'succeeded'
        and runs[t.run_id].workspace_id == t.workspace_id and runs[t.run_id].collection_started_at is not None
        and naive(runs[t.run_id].collection_started_at) >= naive(requested) for t in tasks)
    if not complete:
        cycle.status = 'completed_with_errors'
    if recovery:
        recovery.finished_at = cycle.finished_at
    if complete:
        audit = db.get(AuditState, 1)
        if not audit:
            audit = AuditState(id=1)
            db.add(audit)
        audit.inventory_reconciled_at = cycle.finished_at
