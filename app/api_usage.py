"""Persistent, fail-closed budgets for DingTalk request attempts."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select, update

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


def reserve_call(settings: Settings, interface: str) -> None:
    if settings.demo_mode:
        return
    from .integrations import IntegrationError
    now = datetime.now(CST)
    day, month = now.strftime("day:%Y-%m-%d"), now.strftime("month:%Y-%m")
    limits = [(day, "total", settings.dingtalk_daily_limit),
              (month, "total", settings.dingtalk_monthly_limit)]
    endpoint_limits = {
        "wiki.nodes": (settings.dingtalk_daily_limit, settings.dingtalk_nodes_monthly_limit),
        "storage.search": (settings.dingtalk_search_daily_limit, settings.dingtalk_search_monthly_limit),
        "exclusive.fileAuditLogs": (settings.dingtalk_audit_daily_limit, settings.dingtalk_monthly_limit),
    }
    dl, ml = endpoint_limits.get(interface, (settings.dingtalk_daily_limit, settings.dingtalk_monthly_limit))
    limits += [(day, interface, dl), (month, interface, ml)]
    if scan_month.get():
        limits.append(("scan:" + scan_month.get(), "total", settings.scan_max_api_calls))
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
                    raise IntegrationError("dingtalk_budget_exhausted", "钉钉接口调用预算已达上限，已停止外部请求。", 429)
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
                       "scan": settings.scan_max_api_calls},
            "items": [{"period": r.period, "interface": r.interface, "calls": r.calls} for r in rows]}
