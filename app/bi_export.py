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

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import metrics
from .config import Settings, get_settings
from .db import Document, EmployeeMap, ReviewInstance, UploaderMonthStat, Workspace
from .service import review_excluded_levels, workspace_level, workspace_name_is_ignored


CONTRACT_VERSION = 1
DASHBOARD_CONTRACT_VERSION = 2
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
    # bytes keeps compare_digest constant-time for every UTF-8 value; its str
    # form rejects non-ASCII input with TypeError and would turn a bad header
    # into a 500 instead of the intended 401.
    provided_bytes = provided.encode("utf-8")
    if not provided or not any(
        hmac.compare_digest(provided_bytes, item.encode("utf-8")) for item in allowed
    ):
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
    settings = get_settings()
    for row in db.execute(statement):
        if workspace_name_is_ignored(settings, str(row.workspace_name or "")):
            continue
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


def _meta(db: Session, snapshot_id: str, *, contract_version: int = CONTRACT_VERSION) -> dict[str, Any]:
    return {
        "contractVersion": contract_version,
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


def _latest_review_subquery(eligible_workspace_ids: set[str]):
    """One latest AI review per current file, with no document payload fields."""
    ranked = (
        select(
            ReviewInstance.node_id.label("node_id"),
            ReviewInstance.ai_score.label("ai_score"),
            ReviewInstance.verdict.label("verdict"),
            ReviewInstance.created_at.label("reviewed_at"),
            Document.workspace_id.label("workspace_id"),
            Document.uploader_key.label("uploader_key"),
            func.row_number().over(
                partition_by=ReviewInstance.node_id,
                order_by=(
                    ReviewInstance.created_at.desc(),
                    ReviewInstance.review_instance_id.desc(),
                ),
            ).label("rank"),
        )
        .join(Document, Document.node_id == ReviewInstance.node_id)
        .where(
            Document.is_folder.is_(False),
            Document.is_deleted.is_(False),
            Document.workspace_id.in_(eligible_workspace_ids),
        )
        .subquery()
    )
    return select(
        ranked.c.node_id,
        ranked.c.ai_score,
        ranked.c.verdict,
        ranked.c.reviewed_at,
        ranked.c.workspace_id,
        ranked.c.uploader_key,
    ).where(ranked.c.rank == 1).subquery()


def _quality_columns(latest):
    return (
        func.count().label("reviewed_count"),
        func.avg(latest.c.ai_score).label("average_score"),
        func.sum(case((latest.c.verdict == "pass", 1), else_=0)).label("pass_count"),
        func.sum(case((latest.c.verdict == "manual_review", 1), else_=0)).label("manual_review_count"),
        func.sum(case((latest.c.verdict == "return", 1), else_=0)).label("return_count"),
    )


def _quality_payload(row: Any) -> dict[str, Any]:
    reviewed_count = int(row.reviewed_count or 0)
    average_score = row.average_score
    return {
        "reviewedDocumentCount": reviewed_count,
        "averageAiScore": round(float(average_score), 1) if average_score is not None else None,
        "passCount": int(row.pass_count or 0),
        "manualReviewCount": int(row.manual_review_count or 0),
        "returnCount": int(row.return_count or 0),
    }


def _empty_quality_payload() -> dict[str, Any]:
    return {
        "reviewedDocumentCount": 0,
        "averageAiScore": None,
        "passCount": 0,
        "manualReviewCount": 0,
        "returnCount": 0,
    }


def _review_qualities_by_month(db: Session, latest) -> dict[str, dict[str, Any]]:
    month_key = func.substr(latest.c.reviewed_at, 1, 7).label("month")
    rows = db.execute(
        select(month_key, *_quality_columns(latest))
        .group_by(month_key)
        .order_by(month_key)
    ).all()
    return {str(row.month): _quality_payload(row) for row in rows if str(row.month or "").strip()}


def _employee_quality_facts(
    db: Session,
    latest,
    months: set[str],
    robots: set[str],
) -> list[dict[str, Any]]:
    if not months:
        return []
    month_key = func.substr(latest.c.reviewed_at, 1, 7).label("month")
    statement = (
        select(month_key, EmployeeMap.employee_key.label("employee_key"), *_quality_columns(latest))
        .join(EmployeeMap, EmployeeMap.user_id == latest.c.uploader_key)
        .where(
            EmployeeMap.matched.is_(True),
            EmployeeMap.include_official.is_(True),
            EmployeeMap.employee_key != "",
            month_key.in_(months),
        )
        .group_by(month_key, EmployeeMap.employee_key)
        .order_by(month_key, EmployeeMap.employee_key)
    )
    if robots:
        statement = statement.where(
            latest.c.uploader_key.not_in(robots),
            EmployeeMap.employee_key.not_in(robots),
        )
    return [
        {
            "month": str(row.month),
            "employeeKey": str(row.employee_key),
            **_quality_payload(row),
        }
        for row in db.execute(statement).all()
        if str(row.month or "").strip() and str(row.employee_key or "").strip()
    ]


def dashboard(db: Session, months: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the aggregate-only V2 contract consumed by BI Center.

    This endpoint deliberately exports workspace names and opaque employee keys
    only.  Document names, node IDs, source user IDs, URLs, bodies, findings,
    and cached organisation fields remain inside this service.
    """
    safe_months = max(1, min(int(months or 6), 24))
    excluded_levels = review_excluded_levels(get_settings())
    workspace_names = {
        str(row.workspace_id): str(row.name or "")
        for row in db.execute(
            select(Workspace.workspace_id, Workspace.name).where(Workspace.is_active.is_(True))
        ).all()
        if workspace_level(str(row.name or "")) not in excluded_levels
    }
    eligible_workspace_ids = set(workspace_names)
    increments = metrics.monthly_increments(db)
    collected = metrics.collected(db)
    latest = _latest_review_subquery(eligible_workspace_ids)
    quality_by_month = _review_qualities_by_month(db, latest)
    all_upload_by_month = {
        str(row.get("month") or ""): row
        for row in increments.get("rows") or []
        if str(row.get("month") or "").strip()
    }
    eligible_month_totals: dict[str, int] = {}
    for workspace_id in eligible_workspace_ids:
        for month, count in ((collected.get("space_months") or {}).get(workspace_id) or {}).items():
            eligible_month_totals[str(month)] = eligible_month_totals.get(str(month), 0) + int(count or 0)
    upload_by_month = {}
    for month, total in eligible_month_totals.items():
        all_upload = all_upload_by_month.get(month) or {}
        bulk = min(total, int(all_upload.get("bulk_import") or 0))
        upload_by_month[month] = {
            "month": month,
            "total": total,
            "bulk_import": bulk,
            "routine": max(0, total - bulk),
        }
    available_months = sorted(set(upload_by_month) | set(quality_by_month))
    selected_months = available_months[-safe_months:]
    selected_set = set(selected_months)
    current_month = selected_months[-1] if selected_months else ""
    robots = metrics.robot_ids()

    quality_rows = []
    for month in selected_months:
        upload = upload_by_month.get(month) or {}
        quality = quality_by_month.get(month) or {}
        quality_rows.append(
            {
                "month": month,
                "uploadedFileCount": int(upload.get("total") or 0),
                "bulkUploadedFileCount": int(upload.get("bulk_import") or 0),
                "routineUploadedFileCount": int(upload.get("routine") or 0),
                **{
                    key: quality.get(key, 0 if key.endswith("Count") else None)
                    for key in (
                        "reviewedDocumentCount",
                        "averageAiScore",
                        "passCount",
                        "manualReviewCount",
                        "returnCount",
                    )
                },
            }
        )

    global_quality_row = db.execute(select(*_quality_columns(latest))).one()
    global_quality = _quality_payload(global_quality_row)
    total_files = sum(
        int(count or 0)
        for workspace_id, count in (collected.get("space_totals") or {}).items()
        if str(workspace_id) in eligible_workspace_ids
    )
    global_quality["reviewCoverageRate"] = round(
        global_quality["reviewedDocumentCount"] * 100 / total_files,
        1,
    ) if total_files else 0.0

    workspace_quality_rows = db.execute(
        select(latest.c.workspace_id.label("workspace_id"), *_quality_columns(latest))
        .group_by(latest.c.workspace_id)
    ).all()
    workspace_quality = {str(row.workspace_id): _quality_payload(row) for row in workspace_quality_rows}
    workspace_rows = []
    for workspace_id, file_count in (collected.get("space_totals") or {}).items():
        safe_workspace_id = str(workspace_id or "")
        if not safe_workspace_id or safe_workspace_id not in eligible_workspace_ids:
            continue
        quality = workspace_quality.get(safe_workspace_id) or _empty_quality_payload()
        total = int(file_count or 0)
        workspace_rows.append(
            {
                "workspaceId": safe_workspace_id,
                "workspaceName": workspace_names.get(safe_workspace_id) or "未命名知识库",
                "fileCount": total,
                "currentMonthNewFileCount": int(
                    ((collected.get("space_months") or {}).get(safe_workspace_id) or {}).get(current_month, 0)
                ),
                "reviewCoverageRate": round(quality["reviewedDocumentCount"] * 100 / total, 1) if total else 0.0,
                **quality,
            }
        )
    workspace_rows.sort(
        key=lambda item: (
            -int(item["returnCount"]),
            float(item["reviewCoverageRate"]),
            -int(item["fileCount"]),
            item["workspaceName"],
        )
    )

    employee_facts: dict[tuple[str, str], dict[str, Any]] = {}
    for month in selected_months:
        _, source_rows = _source_rows(db, month)
        for row in source_rows:
            if not _is_official(row, robots) or str(row["workspace_id"] or "") not in eligible_workspace_ids:
                continue
            key = (month, str(row["employee_key"]))
            fact = employee_facts.setdefault(
                key,
                {
                    "month": month,
                    "employeeKey": str(row["employee_key"]),
                    "uploadedFileCount": 0,
                    "workspaceCount": 0,
                    "reviewedDocumentCount": 0,
                    "averageAiScore": None,
                    "passCount": 0,
                    "manualReviewCount": 0,
                    "returnCount": 0,
                    "_workspaceIds": set(),
                    "_scoreTotal": 0.0,
                },
            )
            fact["uploadedFileCount"] += int(row["file_count"] or 0)
            if row["workspace_id"]:
                fact["_workspaceIds"].add(str(row["workspace_id"]))
    for quality in _employee_quality_facts(db, latest, selected_set, robots):
        key = (quality["month"], quality["employeeKey"])
        fact = employee_facts.setdefault(
            key,
            {
                "month": quality["month"],
                "employeeKey": quality["employeeKey"],
                "uploadedFileCount": 0,
                "workspaceCount": 0,
                "reviewedDocumentCount": 0,
                "averageAiScore": None,
                "passCount": 0,
                "manualReviewCount": 0,
                "returnCount": 0,
                "_workspaceIds": set(),
                "_scoreTotal": 0.0,
            },
        )
        reviewed_count = int(quality["reviewedDocumentCount"] or 0)
        fact["reviewedDocumentCount"] += reviewed_count
        fact["passCount"] += int(quality["passCount"] or 0)
        fact["manualReviewCount"] += int(quality["manualReviewCount"] or 0)
        fact["returnCount"] += int(quality["returnCount"] or 0)
        if quality["averageAiScore"] is not None:
            fact["_scoreTotal"] += float(quality["averageAiScore"]) * reviewed_count
    employee_rows = []
    for fact in employee_facts.values():
        fact["workspaceCount"] = len(fact.pop("_workspaceIds"))
        score_total = float(fact.pop("_scoreTotal"))
        reviewed_count = int(fact["reviewedDocumentCount"] or 0)
        if reviewed_count:
            fact["averageAiScore"] = round(score_total / reviewed_count, 1)
        employee_rows.append(fact)
    employee_rows.sort(key=lambda item: (item["month"], item["employeeKey"]))

    current_quality = quality_by_month.get(current_month) or {}
    current_upload = upload_by_month.get(current_month) or {}
    workspace_count = len(eligible_workspace_ids)
    data = {
        "metricScope": "uploaded_files_and_latest_ai_reviews",
        "latestMonth": current_month or None,
        "availableMonths": available_months,
        "summary": {
            "workspaceCount": workspace_count,
            "totalFileCount": total_files,
            "currentMonthNewFileCount": int(current_upload.get("total") or 0),
            "currentMonthAverageAiScore": current_quality.get("averageAiScore"),
            "currentMonthReviewedDocumentCount": int(current_quality.get("reviewedDocumentCount") or 0),
            **global_quality,
        },
        "monthly": quality_rows,
        "workspaces": workspace_rows,
        "employees": employee_rows,
    }
    return data, _meta(
        db,
        _snapshot_id(db),
        contract_version=DASHBOARD_CONTRACT_VERSION,
    )


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
