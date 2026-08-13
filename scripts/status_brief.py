"""One-screen production vitals: watch progress, mirror size, reviews, pushes.

Read-only. Run inside the api container:
    docker compose exec -T api python scripts/status_brief.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.config import get_settings
from app.db import (Document, FileAuditEvent, Notification, ReviewInstance, ReviewJob, SessionLocal, SyncRun,
                    Workspace, init_db, utcnow)


def main() -> None:
    settings = get_settings()
    init_db()
    out: dict = {
        "watch_workspaces": settings.watch_workspaces,
        "interval_seconds": settings.watch_interval_seconds,
        "notify": {"enabled": settings.notify_enabled, "on_pass": settings.notify_on_pass,
                   "departments": settings.notify_departments},
    }
    day_ago = utcnow() - timedelta(hours=24)
    with SessionLocal() as db:
        out["watch_runs"] = [
            {"mode": mode, "status": status, "count": count,
             "last": last.isoformat(timespec="seconds") if last else None}
            for mode, status, count, last in db.execute(
                select(SyncRun.mode, SyncRun.status, func.count(), func.max(SyncRun.created_at))
                .where(SyncRun.mode.in_(("watch", "watch_seed")))
                .group_by(SyncRun.mode, SyncRun.status)).all()]
        out["watch_failures_24h"] = {code or "?": count for code, count in db.execute(
            select(SyncRun.error_code, func.count())
            .where(SyncRun.mode.in_(("watch", "watch_seed")), SyncRun.status == "failed",
                   SyncRun.created_at >= day_ago)
            .group_by(SyncRun.error_code)).all()}
        out["failed_runs_24h"] = [
            {"workspace": run.workspace_name or run.workspace_id or "?", "mode": run.mode,
             "error": run.error_code, "detail": (run.error_detail or "")[:120],
             "at": run.created_at.isoformat(timespec="seconds")}
            for run in db.scalars(select(SyncRun).where(SyncRun.status == "failed", SyncRun.created_at >= day_ago)
                                  .order_by(SyncRun.created_at.desc()).limit(5)).all()]
        out["watch_seed_progress"] = {
            "registered_workspaces": db.scalar(select(func.count()).select_from(Workspace)) or 0,
            "seeded": db.scalar(select(func.count()).select_from(Workspace)
                                .where(Workspace.watch_seeded.is_(True))) or 0}
        latest_audit_ms = db.scalar(select(func.max(FileAuditEvent.gmt_create)))
        out["audit"] = {"enabled": settings.audit_pull_enabled,
                        "latest_event_at": (datetime.fromtimestamp(latest_audit_ms / 1000, tz=timezone.utc)
                                            .isoformat(timespec="seconds") if latest_audit_ms else None)}
        out["mirror"] = {
            "workspaces": db.scalar(select(func.count(func.distinct(Document.workspace_id)))) or 0,
            "documents": db.scalar(select(func.count()).select_from(Document)
                                   .where(Document.is_folder.is_(False), Document.is_deleted.is_(False))) or 0,
            "deleted": db.scalar(select(func.count()).select_from(Document)
                                 .where(Document.is_deleted.is_(True))) or 0,
        }
        out["review_jobs_pending"] = db.scalar(select(func.count()).select_from(ReviewJob)
                                               .where(ReviewJob.status == "pending")) or 0
        out["reviews"] = {
            "total": db.scalar(select(func.count()).select_from(ReviewInstance)) or 0,
            "last_24h": db.scalar(select(func.count()).select_from(ReviewInstance)
                                  .where(ReviewInstance.created_at >= day_ago)) or 0,
            "latest": [{"score": r.ai_score, "verdict": r.verdict, "scope": r.review_scope,
                        "at": r.created_at.isoformat(timespec="seconds")}
                       for r in db.scalars(select(ReviewInstance)
                                           .order_by(ReviewInstance.created_at.desc()).limit(3)).all()],
        }
        out["notifications"] = {
            "by_status": {status: count for status, count in db.execute(
                select(Notification.status, func.count()).group_by(Notification.status)).all()},
            "latest": [{"status": n.status, "error": n.error_code, "title": (n.title or "")[:40],
                        "at": n.created_at.isoformat(timespec="seconds")}
                       for n in db.scalars(select(Notification)
                                           .order_by(Notification.created_at.desc()).limit(5)).all()],
        }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
