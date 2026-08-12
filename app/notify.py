"""Review-result push over the DingTalk robot, with an auditable outbox.

Flow: a finished review enqueues a Notification row (non-pass always; pass
verdicts only when KG_NOTIFY_ON_PASS is on); the worker drains pending rows,
resolves the uploader's userId from their unionId when needed, and sends a
one-to-one robot markdown message. Failures keep the row with an error code
instead of silently dropping — the diagnostics view lists recent rows so a
broken permission is visible, not guessed.

试点口径（2026-08-12 用户拍板）：低分不走退回流程，推送只做"低分说明"，
所有消息带试点尾注，避免"文档被退回/删除"的歧义。

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

# 试点尾注：随每条推送发出（含汇总），措辞为用户指定原文。
PILOT_FOOTER = ('\n\n---\n<font color="#9CA3AF">此功能由AI应用研发部-陈鹏列开发，'
                '当前试点阶段，不会对文档产生影响，如有使用意见请及时反馈。</font>')


def build_message(doc: Document, instance: ReviewInstance) -> tuple[str, str]:
    """单条推送：通过 = 正反馈；低分 = 说明语气（试点期无退回流程）。"""
    scope = '完整正文' if instance.review_scope == 'full_content' else '元数据合规'
    if instance.verdict == "pass":
        lines = [
            "### 知识库文档评审：通过 ✅",
            f"- 文档：**{doc.name}**",
            f"- AI 评分：**{instance.ai_score:.0f} / 100**（{scope}口径，规则 {instance.rule_version}）",
            "",
            "文档质量达标，感谢维护。后续修改会被自动发现并重新评审。",
        ]
        title = f"文档评审通过：{doc.name}"[:60]
    else:
        findings = [f.get("message", "") for f in (instance.findings or [])[:3] if isinstance(f, dict)]
        lines = [
            "### 知识库文档评审：低分说明 ⚠️",
            f"- 文档：**{doc.name}**",
            f"- AI 评分：**{instance.ai_score:.0f} / 100**（{scope}口径，规则 {instance.rule_version}）",
        ]
        if findings:
            lines.append("- 主要扣分点：")
            lines.extend(f"  {index}. {message}" for index, message in enumerate(findings, 1))
        lines.append("")
        lines.append("以上仅为质量提示，不影响文档本身；在钉钉知识库中修改原文档会被自动发现并重新评审，历史评分保留。")
        title = f"文档评审低分说明：{doc.name}"[:60]
    return title, "\n".join(lines) + PILOT_FOOTER


def enqueue_review_notification(db: Session, settings: Settings, doc: Document, instance: ReviewInstance) -> Notification | None:
    """Queue a push for a finished review. Caller commits."""
    if not settings.notify_enabled:
        return None
    if instance.verdict == "pass" and not settings.notify_on_pass:
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


def digest_message(entries: list[dict]) -> tuple[str, str]:
    """One message for a burst: entries = [{"name", "score", "verdict"}]
    （score/verdict 可缺省）。独立于 DB，样例脚本可原样复用生产文案。"""
    passed = sum(1 for e in entries if e.get("verdict") == "pass")
    low = sum(1 for e in entries if e.get("verdict") not in (None, "", "pass"))
    parts = ([f"通过 {passed} 份"] if passed else []) + ([f"低分说明 {low} 份"] if low else [])
    summary = f"（{' · '.join(parts)}）" if parts else ""
    lines = [f"### 知识库文档评审汇总：{len(entries)} 份{summary}", ""]
    for entry in entries[:10]:
        name, score, verdict = entry.get("name", "未知文档"), entry.get("score"), entry.get("verdict")
        badge = " · ✅ 通过" if verdict == "pass" else (" · ⚠️ 低分" if verdict else "")
        score_part = f" — {score:.0f} 分" if isinstance(score, (int, float)) else ""
        lines.append(f"- **{name}**{score_part}{badge}")
    if len(entries) > 10:
        lines.append(f"- ……其余 {len(entries) - 10} 份见治理看板")
    if low:
        lines += ["", "低分仅为质量提示，不影响文档本身；修改原文档会被自动发现并重新评审，历史评分保留。"]
    return f"文档评审汇总：{len(entries)} 份{summary}"[:60], "\n".join(lines) + PILOT_FOOTER


def _digest_message(db: Session, rows: list[Notification]) -> tuple[str, str]:
    entries = []
    for row in rows:
        instance = db.get(ReviewInstance, row.review_instance_id) if row.review_instance_id else None
        entries.append({"name": row.title.split("：", 1)[-1],
                        "score": instance.ai_score if instance else None,
                        "verdict": instance.verdict if instance else ""})
    return digest_message(entries)


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
