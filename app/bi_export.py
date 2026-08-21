"""Versioned, read-only upload facts for BI-center ingestion.

The web dashboard exposes session-protected, page-oriented APIs.  This module
keeps the cross-service contract deliberately smaller: no document bodies,
document titles, URLs, source UserIDs, or cached organisation names leave this
service.  ``bi_center`` owns the employee directory and applies its monthly
organisation snapshots after it pulls the employee-key facts.
"""
from __future__ import annotations

import hmac
import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import metrics
from .config import Settings
from .db import EmployeeMap, UploaderMonthStat, Workspace


CONTRACT_VERSION = 1
TIMEZONE = "Asia/Shanghai"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ExportApiError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def authorize(request, settings: Settings) -> None:
    """Authenticate an export caller independently from the web session."""
    if not settings.bi_export_enabled:
        raise ExportApiError(404, "export_disabled")
    allowed = [item.strip() for item in settings.bi_export_api_keys.split(",") if item.strip()]
    if not allowed:
        raise ExportApiError(503, "export_not_configured")
    provided = str(request.headers.get("x-api-key") or "").strip()
    if not provided or not any(hmac.compare_digest(provided, item) for item in allowed):
        raise ExportApiError(401, "unauthorized")


def validate_month(value: str) -> str:
    month = str(value or "").strip()
    if not MONTH_RE.fullmatch(month):
        raise ExportApiError(400, "invalid_month")
    return month


def validate_page(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        raise ExportApiError(400, "invalid_pagination") from None
    if parsed < minimum or parsed > maximum:
        raise ExportApiError(400, "invalid_pagination")
    return parsed


def _snapshot_id(db: Session) -> str:
    return metrics.uploader_snapshot_id(db)


def _source_rows(db: Session, month: str) -> tuple[str, list[dict[str, Any]]]:
    snapshot_id = _snapshot_id(db)
    if not snapshot_id:
        return "", []
    statement = (
        select(
            UploaderMonthStat.creator_user_id,
            UploaderMonthStat.workspace_id,
            UploaderMonthStat.workspace_name,
            UploaderMonthStat.file_count,
            EmployeeMap.employee_key,
            EmployeeMap.matched,
            EmployeeMap.include_official,
        )
        .outerjoin(EmployeeMap, EmployeeMap.user_id == UploaderMonthStat.creator_user_id)
        .where(UploaderMonthStat.snapshot_id == snapshot_id, UploaderMonthStat.month == month)
    )
    rows = []
    for row in db.execute(statement):
        rows.append(
            {
                "source_user_id": str(row.creator_user_id or ""),
                "workspace_id": str(row.workspace_id or ""),
                "workspace_name": str(row.workspace_name or ""),
                "file_count": int(row.file_count or 0),
                "employee_key": str(row.employee_key or ""),
                "matched": bool(row.matched),
                "include_official": bool(row.include_official),
            }
        )
    return snapshot_id, rows


def _is_official(row: dict[str, Any], robots: set[str]) -> bool:
    return bool(
        row["employee_key"]
        and row["matched"]
        and row["include_official"]
        and row["source_user_id"] not in robots
        and row["employee_key"] not in robots
    )


def _as_of(db: Session) -> str | None:
    value = db.scalar(select(func.max(Workspace.synced_at)))
    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def _meta(db: Session, snapshot_id: str) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "timezone": TIMEZONE,
        "sourceSnapshotId": snapshot_id or None,
        "dataStatus": "live_derived",
        "asOf": _as_of(db),
    }


def latest(db: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_id = _snapshot_id(db)
    months: list[str] = []
    if snapshot_id:
        months = list(
            db.scalars(
                select(UploaderMonthStat.month)
                .where(UploaderMonthStat.snapshot_id == snapshot_id)
                .distinct()
                .order_by(UploaderMonthStat.month)
            )
        )
    data = {
        "latestMonth": months[-1] if months else None,
        "availableMonths": months,
        "metricScope": "uploads_only",
        "note": "当前版本按上传文件统计；评审完成、人工审核和访问行为不包含在本导出中。",
    }
    return data, _meta(db, snapshot_id)


def monthly_summary(db: Session, month: str) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_id, rows = _source_rows(db, month)
    robots = metrics.robot_ids()
    observed_files = sum(row["file_count"] for row in rows)
    official_rows = [row for row in rows if _is_official(row, robots)]
    official_files = sum(row["file_count"] for row in official_rows)
    data = {
        "month": month,
        "allObserved": {
            "uploadedFileCount": observed_files,
            "sourceContributorCount": len({row["source_user_id"] for row in rows if row["source_user_id"]}),
            "workspaceCount": len({row["workspace_id"] for row in rows if row["workspace_id"]}),
        },
        "officialEmployees": {
            "uploadedFileCount": official_files,
            "employeeCount": len({row["employee_key"] for row in official_rows}),
            "workspaceCount": len({row["workspace_id"] for row in official_rows if row["workspace_id"]}),
        },
        "diagnostics": {
            "excludedUploadedFileCount": observed_files - official_files,
            "excludedSourceContributorCount": len(
                {row["source_user_id"] for row in rows if not _is_official(row, robots) and row["source_user_id"]}
            ),
        },
    }
    return data, _meta(db, snapshot_id)


def monthly_employees(db: Session, month: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot_id, rows = _source_rows(db, month)
    robots = metrics.robot_ids()
    facts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _is_official(row, robots):
            continue
        fact = facts.setdefault(
            row["employee_key"],
            {"month": month, "employeeKey": row["employee_key"], "uploadedFileCount": 0, "_workspaces": set()},
        )
        fact["uploadedFileCount"] += row["file_count"]
        if row["workspace_id"]:
            fact["_workspaces"].add(row["workspace_id"])
    items = []
    for fact in facts.values():
        items.append(
            {
                "month": fact["month"],
                "employeeKey": fact["employeeKey"],
                "uploadedFileCount": fact["uploadedFileCount"],
                "workspaceCount": len(fact["_workspaces"]),
            }
        )
    items.sort(key=lambda item: (-item["uploadedFileCount"], item["employeeKey"]))
    return items, _meta(db, snapshot_id)


def monthly_employee_workspaces(db: Session, month: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot_id, rows = _source_rows(db, month)
    robots = metrics.robot_ids()
    facts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not _is_official(row, robots) or not row["workspace_id"]:
            continue
        key = (row["employee_key"], row["workspace_id"])
        fact = facts.setdefault(
            key,
            {
                "month": month,
                "employeeKey": row["employee_key"],
                "workspaceId": row["workspace_id"],
                "workspaceName": row["workspace_name"],
                "uploadedFileCount": 0,
            },
        )
        fact["uploadedFileCount"] += row["file_count"]
    items = list(facts.values())
    items.sort(key=lambda item: (-item["uploadedFileCount"], item["employeeKey"], item["workspaceId"]))
    return items, _meta(db, snapshot_id)


def paginate(items: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    offset = (page - 1) * page_size
    return items[offset:offset + page_size], {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }
