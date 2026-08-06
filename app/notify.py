"""Review-result push over the DingTalk robot, with an auditable outbox.

Flow: a review whose verdict is not `pass` enqueues a Notification row; the
worker drains pending rows, resolves the uploader's userId from their unionId
when needed, and sends a one-to-one robot markdown message. Failures keep the
row with an error code instead of silently dropping — the diagnostics view
lists recent rows so a broken permission is visible, not guessed.

Nothing here stores document body content; messages carry name, score and
finding summaries only.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, Notification, ReviewInstance, utcnow
from .integrations import DingtalkClient, IntegrationError

VERDICT_LABEL = {"pass": "通过", "manual_review": "待人工审核", "return": "退回"}


def build_message(doc: Document, instance: ReviewInstance) -> tuple[str, str]:
    findings = [f.get("message", "") for f in (instance.findings or [])[:3] if isinstance(f, dict)]
    lines = [
        f"### 知识库文档评审：{VERDICT_LABEL.get(instance.verdict, instance.verdict)}",
        f"- 文档：**{doc.name}**",
        f"- AI 评分：**{instance.ai_score:.0f} / 100**（{'完整正文' if instance.review_scope == 'full_content' else '元数据合规'}口径，规则 {instance.rule_version}）",
    ]
    if findings:
        lines.append("- 主要问题：")
        lines.extend(f"  {index}. {message}" for index, message in enumerate(findings, 1))
    lines.append("")
    lines.append("请在钉钉知识库中修改原文档；修改会被自动发现并重新评审，历史评分保留。")
    return f"文档评审{VERDICT_LABEL.get(instance.verdict, '')}：{doc.name}"[:60], "\n".join(lines)


def enqueue_review_notification(db: Session, settings: Settings, doc: Document, instance: ReviewInstance) -> Notification | None:
    """Queue a push for a non-pass review. Caller commits."""
    if not settings.notify_enabled or instance.verdict == "pass":
        return None
    if not doc.uploader_key:
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code="uploader_unknown")
        db.add(notification)
        return notification
    title, body = build_message(doc, instance)
    notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                target_union_id=doc.uploader_key, title=title, body=body)
    db.add(notification)
    return notification


def process_pending_notifications(db: Session, settings: Settings, batch: int = 5) -> int:
    """Send up to `batch` pending rows. Returns how many were attempted."""
    rows = db.scalars(select(Notification).where(Notification.status == "pending")
                      .order_by(Notification.created_at).limit(batch)).all()
    if not rows:
        return 0
    client = DingtalkClient(settings)
    for row in rows:
        if not settings.notify_enabled:
            row.status, row.error_code = "skipped", "notify_disabled"
            continue
        try:
            user_id = row.target_user_id or asyncio.run(client.resolve_user_id(row.target_union_id))
            if not user_id:
                row.status, row.error_code = "failed", "user_id_not_resolved"
                continue
            row.target_user_id = user_id
            asyncio.run(client.send_robot_markdown([user_id], row.title, row.body))
            row.status, row.sent_at, row.error_code = "sent", utcnow(), ""
        except IntegrationError as exc:
            row.status, row.error_code = "failed", exc.code
        except Exception:
            row.status, row.error_code = "failed", "notify_execution_failed"
    db.commit()
    return len(rows)
