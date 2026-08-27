"""Log-based CDC over the exclusive DingTalk file-audit trail (pillar B).

Every cycle pulls the org-wide operation log with a lookback overlap (the
feed lags 80s–20min, so the window re-reads and dedupes by bizId), then:

  * write-type operations land as FileAuditEvent rows — the change feed that
    downstream mirroring/review will consume;
  * read-type operations (preview/download) only bump a daily aggregate —
    at ~100k reads/day org-wide, raw rows would be noise;
  * a silence alarm notifies the operator when business hours pass with no
    events at all — the audit switch is org-level and one admin toggle could
    silently blind us otherwise.

Cursor state persists in audit_state so restarts resume without gaps.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import AuditDailyAgg, AuditState, FileAuditEvent, SessionLocal, _audit_event_key, utcnow
from .integrations import DingtalkClient, IntegrationError

LOOKBACK_MS = 40 * 60 * 1000
PAGE_SIZE = 500
MAX_PAGES_PER_CYCLE = 40
READ_MARKERS = ("预览", "下载", "导出")
CST = timezone(timedelta(hours=8))


def _filename_extension(resource: object, reported: object = "") -> str:
    """Return the filename suffix when available.

    ``resourceExtension`` in the audit trail is advisory: production has
    reported ``adoc`` for uploaded .xlsx/.docx files. The filename is used
    only to canonicalize the stored hint; the bridge still requires an exact
    filename plus temporal node corroboration before it can act on an event.
    """
    name = str(resource or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].strip().lower()
        if suffix and len(suffix) <= 32:
            return suffix
    return str(reported or "").strip().lower()[:32]


def _is_read(action_view: str) -> bool:
    return any(marker in (action_view or "") for marker in READ_MARKERS)


def _state(db: Session) -> AuditState:
    state = db.get(AuditState, 1)
    if not state:
        state = AuditState(id=1)
        db.add(state)
        db.flush()
    return state


async def _fetch_pages(settings: Settings, start_ms: int, end_ms: int) -> list[dict]:
    client = DingtalkClient(settings)
    token = await client._token_value()
    rows: list[dict] = []
    cursor_g = cursor_b = None
    async with httpx.AsyncClient(timeout=25) as http:
        for _ in range(MAX_PAGES_PER_CYCLE):
            params = {"startDate": start_ms, "endDate": end_ms, "pageSize": PAGE_SIZE}
            if cursor_g:
                params["nextGmtCreate"], params["nextBizId"] = cursor_g, cursor_b
            response = await http.get("https://api.dingtalk.com/v1.0/exclusive/fileAuditLogs",
                                      params=params, headers={"x-acs-dingtalk-access-token": token})
            if response.status_code != 200:
                raise IntegrationError("audit_pull_failed", f"审计接口调用失败（HTTP {response.status_code}）。", response.status_code)
            items = response.json().get("list", [])
            rows.extend(items)
            if len(items) < PAGE_SIZE:
                break
            cursor_g, cursor_b = items[-1]["gmtCreate"], items[-1]["bizId"]
    return rows


def _ingest(db: Session, rows: list[dict]) -> dict:
    stored = reads = dupes = 0
    known: dict[str, FileAuditEvent] = {}
    if rows:
        # Normalize exactly as the insert path below.  DingTalk sometimes
        # returns numeric ``bizId=0`` for membership operations; passing that
        # raw value to _audit_event_key treats it as falsey, while the insert
        # path stores/hashes the string "0".  The mismatch bypassed overlap
        # dedupe and made a pre-existing event fail the whole transaction.
        keys = [_audit_event_key(str(r.get("bizId")), int(r.get("gmtCreate") or 0),
                                 r.get("action"), str(r.get("actionView") or ""))
                for r in rows]
        for chunk_start in range(0, len(keys), 500):
            chunk = keys[chunk_start:chunk_start + 500]
            for event in db.scalars(select(FileAuditEvent).where(FileAuditEvent.event_key.in_(chunk))).all():
                known[event.event_key] = event
    agg: dict[tuple[str, str, str], int] = {}
    max_gmt = 0
    for row in rows:
        gmt = int(row.get("gmtCreate") or 0)
        max_gmt = max(max_gmt, gmt)
        action_view = str(row.get("actionView") or "")
        day = datetime.fromtimestamp(gmt / 1000, CST).strftime("%Y-%m-%d") if gmt else ""
        module = str(row.get("operateModuleView") or "")
        if _is_read(action_view):
            reads += 1
            if day:
                key = (day, module, action_view)
                agg[key] = agg.get(key, 0) + 1
            continue
        biz_id = str(row.get("bizId"))
        event_key = _audit_event_key(biz_id, gmt, row.get("action"), action_view)
        if event_key in known:
            # The schema may have gained fields after this immutable event was
            # first stored. The overlap window doubles as safe enrichment.
            existing = known[event_key]
            workspace_name = str(row.get("workSpaceName") or "")[:255]
            if not existing.workspace_name and workspace_name:
                existing.workspace_name = workspace_name
            dupes += 1
            continue
        resource = str(row.get("resource") or "")[:512]
        event = FileAuditEvent(
            event_key=event_key, biz_id=biz_id, gmt_create=gmt,
            operator_user_id=str(row.get("userId") or ""), operator_name=str(row.get("operatorName") or "")[:128],
            action=str(row.get("action") or ""), action_view=action_view[:64],
            module_view=module[:64], resource=resource,
            extension=_filename_extension(resource, row.get("resourceExtension")),
            size=int(row.get("resourceSize") or 0), target_space_id=str(row.get("targetSpaceId") or "")[:64],
            workspace_name=str(row.get("workSpaceName") or "")[:255],
            ip_address=str(row.get("ipAddress") or "")[:64], platform=str(row.get("platformView") or "")[:32],
        )
        db.add(event)
        known[event_key] = event
        stored += 1
    for (day, module, action_view), count in agg.items():
        existing = db.scalar(select(AuditDailyAgg).where(AuditDailyAgg.day == day, AuditDailyAgg.module_view == module,
                                                         AuditDailyAgg.action_view == action_view))
        if existing:
            existing.count += count
        else:
            db.add(AuditDailyAgg(day=day, module_view=module, action_view=action_view, count=count))
    return {"stored": stored, "reads": reads, "dupes": dupes, "max_gmt": max_gmt}


def _maybe_alarm(db: Session, settings: Settings, state: AuditState) -> None:
    now_cst = datetime.now(CST)
    if now_cst.weekday() >= 5 or not (9 <= now_cst.hour < 18):
        return
    last_ms = state.last_gmt_create or 0
    silent_minutes = (time.time() * 1000 - last_ms) / 60000 if last_ms else None
    if silent_minutes is None or silent_minutes < 30:
        return
    alerted = state.silence_alerted_at
    if alerted and (utcnow() - alerted.replace(tzinfo=alerted.tzinfo or timezone.utc)).total_seconds() < 4 * 3600:
        return
    target = settings.audit_alert_user_id
    if not target:
        return
    # Reserve the four-hour slot before crossing the external side-effect
    # boundary.  If sending fails, release it so the next pull can retry.
    # run_audit_pull has already committed audit ingestion at this point.
    state.silence_alerted_at = utcnow()
    db.commit()
    try:
        asyncio.run(DingtalkClient(settings).send_robot_markdown(
            [target], "审计流水静默告警",
            f"### 审计流水静默告警\n工作时段内已连续 **{int(silent_minutes)} 分钟** 未收到任何文件操作记录。\n\n"
            "请检查专属钉钉管理后台的文件审计开关是否被关闭（专属安全 → 专属审计平台）。"))
    except Exception:
        state.silence_alerted_at = None
        db.commit()


def run_audit_pull(db: Session, settings: Settings) -> dict:
    """One CDC cycle. Returns a summary for the worker log."""
    state = _state(db)
    now_ms = int(time.time() * 1000)
    start_ms = max((state.last_gmt_create or 0) - LOOKBACK_MS, now_ms - 24 * 3600 * 1000)
    if not state.last_gmt_create:
        start_ms = now_ms - LOOKBACK_MS  # first run: start shallow, no historic replay
    rows = asyncio.run(_fetch_pages(settings, start_ms, now_ms))
    summary = _ingest(db, rows)
    if summary["max_gmt"] > (state.last_gmt_create or 0):
        state.last_gmt_create = summary["max_gmt"]
    state.last_run_at = utcnow()
    state.last_rows = len(rows)
    # Never emit an alarm for a pull whose audit rows/cursor could still roll
    # back.  The old order sent the robot message first, then lost both cursor
    # and silence_alerted_at when an unrelated event insert failed.
    db.commit()
    _maybe_alarm(db, settings, state)
    return {"rows": len(rows), **summary}


def audit_status(db: Session) -> dict:
    state = db.get(AuditState, 1)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    stored_today = db.scalar(select(func.count()).select_from(FileAuditEvent)
                             .where(FileAuditEvent.gmt_create >= int(datetime.strptime(today, "%Y-%m-%d")
                                                                     .replace(tzinfo=CST).timestamp() * 1000))) or 0
    reads_today = db.scalar(select(func.sum(AuditDailyAgg.count)).where(AuditDailyAgg.day == today)) or 0
    recent = db.scalars(select(FileAuditEvent).order_by(FileAuditEvent.gmt_create.desc()).limit(10)).all()
    return {
        "cursor_at": datetime.fromtimestamp(state.last_gmt_create / 1000, CST).isoformat() if state and state.last_gmt_create else None,
        "last_run_at": state.last_run_at.isoformat() if state and state.last_run_at else None,
        "last_rows": state.last_rows if state else 0,
        "write_events_today": stored_today,
        "read_ops_today": int(reads_today),
        "recent_writes": [{"time": datetime.fromtimestamp(e.gmt_create / 1000, CST).strftime("%H:%M:%S"),
                           "operator": e.operator_name, "action": e.action_view, "resource": e.resource[:60],
                           "module": e.module_view} for e in recent],
    }
