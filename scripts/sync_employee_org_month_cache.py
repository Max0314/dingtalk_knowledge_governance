"""Cache bi_center monthly organization snapshots for increment filtering.

This is an explicit, read-only maintenance command.  It calls bi_center only;
it never calls DingTalk and never prints employee identities.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete

from app import metrics
from app.config import get_settings
from app.db import EmployeeOrgMonth, SessionLocal, init_db
from app.integrations import BiCenterClient


MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def report_months() -> list[str]:
    """Months actually represented in the merged increment data."""
    with SessionLocal() as db:
        data = metrics.collected(db)
    return sorted({day[:7] for _, day in data["creator_day"] if MONTH_RE.fullmatch(day[:7])})


async def directory_for_month(client: BiCenterClient, month: str,
                              resolved_cache: dict[str, tuple[list[dict[str, Any]], dict[str, str]]]
                              ) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
    first = await client.employee_directory_month_page(month, limit=500, offset=0)
    resolved = str(first.get("resolvedSnapshotMonth") or month)
    if resolved in resolved_cache:
        items, versions = resolved_cache[resolved]
        return resolved, items, versions

    items = list(first.get("items") or [])
    total = int(first.get("total") or len(items))
    offset = len(items)
    while offset < total:
        page = await client.employee_directory_month_page(month, limit=500, offset=offset)
        page_items = list(page.get("items") or [])
        if not page_items:
            break
        items.extend(page_items)
        offset += len(page_items)
    versions = {
        "directory_version": str(first.get("directoryVersion") or ""),
        "policy_version": str(first.get("policyVersion") or ""),
    }
    resolved_cache[resolved] = (items, versions)
    return resolved, items, versions


async def sync(months: list[str]) -> dict[str, Any]:
    client = BiCenterClient(get_settings())
    resolved_cache: dict[str, tuple[list[dict[str, Any]], dict[str, str]]] = {}
    inserted = 0
    rd_rows = 0
    resolved_months: set[str] = set()
    for month in months:
        resolved, items, versions = await directory_for_month(client, month, resolved_cache)
        resolved_months.add(resolved)
        # One UnionID must appear at most once per monthly snapshot.  Dedupe
        # defensively before replacing the local cache month.
        by_employee: dict[str, dict[str, Any]] = {}
        for item in items:
            employee_key = str(item.get("employeeKey") or item.get("unionId") or "")
            if employee_key and item.get("isActive") is not False:
                by_employee[employee_key] = item
        synced_at = datetime.now(timezone.utc)
        rows = [
            EmployeeOrgMonth(
                month=month,
                employee_key=employee_key,
                department_name=str(item.get("dept") or item.get("departmentName") or ""),
                biz_group_name=str(item.get("biz") or item.get("bizGroupName") or ""),
                is_rd_system=bool(item.get("isRdSystem")),
                resolved_snapshot_month=resolved,
                directory_version=versions["directory_version"],
                policy_version=versions["policy_version"],
                synced_at=synced_at,
            )
            for employee_key, item in by_employee.items()
        ]
        if not rows:
            raise RuntimeError(f"bi_center 月度组织目录 {month} 为空，未覆盖本地缓存。")
        month_rd_rows = sum(1 for item in by_employee.values() if bool(item.get("isRdSystem")))
        with SessionLocal() as db:
            db.execute(delete(EmployeeOrgMonth).where(EmployeeOrgMonth.month == month))
            db.add_all(rows)
            db.commit()
        inserted += len(rows)
        rd_rows += month_rd_rows
    return {
        "status": "ok",
        "report_months": len(months),
        "resolved_snapshots": len(resolved_months),
        "cached_rows": inserted,
        "rd_scope_rows": rd_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步研发体系月度组织口径缓存")
    parser.add_argument("--month", action="append", default=[], help="仅同步指定 YYYY-MM，可重复")
    args = parser.parse_args()
    init_db()
    months = sorted(set(args.month or report_months()))
    invalid = [month for month in months if not MONTH_RE.fullmatch(month)]
    if invalid:
        raise SystemExit("month 必须是 YYYY-MM。")
    result = asyncio.run(sync(months))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
