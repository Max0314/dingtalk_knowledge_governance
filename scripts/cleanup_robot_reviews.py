"""Remove review noise produced by robot (数字员工) uploads.

Before 2026-08-13 the robot skip only matched raw ids, while bi_center
resolves the digital employee into a regular identity — so robot-synced
documents slipped into the review queue. This deletes their review
instances (plus decisions), their notifications, and cancels their queued
jobs. Reviews of human uploads are untouched.

Dry-run by default; --apply executes.
    docker compose exec -T api python scripts/cleanup_robot_reviews.py
    docker compose exec -T api python scripts/cleanup_robot_reviews.py --apply
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import get_settings
from app.db import Document, Notification, ReviewDecision, ReviewInstance, ReviewJob, SessionLocal, init_db, utcnow
from app.service import is_robot_uploader


def main() -> None:
    apply_changes = "--apply" in sys.argv
    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        reviewed_node_ids = [nid for (nid,) in db.execute(select(ReviewInstance.node_id).distinct()).all()]
        robot_nodes = []
        for node_id in reviewed_node_ids:
            doc = db.get(Document, node_id)
            if doc is not None and is_robot_uploader(settings, doc.uploader_key, doc.uploader_name):
                robot_nodes.append({"node_id": node_id, "name": doc.name, "uploader": doc.uploader_name or doc.uploader_key})
        node_ids = [item["node_id"] for item in robot_nodes]
        instances = db.scalars(select(ReviewInstance).where(ReviewInstance.node_id.in_(node_ids))).all() if node_ids else []
        instance_ids = [i.review_instance_id for i in instances]
        decisions = db.scalars(select(ReviewDecision).where(ReviewDecision.review_instance_id.in_(instance_ids))).all() if instance_ids else []
        notifications = db.scalars(select(Notification).where(Notification.node_id.in_(node_ids))).all() if node_ids else []
        open_jobs = [job for job in (db.scalars(select(ReviewJob).where(ReviewJob.status.in_(("pending", "running")))).all())
                     if (doc := db.get(Document, job.node_id)) is not None
                     and is_robot_uploader(settings, doc.uploader_key, doc.uploader_name)]
        print(json.dumps({
            "mode": "APPLIED" if apply_changes else "DRY-RUN（加 --apply 执行）",
            "robot_documents_reviewed": len(robot_nodes),
            "review_instances_to_delete": len(instances),
            "decisions_to_delete": len(decisions),
            "notifications_to_delete": len(notifications),
            "open_jobs_to_cancel": len(open_jobs),
        }, ensure_ascii=False, indent=1))
        for item in robot_nodes[:50]:
            print(f"[机器人文档] {item['name']}（上传人 {item['uploader']}）")
        if not apply_changes:
            return
        for row in decisions:
            db.delete(row)
        for row in instances:
            db.delete(row)
        for row in notifications:
            db.delete(row)
        for job in open_jobs:
            job.status, job.error_code, job.finished_at = "skipped", "robot_uploader", utcnow()
        db.commit()
        print("done")


if __name__ == "__main__":
    main()
