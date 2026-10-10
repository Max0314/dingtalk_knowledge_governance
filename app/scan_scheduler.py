"""One durable calibration per calendar month; failed libraries retry alone."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import threading

from sqlalchemy import select, text

from .api_usage import CST, scan_budget
from .db import ScanCycle, ScanTask, utcnow
from .integrations import IntegrationError
from .service import resolve_watch_targets, watch_workspace

_local_lock = threading.Lock()


@contextmanager
def scan_lock(db):
    # Shared with API/manual runs. Keep this dedicated connection alive until
    # the entire slice finishes; process death automatically releases MySQL lock.
    if db.bind.dialect.name == "mysql":
        with db.bind.connect() as conn:
            got = conn.execute(text("SELECT GET_LOCK('kg_monthly_scan', 0)")).scalar()
            try:
                yield bool(got)
            finally:
                if got:
                    conn.execute(text("SELECT RELEASE_LOCK('kg_monthly_scan')"))
    else:
        got = _local_lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                _local_lock.release()


async def run_scan_slice_async(db, settings, *, force=False) -> dict:
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
        cycle = db.get(ScanCycle, month)
        for old in db.scalars(select(ScanCycle).where(ScanCycle.month < month, ScanCycle.status == "running")).all():
            old.status, old.finished_at = "expired", utcnow()
        db.commit()
        if cycle is None and not force and today.day < due_day:
            return {"status": "idle", "walked": []}
        if cycle is not None and cycle.status != "running":
            return {"status": cycle.status, "month": month, "walked": []}
        with scan_budget(month):
            if cycle is None:
                targets = await resolve_watch_targets(settings, force=True)
                if not targets["resolved"]:
                    return {"status": "no_targets", "walked": []}
                cycle = ScanCycle(month=month)
                db.add(cycle)
                for target in targets["resolved"]:
                    space = target.get("space") or {}
                    db.add(ScanTask(month=month, workspace_id=target["workspace_id"],
                                    root_node_id=space.get("root_node_id", ""), workspace_name=target["name"]))
                db.commit()
            tasks = db.scalars(select(ScanTask).where(ScanTask.month == month)
                               .order_by(ScanTask.workspace_id)).all()
            now = utcnow().replace(tzinfo=None)
            for task in tasks:
                if task.status == "running":
                    # We own the global lock: previous owner was interrupted.
                    task.status = "dead_letter" if task.attempts >= settings.scan_max_attempts else "pending"
                    task.error_code = "interrupted_by_restart"
                    task.next_attempt_at = now + timedelta(seconds=settings.scan_retry_seconds)
            db.commit()
            walked = []
            for task in tasks:
                if task.status != "pending":
                    continue
                if task.next_attempt_at and task.next_attempt_at.replace(tzinfo=None) > now:
                    continue
                task.status, task.attempts = "running", task.attempts + 1
                db.commit()  # Reserve attempt durably BEFORE touching DingTalk.
                space = {"workspace_id": task.workspace_id, "name": task.workspace_name,
                         "root_node_id": task.root_node_id}
                try:
                    run = await watch_workspace(db, settings, task.workspace_id, space, mode="watch")
                    task.run_id, task.error_code = run.run_id, run.error_code
                    success = run.status in ("succeeded", "skipped")
                except IntegrationError as exc:
                    db.rollback()
                    task.error_code = exc.code
                    success = False
                except Exception:
                    db.rollback()
                    task.error_code = "scan_execution_failed"
                    success = False
                task.status = "succeeded" if success else ("dead_letter" if task.attempts >= settings.scan_max_attempts else "pending")
                task.next_attempt_at = None if success else now + timedelta(seconds=settings.scan_retry_seconds * 2 ** (task.attempts - 1))
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
                db.commit()
            return {"status": cycle.status, "month": month, "walked": walked,
                    "remaining": sum(t.status in ("pending", "running") for t in tasks)}


def run_scan_slice(db, settings):
    return asyncio.run(run_scan_slice_async(db, settings))
