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
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, Notification, ReviewInstance, utcnow
from .integrations import DingtalkClient, IntegrationError

VERDICT_LABEL = {"pass": "通过", "manual_review": "待人工审核", "return": "退回"}

CN_TZ = timezone(timedelta(hours=8))  # 每日推送限额按业务日（Asia/Shanghai）结算

# 试点尾注：随每条推送发出（含汇总）。
PILOT_FOOTER = '\n\n---\n<font color="#9CA3AF">试点功能 · 不影响文档 · 意见反馈：AI应用研发部-陈鹏列</font>'

GREEN, AMBER, RED = "#16A34A", "#D97706", "#DC2626"


def _score_color(score: float, verdict: str) -> str:
    if verdict == "pass":
        return GREEN
    return AMBER if (score or 0) >= 60 else RED


def _doc_link(base_url: str, node_id: str) -> str:
    from urllib.parse import quote
    return f"{base_url.rstrip('/')}/#/doc/{quote(str(node_id), safe='')}"


def build_message(doc: Document, instance: ReviewInstance, base_url: str = "",
                  mode: str = "", prev_score: float | None = None) -> tuple[str, str]:
    """单条推送：通过 = 正反馈；低分 = 说明语气 + 分析页链接（试点期无退回流程）。
    metadata_only 仅保留历史兼容文案；当前门禁不会为无正文创建新实例，
    enqueue_review_notification 也不会推送历史初检。状态用彩色文字呈现。

    重评降噪矩阵（2026-08-14 拍板）的两种专属文案：``mode="improved"``
    低分修改后达标的一次性正反馈；``mode="drop"`` 同结论但明显下降的提醒。"""
    partial = instance.review_scope != "full_content"
    stage = "初检" if partial else "评审"
    score = f'<font color="{_score_color(instance.ai_score, instance.verdict)}">**{instance.ai_score:.0f} / 100**</font>'
    has_prev = isinstance(prev_score, (int, float))
    if mode == "improved":
        prev_part = f"{prev_score:.0f} 分 → " if has_prev else ""
        lines = [
            "### 文档重评：修改后已改善",
            f"**{doc.name}**",
            "",
            f"{prev_part}{score} · 修改后的文档已达标，感谢维护。",
        ]
        title = f"文档修改后已改善：{doc.name}"[:60]
        return title, "\n".join(lines) + PILOT_FOOTER
    drop_line = ""
    if mode == "drop" and has_prev:
        drop_line = (f'较上次评审下降 <font color="{AMBER}">**{prev_score - (instance.ai_score or 0):.0f} 分**</font>'
                     f'（{prev_score:.0f} → {instance.ai_score:.0f}）。')
    if instance.verdict == "pass":
        lines = [
            f"### 文档{stage}：通过" if mode != "drop" else "### 文档重评：评分下降提醒",
            f"**{doc.name}**",
            "",
            f"{score} · " + ("元数据合规初检通过。" if partial else "文档质量达标，感谢维护。"),
        ]
        if drop_line:
            lines.append(drop_line + "结论仍为通过，供维护参考。")
        if partial:
            lines.append("这是历史元数据初检记录（现已停用），不代表正文质量，也不会自动补评或推送。")
        title = (f"文档{stage}通过：{doc.name}" if mode != "drop" else f"文档评分下降提醒：{doc.name}")[:60]
    else:
        findings = [f.get("message", "") for f in (instance.findings or [])[:3] if isinstance(f, dict)]
        lines = [
            f"### 文档{stage}：低分说明",
            f"**{doc.name}**",
            "",
        ]
        if drop_line:
            lines.append(drop_line)
        lines.append(f"{score}，主要扣分点：")
        lines.extend(f"{index}. {message}" for index, message in enumerate(findings, 1))
        lines.append("")
        closing = "以上仅为质量提示，修改后会自动重新评审。"
        if partial:
            closing = "这是历史元数据初检记录（现已停用），不代表正文质量，也不会自动补评或推送。"
        lines.append(closing)
        if base_url and getattr(doc, "node_id", ""):
            lines.append(f"[查看完整评审分析 →]({_doc_link(base_url, doc.node_id)})")
        title = f"文档{stage}低分说明：{doc.name}"[:60]
    return title, "\n".join(lines) + PILOT_FOOTER


def _prev_instance(db: Session, node_id: str, instance: ReviewInstance) -> ReviewInstance | None:
    """本次实例之前最近的一条评审（降噪矩阵的比较基准）。instance 可能尚未
    flush（created_at 还没落）——条件按可得字段收敛，绝不 AttributeError。"""
    conds = [ReviewInstance.node_id == node_id,
             ReviewInstance.review_instance_id != (getattr(instance, "review_instance_id", "") or "")]
    stamp = getattr(instance, "created_at", None)
    if stamp is not None:
        conds.append(ReviewInstance.created_at <= stamp)
    return db.scalar(select(ReviewInstance).where(*conds)
                     .order_by(ReviewInstance.created_at.desc()).limit(1))


def _naive(moment):
    return moment.replace(tzinfo=None) if moment is not None and moment.tzinfo else moment


def _notify_decision(db: Session, settings: Settings, node_id: str, instance: ReviewInstance,
                     prev: ReviewInstance | None) -> tuple[str, str]:
    """重评降噪矩阵（2026-08-14 拍板）。返回 (decision, mode)：

    - 首评：照旧（pass 且未开 KG_NOTIFY_ON_PASS 时静默不留痕）。
    - 结论翻转必通知：通过→低分（低分说明）、低分→通过（改善文案，
      不受 KG_NOTIFY_ON_PASS 限制——修改的人应得到一次收尾反馈）。
    - 同结论下降 ≥10 分通知提醒；其余波动一律留痕静默。
    - 同一文档每业务日至多 1 条自动重评通知（首评不占额度），超出留痕。"""
    if prev is None:
        if instance.verdict == "pass" and not settings.notify_on_pass:
            return "silent", ""
        return "notify", ""
    prev_pass, cur_pass = prev.verdict == "pass", instance.verdict == "pass"
    delta = (instance.ai_score or 0) - (prev.ai_score or 0)
    if prev_pass and not cur_pass:
        mode = ""
    elif not prev_pass and cur_pass:
        mode = "improved"
    elif delta <= -10:
        mode = "drop"
    else:
        return "suppressed_minor_change", ""
    day_start = (datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc).replace(tzinfo=None))
    sent_today = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.node_id == node_id, Notification.status.in_(("pending", "sent")),
        Notification.created_at >= day_start)) or 0
    earliest = db.scalar(select(func.min(ReviewInstance.created_at))
                         .where(ReviewInstance.node_id == node_id))
    allowance = 2 if earliest is not None and _naive(earliest) >= day_start else 1
    if sent_today >= allowance:
        return "suppressed_daily_cap", ""
    return "notify", mode


def enqueue_review_notification(db: Session, settings: Settings, doc: Document, instance: ReviewInstance) -> Notification | None:
    """Queue a push for a finished review. Caller commits."""
    if not settings.notify_enabled:
        return None
    if getattr(instance, "review_scope", "") != "full_content":
        # 2026-08-17 拍板：拿不到正文不推送——初检推送整体下线（手动重评产生
        # 的 metadata_only 实例同样只留痕不打扰）。
        notification = Notification(node_id=doc.node_id,
                                    review_instance_id=getattr(instance, "review_instance_id", "") or "",
                                    status="skipped", error_code="no_content_not_pushed")
        db.add(notification)
        return notification
    prev = _prev_instance(db, doc.node_id, instance)
    decision, mode = _notify_decision(db, settings, doc.node_id, instance, prev)
    if decision == "silent":
        return None
    if decision != "notify":
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code=decision)
        db.add(notification)
        return notification
    allowed = {token.strip() for token in settings.notify_workspaces.split(",") if token.strip()}
    if allowed and doc.workspace_id not in allowed:
        # Org-rollout guardrail: reviews outside the allowlist stay silent but
        # auditable — nobody gets robot-spammed by a workspace we never onboarded.
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code="workspace_not_allowlisted")
        db.add(notification)
        return notification
    allowed_depts = {token.strip() for token in settings.notify_departments.split(",") if token.strip()}
    if allowed_depts and (getattr(doc, "department_name", "") or "") not in allowed_depts:
        # 按上传人部门灰度：名单外（含"未映射"）只留痕不打扰，评审照常记录。
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code="department_not_allowlisted")
        db.add(notification)
        return notification
    if not doc.uploader_key:
        notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                    status="skipped", error_code="uploader_unknown")
        db.add(notification)
        return notification
    title, body = build_message(doc, instance, settings.public_base_url,
                                mode=mode, prev_score=(prev.ai_score if prev is not None else None))
    notification = Notification(node_id=doc.node_id, review_instance_id=instance.review_instance_id,
                                target_union_id=doc.uploader_key, title=title, body=body)
    db.add(notification)
    return notification


def _age_seconds(now, moment) -> float:
    return (now.replace(tzinfo=None) - moment.replace(tzinfo=None)).total_seconds()


def digest_message(entries: list[dict], base_url: str = "") -> tuple[str, str]:
    """One message for a burst: entries = [{"name", "score", "verdict"}]
    （score/verdict 可缺省）。独立于 DB，样例脚本可原样复用生产文案。"""
    passed = sum(1 for e in entries if e.get("verdict") == "pass")
    low = sum(1 for e in entries if e.get("verdict") not in (None, "", "pass"))
    parts = ([f"通过 {passed}"] if passed else []) + ([f"低分 {low}"] if low else [])
    summary = f"（{' · '.join(parts)}）" if parts else ""
    lines = [f"### 文档评审汇总：{len(entries)} 份{summary}", ""]
    for entry in entries[:10]:
        name, score, verdict = entry.get("name", "未知文档"), entry.get("score"), entry.get("verdict")
        if isinstance(score, (int, float)):
            score_part = f' — <font color="{_score_color(score, verdict)}">**{score:.0f} 分**</font>'
        else:
            score_part = ""
        stage_part = " · 历史初检（已停用）" if entry.get("scope") == "metadata_only" else ""
        lines.append(f"- **{name}**{score_part}{stage_part}")
    if len(entries) > 10:
        lines.append(f"- ……其余 {len(entries) - 10} 份")
    if low:
        lines += ["", "低分仅为质量提示，修改后会自动重新评审。"]
        if base_url:
            lines.append(f"[查看评审分析 →]({base_url.rstrip('/')}/#/reviews)")
    return f"文档评审汇总：{len(entries)} 份{summary}"[:60], "\n".join(lines) + PILOT_FOOTER


def _digest_message(db: Session, rows: list[Notification], base_url: str = "") -> tuple[str, str]:
    entries = []
    for row in rows:
        instance = db.get(ReviewInstance, row.review_instance_id) if row.review_instance_id else None
        entries.append({"name": row.title.split("：", 1)[-1],
                        "score": instance.ai_score if instance else None,
                        "verdict": instance.verdict if instance else "",
                        "scope": instance.review_scope if instance else ""})
    return digest_message(entries, base_url)


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
                title, body = _digest_message(db, group, settings.public_base_url)
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
