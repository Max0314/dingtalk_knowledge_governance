"""Read-only audit-trail probe: cursor status, today's write-event breakdown by
module/action, wiki-module samples (do knowledge-base operations appear, and
with which identifiers?), and an optional resource-name grep.

Usage: python scripts/audit_probe.py [resource-substring]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.audit_bridge import bridge_status
from app.audit_pull import audit_status
from app.db import FileAuditEvent, SessionLocal, SyncRun, init_db

CST = timezone(timedelta(hours=8))


def event_dict(event: FileAuditEvent) -> dict:
    return {"time": datetime.fromtimestamp(event.gmt_create / 1000, CST).strftime("%m-%d %H:%M:%S"),
            "operator": event.operator_name, "action": event.action, "action_view": event.action_view,
            "module": event.module_view, "resource": event.resource[:80], "extension": event.extension,
            "target_space_id": event.target_space_id, "platform": event.platform}


def main() -> None:
    grep = sys.argv[1] if len(sys.argv) > 1 else ""
    init_db()
    with SessionLocal() as db:
        out: dict = {"status": audit_status(db), "bridge": bridge_status(db)}
        out["recent_bridge_runs"] = [
            {"run_id": run.run_id, "mode": run.mode, "status": run.status,
             "documents_seen": run.documents_seen, "documents_new": run.documents_new,
             "documents_changed": run.documents_changed, "error_code": run.error_code,
             "created_at": run.created_at.isoformat()}
            for run in db.scalars(select(SyncRun).where(SyncRun.mode.in_(("bridge", "bridge_seed")))
                                  .order_by(SyncRun.created_at.desc()).limit(5)).all()]
        day_start_ms = int(datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        out["writes_today_by_module_action"] = [
            {"module": module, "action_view": action_view, "count": count}
            for module, action_view, count in db.execute(
                select(FileAuditEvent.module_view, FileAuditEvent.action_view, func.count())
                .where(FileAuditEvent.gmt_create >= day_start_ms)
                .group_by(FileAuditEvent.module_view, FileAuditEvent.action_view)
                .order_by(func.count().desc())).all()]
        out["wiki_module_samples"] = [event_dict(event) for event in db.scalars(
            select(FileAuditEvent).where(FileAuditEvent.module_view.contains("知识库"))
            .order_by(FileAuditEvent.gmt_create.desc()).limit(10)).all()]
        if grep:
            out["resource_grep"] = [event_dict(event) for event in db.scalars(
                select(FileAuditEvent).where(FileAuditEvent.resource.contains(grep))
                .order_by(FileAuditEvent.gmt_create.desc()).limit(10)).all()]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
