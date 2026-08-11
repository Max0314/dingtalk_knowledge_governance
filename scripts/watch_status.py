"""Print watcher state as JSON: target resolution, recent watch runs, and
per-watched-workspace document/job/review counts. Runs inside the api or
worker container (uses the container env for credentials); read-only."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.config import get_settings
from app.db import Document, Notification, ReviewInstance, ReviewJob, SessionLocal, SyncRun, init_db
from app.service import resolve_watch_targets


def main() -> None:
    settings = get_settings()
    init_db()
    out: dict = {"watch_workspaces": settings.watch_workspaces,
                 "interval_seconds": settings.watch_interval_seconds}
    try:
        out["resolution"] = asyncio.run(resolve_watch_targets(settings, force=True))
    except Exception as exc:  # keep the DB half useful even when DingTalk is down
        out["resolution_error"] = str(exc)[:300]
    with SessionLocal() as db:
        out["run_totals"] = [
            {"mode": mode, "status": status, "count": count,
             "first": first.isoformat() if first else None, "last": last.isoformat() if last else None}
            for mode, status, count, first, last in db.execute(
                select(SyncRun.mode, SyncRun.status, func.count(), func.min(SyncRun.created_at),
                       func.max(SyncRun.created_at))
                .where(SyncRun.mode.in_(("watch", "watch_seed")))
                .group_by(SyncRun.mode, SyncRun.status)).all()]
        out["run_errors"] = {code: count for code, count in db.execute(
            select(SyncRun.error_code, func.count()).where(SyncRun.mode.in_(("watch", "watch_seed")),
                                                           SyncRun.status == "failed")
            .group_by(SyncRun.error_code)).all()}
        out["notifications"] = {
            "by_status": {status: count for status, count in db.execute(
                select(Notification.status, func.count()).group_by(Notification.status)).all()},
            "recent": [{"node_id": row.node_id, "status": row.status, "error_code": row.error_code,
                        "title": row.title, "created_at": row.created_at.isoformat()}
                       for row in db.scalars(select(Notification).order_by(Notification.created_at.desc())
                                             .limit(5)).all()]}
        out["recent_watch_runs"] = [
            {"run_id": run.run_id, "mode": run.mode, "status": run.status,
             "documents_seen": run.documents_seen, "documents_new": run.documents_new,
             "documents_changed": run.documents_changed, "error_code": run.error_code,
             "created_at": run.created_at.isoformat(),
             "finished_at": run.finished_at.isoformat() if run.finished_at else None}
            for run in db.scalars(select(SyncRun).where(SyncRun.mode.in_(("watch", "watch_seed")))
                                  .order_by(SyncRun.created_at.desc()).limit(10)).all()]
        out["workspaces"] = []
        for target in (out.get("resolution") or {}).get("resolved", []):
            ws_id = target["workspace_id"]
            docs = db.scalar(select(func.count()).select_from(Document)
                             .where(Document.workspace_id == ws_id, Document.is_deleted.is_(False))) or 0
            # Folder count ~= DingTalk list calls per walk cycle (cost driver).
            folders = db.scalar(select(func.count()).select_from(Document)
                                .where(Document.workspace_id == ws_id, Document.is_folder.is_(True),
                                       Document.is_deleted.is_(False))) or 0
            deleted = db.scalar(select(func.count()).select_from(Document)
                                .where(Document.workspace_id == ws_id, Document.is_deleted.is_(True))) or 0
            node_ids = select(Document.node_id).where(Document.workspace_id == ws_id)
            jobs = db.scalar(select(func.count()).select_from(ReviewJob).where(ReviewJob.node_id.in_(node_ids))) or 0
            reviews = db.scalar(select(func.count()).select_from(ReviewInstance)
                                .where(ReviewInstance.node_id.in_(node_ids))) or 0
            reviews_by_trigger = {trigger: count for trigger, count in db.execute(
                select(ReviewInstance.trigger, func.count()).where(ReviewInstance.node_id.in_(node_ids))
                .group_by(ReviewInstance.trigger)).all()}
            latest = db.scalars(select(ReviewInstance).where(ReviewInstance.node_id.in_(node_ids))
                                .order_by(ReviewInstance.created_at.desc()).limit(5)).all()
            out["workspaces"].append({
                "workspace_id": ws_id, "name": target["name"], "documents": docs, "folders": folders, "deleted": deleted,
                "review_jobs": jobs, "review_instances": reviews, "reviews_by_trigger": reviews_by_trigger,
                "latest_reviews": [{"node_id": r.node_id, "score": r.ai_score, "verdict": r.verdict,
                                    "scope": r.review_scope, "trigger": r.trigger,
                                    "created_at": r.created_at.isoformat()} for r in latest]})
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
