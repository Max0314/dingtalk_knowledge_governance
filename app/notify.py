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
    allowed = {token.strip() for token in settings.notify_workspaces.split(",") if token.strip()}
    if allowed and doc.workspace_id not in allowed:
        # Org-rollout guardrail: reviews outside the allowlist stay silent but
        # auditable — nobody gets robot-spammed by a workspace we never onboarded.
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code="workspace_not_allowlisted")
        db.add(notification)
        return notification
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


def _age_seconds(now, moment) -> float:
    return (now.replace(tzinfo=None) - moment.replace(tzinfo=None)).total_seconds()


def _digest_message(db: Session, rows: list[Notification]) -> tuple[str, str]:
    """One message for a burst: counts plus a one-line summary per document."""
    from .db import ReviewInstance

    lines = [f"### 知识库文档评审汇总：{len(rows)} 份待处理", ""]
    for row in rows[:10]:
        instance = db.get(ReviewInstance, row.review_instance_id) if row.review_instance_id else None
        name = row.title.split("：", 1)[-1]
        if instance:
            lines.append(f"- **{name}** — {instance.ai_score:.0f} 分 · {VERDICT_LABEL.get(instance.verdict, instance.verdict)}")
        else:
            lines.append(f"- **{name}**")
    if len(rows) > 10:
        lines.append(f"- ……其余 {len(rows) - 10} 份见治理看板")
    lines += ["", "请在钉钉知识库中修改原文档；修改会被自动发现并重新评审，历史评分保留。"]
    return f"文档评审汇总：{len(rows)} 份待处理", "\n".join(lines)


def process_pending_notifications(db: Session, settings: Settings, batch: int = 100) -> int:
    """Digest-aware pump: pushes wait out a quiet window per recipient so a
    burst of uploads becomes one summary message instead of a message storm.
    Returns how many rows were attempted (sent or failed)."""
    rows = db.scalars(select(Notification).where(Notification.status == "pending")
                      .order_by(Notification.created_at).limit(batch)).all()
    if not rows:
        return 0
    client = DingtalkClient(settings)
    now = utcnow()
    window = max(0, settings.notify_digest_window_seconds)
    max_delay = max(window, settings.notify_digest_max_delay_seconds)
    groups: dict[str, list[Notification]] = {}
    for row in rows:
        key = settings.notify_override_user_id or row.target_union_id or "?"
        groups.setdefault(key, []).append(row)
    attempted = 0
    for key, group in groups.items():
        if not settings.notify_enabled:
            for row in group:
                row.status, row.error_code = "skipped", "notify_disabled"
            attempted += len(group)
            continue
        newest_age = min(_age_seconds(now, row.created_at) for row in group)
        oldest_age = max(_age_seconds(now, row.created_at) for row in group)
        if window and newest_age < window and oldest_age < max_delay:
            continue  # recipient still in a burst — keep accumulating
        try:
            if settings.notify_override_user_id:
                user_id = settings.notify_override_user_id
                origin = "、".join(sorted({row.target_union_id for row in group if row.target_union_id})) or "未知"
                prefix = f"> 试点观察模式：本应推送给上传人 `{origin}`\n\n"
            else:
                sample = group[0]
                user_id = sample.target_user_id or (sample.target_union_id if sample.target_union_id.isdigit()
                                                    else asyncio.run(client.resolve_user_id(sample.target_union_id)))
                prefix = ""
            if not user_id:
                for row in group:
                    row.status, row.error_code = "failed", "user_id_not_resolved"
                attempted += len(group)
                continue
            if len(group) == 1:
                title, body = group[0].title, group[0].body
            else:
                title, body = _digest_message(db, group)
            asyncio.run(client.send_robot_markdown([user_id], title, prefix + body))
            for row in group:
                row.target_user_id = user_id
                row.status, row.sent_at, row.error_code = "sent", utcnow(), ""
        except IntegrationError as exc:
            for row in group:
                row.status, row.error_code = "failed", exc.code
        except Exception:
            for row in group:
                row.status, row.error_code = "failed", "notify_execution_failed"
        attempted += len(group)
    db.commit()
    return attempted
