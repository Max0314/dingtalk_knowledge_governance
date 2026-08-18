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
from app.db import (AuditState, BridgeWalk, Document, FileAuditEvent, Notification, ReviewInstance, ReviewJob,
                    SessionLocal, SyncRun, WatchPlan, Workspace, init_db, utcnow)
from app.fileclass import review_classes
from app.service import current_scan_due


def _storage_key_review_classes(configured: str) -> set[str]:
    """Reviewable classes whose body adapter requires a numeric storage key."""
    return review_classes(configured) - {"native_doc"}


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
                                .where(Workspace.watch_seeded.is_(True))) or 0,
            # 已删除/失权的库（整轮缺席或 404 自动标记）：不计入补种与计划
            "inactive": db.scalar(select(func.count()).select_from(Workspace)
                                  .where(Workspace.is_active.is_(False))) or 0}
        plan = db.get(WatchPlan, 1)
        out["scan_plan"] = {
            "due": current_scan_due(settings),
            "completed_for": plan.completed_for if plan else "",
            "mode": "seeding" if (db.scalar(select(func.count()).select_from(Workspace)
                                            .where(Workspace.watch_seeded.is_(False),
                                                   Workspace.is_active.is_(True))) or 0)
                    else ("idle" if plan and plan.completed_for == current_scan_due(settings) else "scanning"),
        }
        week_ago = utcnow() - timedelta(days=7)
        from sqlalchemy import or_ as sa_or

        # 无键新增文档：只统计"应当有键"的对象——上线时刻之后创建/修改、
        # 可评审上传文件、非机器人。原生 .adoc 按设计用 node id 导出正文，
        # 永远没有数字下载键，不能计入欠账。
        no_key_conds = [Document.is_folder.is_(False), Document.is_deleted.is_(False),
                        Document.storage_dentry_id == "", Document.discovered_at >= week_ago,
                        Document.file_class.in_(sorted(_storage_key_review_classes(settings.review_classes)))]
        for prefix in (p.strip() for p in settings.robot_name_prefixes.split(",") if p.strip()):
            no_key_conds.append(~Document.uploader_name.like(f"{prefix}%"))
        robot_ids = [t.strip() for t in settings.robot_user_ids.split(",") if t.strip()]
        if robot_ids:
            no_key_conds.append(Document.uploader_key.not_in(robot_ids))
        if settings.review_since:
            no_key_conds.append(sa_or(Document.source_created_at >= settings.review_since,
                                      Document.source_updated_at >= settings.review_since))
        out["bridge"] = {
            "pending_retry": db.scalar(select(func.count()).select_from(FileAuditEvent)
                                       .where(FileAuditEvent.processed.is_(False))) or 0,
            # Plan A repair cutover: retained audit evidence that intentionally
            # did not replay into node matching, review jobs, or notifications.
            "pre_cutover_not_reviewed": db.scalar(select(func.count()).select_from(FileAuditEvent)
                                                   .where(FileAuditEvent.resolution == "pre_cutover_not_reviewed")) or 0,
            "dead_letter_total": db.scalar(select(func.count()).select_from(FileAuditEvent)
                                           .where(FileAuditEvent.resolution.like("dead_letter%"))) or 0,
            "walk_queue": db.scalar(select(func.count()).select_from(BridgeWalk)) or 0,
            "reviewable_new_docs_without_key_7d": db.scalar(
                select(func.count()).select_from(Document).where(*no_key_conds)) or 0,
            # 修改合并窗内待收割的脏文档（30 分钟无新修改后自动评审）
            "merge_window_pending": db.scalar(
                select(func.count()).select_from(Document)
                .where(Document.review_due_at.is_not(None))) or 0,
            # 白名单外的未知动作（终态可观测）：出现即评估是否扩名单
            "unknown_actions": db.scalar(
                select(func.count()).select_from(FileAuditEvent)
                .where(FileAuditEvent.resolution == "ignored_unknown_action")) or 0,
        }
        if settings.review_since:
            # 审计漏捕嫌疑：上线后创建/修改、可评审、非机器人，却从未有过
            # 评审实例也没有排队任务——按 2026-08-14 决策只观测不补评。
            robot_conds = []
            for prefix in (p.strip() for p in settings.robot_name_prefixes.split(",") if p.strip()):
                robot_conds.append(~Document.uploader_name.like(f"{prefix}%"))
            if robot_ids:
                robot_conds.append(Document.uploader_key.not_in(robot_ids))
            out["bridge"]["suspected_audit_missed"] = db.scalar(
                select(func.count()).select_from(Document).where(
                    Document.is_folder.is_(False), Document.is_deleted.is_(False),
                    Document.file_class.in_(sorted(review_classes(settings.review_classes))),
                    sa_or(Document.source_created_at >= settings.review_since,
                          Document.source_updated_at >= settings.review_since),
                    Document.node_id.not_in(select(ReviewInstance.node_id).distinct()),
                    Document.node_id.not_in(select(ReviewJob.node_id)
                                            .where(ReviewJob.status.in_(("pending", "running")))),
                    *robot_conds)) or 0
        latest_audit_ms = db.scalar(select(func.max(FileAuditEvent.gmt_create)))
        audit_state = db.scalars(select(AuditState)).first()
        out["audit"] = {
            "enabled": settings.audit_pull_enabled,
            # 拉取循环自身的新鲜度（没有新写操作时事件时间不会动，须看这里）
            "last_run_at": (audit_state.last_run_at.isoformat(timespec="seconds")
                            if audit_state and audit_state.last_run_at else None),
            "last_rows": audit_state.last_rows if audit_state else None,
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
            # 拿不到正文而放弃的评审（2026-08-17 拍板：不评不推只留痕）
            "skipped_no_content_24h": db.scalar(
                select(func.count()).select_from(ReviewJob)
                .where(ReviewJob.status == "skipped",
                       ReviewJob.error_code.like("content_unavailable%"),
                       ReviewJob.finished_at >= day_ago)) or 0,
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
