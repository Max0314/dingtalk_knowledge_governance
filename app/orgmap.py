"""bi_center identity resolution with a local cache table.

Resolution happens in small batches with pauses so a dashboard load never
turns into a burst against bi_center. Unresolvable ids (robots, resigned,
external) are cached as unmatched so they are not retried on every view.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import EmployeeMap, utcnow
from .integrations import BiCenterClient, IntegrationError

CACHE_TTL = timedelta(days=7)
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 0.5
MAX_PER_CALL = 500


def _stale(row: EmployeeMap) -> bool:
    resolved = row.resolved_at
    if resolved is None:
        return True
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - resolved > CACHE_TTL


def ensure_employees(db: Session, settings: Settings, user_ids: list[str]) -> dict[str, int]:
    """Resolve unknown or stale userIds through bi_center. Bounded and cached."""
    wanted = [u for u in dict.fromkeys(user_ids) if u][:MAX_PER_CALL]
    cached = {row.user_id: row for row in db.scalars(select(EmployeeMap).where(EmployeeMap.user_id.in_(wanted))).all()} if wanted else {}
    pending = [u for u in wanted if u not in cached or _stale(cached[u])]
    if not pending:
        return {"requested": len(wanted), "resolved": 0, "skipped": len(wanted)}

    client = BiCenterClient(settings)
    if not client.configured():
        for user_id in pending:
            row = cached.get(user_id) or EmployeeMap(user_id=user_id)
            row.matched, row.resolved_at = False, utcnow()
            db.merge(row)
        db.commit()
        return {"requested": len(wanted), "resolved": 0, "skipped": len(wanted), "note": "bi_center 未配置"}

    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    resolved = failed = 0
    for offset in range(0, len(pending), BATCH_SIZE):
        chunk = pending[offset:offset + BATCH_SIZE]
        try:
            results = asyncio.run(client.resolve_batch([{"userId": user_id} for user_id in chunk], month_key))
        except (IntegrationError, Exception):
            failed += len(chunk)
            continue
        for user_id, result in zip(chunk, results if len(results) == len(chunk) else [{}] * len(chunk)):
            row = cached.get(user_id) or db.get(EmployeeMap, user_id) or EmployeeMap(user_id=user_id)
            matched = bool(result.get("matched"))
            row.matched = matched
            row.include_official = bool(result.get("includeInOfficialStats"))
            row.employee_key = result.get("employeeKey", "") or row.employee_key
            row.name = result.get("employeeName", "") or row.name
            row.department_name = result.get("departmentName", "") or row.department_name
            row.biz_group_name = result.get("bizGroupName", "") or row.biz_group_name
            row.resolved_at = utcnow()
            db.merge(row)
            resolved += 1
        db.commit()
        if offset + BATCH_SIZE < len(pending):
            time.sleep(BATCH_PAUSE_SECONDS)
    return {"requested": len(wanted), "resolved": resolved, "failed": failed}
