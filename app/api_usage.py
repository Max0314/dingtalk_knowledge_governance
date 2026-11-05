"""Persistent, fail-closed budgets for DingTalk request attempts."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import Settings
from .db import ApiUsage, SessionLocal, utcnow

logger = logging.getLogger("kg.api_usage")
scan_month: ContextVar[str] = ContextVar("scan_month", default="")
CST = timezone(timedelta(hours=8))


@contextmanager
def scan_budget(month: str):
    token = scan_month.set(month)
    try:
        yield
    finally:
        scan_month.reset(token)


def _limits(settings, interface, now):
    day, month = now.strftime("day:%Y-%m-%d"), now.strftime("month:%Y-%m")
    scanning = bool(scan_month.get())
    limits = [(day, "total", max(0, settings.dingtalk_daily_limit - (settings.scan_daily_reserve if scanning else 0))),
              (month, "total", max(0, settings.dingtalk_monthly_limit - (settings.scan_monthly_reserve if scanning else 0)))]
    endpoint_limits = {
        "wiki.nodes": (settings.dingtalk_daily_limit, settings.dingtalk_nodes_monthly_limit),
        "storage.search": (settings.dingtalk_search_daily_limit, settings.dingtalk_search_monthly_limit),
        "exclusive.fileAuditLogs": (settings.dingtalk_audit_daily_limit, settings.dingtalk_monthly_limit),
    }
    dl, ml = endpoint_limits.get(interface, (settings.dingtalk_daily_limit, settings.dingtalk_monthly_limit))
    limits += [(day, interface, dl), (month, interface, ml)]
    if scan_month.get():
        limits.append(("scan:" + scan_month.get(), "total", settings.scan_max_api_calls))
    return limits


def _reset_at(period, now):
    if period.startswith('day:'):
        reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)
    else:
        reset = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=1, microsecond=0)
    return reset.astimezone(timezone.utc)


def budget_wait_until(db, settings, interface):
    """Cheap advisory read; reserve_call remains the atomic final gate."""
    if settings.demo_mode:
        return None
    from .integrations import IntegrationError
    now = datetime.now(CST)
    limits = _limits(settings, interface, now)
    try:
        # Independent short snapshot: never roll back local event changes when
        # this advisory read fails, or retain a caller's stale quota snapshot.
        with Session(bind=db.get_bind()) as reader:
            rows = reader.scalars(select(ApiUsage).where(ApiUsage.period.in_({p for p, _, _ in limits}),
                                                         ApiUsage.interface.in_({'total', interface}))).all()
            calls = {(r.period, r.interface): r.calls for r in rows}
    except Exception as exc:
        raise IntegrationError('dingtalk_budget_unavailable', '调用量计数不可用，已停止钉钉请求。', 503) from exc
    resets = [_reset_at(p, now) for p, name, limit in limits if calls.get((p, name), 0) >= limit]
    return max(resets) if resets else None


def reserve_call(settings: Settings, interface: str) -> None:
    if settings.demo_mode:
        return
    from .integrations import IntegrationError
    now = datetime.now(CST)
    day, month = now.strftime("day:%Y-%m-%d"), now.strftime("month:%Y-%m")
    scanning = bool(scan_month.get())
    limits = _limits(settings, interface, now)
    try:
        with SessionLocal.begin() as db:
            for period, name, limit in limits:
                values = dict(period=period, interface=name, calls=0, updated_at=utcnow())
                if db.bind.dialect.name == "mysql":
                    from sqlalchemy.dialects.mysql import insert
                    stmt = insert(ApiUsage).values(**values)
                    db.execute(stmt.on_duplicate_key_update(calls=ApiUsage.calls))
                else:
                    from sqlalchemy.dialects.sqlite import insert
                    db.execute(insert(ApiUsage).values(**values).on_conflict_do_nothing())
                changed = db.execute(update(ApiUsage).where(
                    ApiUsage.period == period, ApiUsage.interface == name, ApiUsage.calls < limit
                ).values(calls=ApiUsage.calls + 1, updated_at=utcnow())).rowcount
                if not changed:
                    logger.warning("DingTalk budget exhausted: %s %s limit=%s", period, name, limit)
                    if scanning and name == 'total' and period == day and settings.scan_daily_reserve:
                        raise IntegrationError('dingtalk_scan_daily_reserved', '本日补采额度已用尽，预留额度供增量采集；次日继续断点。', 429)
                    if scanning and name == 'total' and period == month and settings.scan_monthly_reserve:
                        raise IntegrationError('dingtalk_budget_scan_monthly_reserved', '补采触及月度增量预留额度，已停止。', 429)
                    error = IntegrationError("dingtalk_budget_exhausted", "钉钉接口调用预算已达上限，已停止外部请求。", 429)
                    error.retry_at = _reset_at(period, now)
                    raise error
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError("dingtalk_budget_unavailable", "调用量计数不可用，已停止钉钉请求。", 503) from exc


def usage_status(db, settings: Settings) -> dict:
    now = datetime.now(CST)
    periods = [now.strftime("day:%Y-%m-%d"), now.strftime("month:%Y-%m"), now.strftime("scan:%Y-%m")]
    rows = db.scalars(select(ApiUsage).where(ApiUsage.period.in_(periods))).all()
    return {"measurement": "HTTP 请求尝试数（含失败），从修复部署后开始计数",
            "limits": {"daily": settings.dingtalk_daily_limit, "monthly": settings.dingtalk_monthly_limit,
                       "nodes_monthly": settings.dingtalk_nodes_monthly_limit,
                       "search_daily": settings.dingtalk_search_daily_limit,
                       "search_monthly": settings.dingtalk_search_monthly_limit,
                       "audit_daily": settings.dingtalk_audit_daily_limit,
                       "scan": settings.scan_max_api_calls,
                       "scan_daily_reserve": settings.scan_daily_reserve, "scan_monthly_reserve": settings.scan_monthly_reserve},
            "items": [{"period": r.period, "interface": r.interface, "calls": r.calls} for r in rows]}
