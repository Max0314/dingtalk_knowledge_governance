"""Governance metrics: one merged view over the frozen baseline and live sync.

The baseline (historical_file_nodes, imported once from the 2026-08-05 full
scan) and live documents (filled by incremental sync) are merged by node_id so
a file is never counted twice. Months attribute by source createTime in
Asia/Shanghai — the knowledge-base ingestion time.

Bulk-import detection mirrors runtime/build_increment_report.py: a calendar
day carrying >= BULK_DAY_SHARE of its month with >= BULK_DAY_MIN files is a
migration event. Bulk files still count — the split annotates the total and
never subtracts from it.
"""
from __future__ import annotations

import collections
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Document, EmployeeMap, HistoricalFileNode, HistoricalSnapshot, UploaderMonthStat, Workspace


def robot_ids() -> set[str]:
    return {value.strip() for value in get_settings().robot_user_ids.split(",") if value.strip()}

BULK_DAY_MIN = 200
BULK_DAY_SHARE = 0.25
CACHE_TTL_SECONDS = 60

_cache: dict[str, Any] = {"stamp": None, "at": 0.0, "value": None}


def _change_stamp(db: Session) -> tuple:
    return (
        db.scalar(select(func.count()).select_from(HistoricalFileNode)) or 0,
        db.scalar(select(func.count()).select_from(Document)) or 0,
        db.scalar(select(func.max(Document.discovered_at))),
    )


def primary_snapshot_id(db: Session) -> str:
    """The one baseline snapshot that headline increment numbers come from.

    Later snapshots (e.g. the uploader-attribution scan in the new workspace-id
    namespace) must NOT leak into the increment series, or the headline totals
    would silently double-count the same knowledge bases.
    """
    snapshots = db.scalars(select(HistoricalSnapshot).order_by(HistoricalSnapshot.collected_at)).all()
    for snapshot in snapshots:
        if (snapshot.definition or {}).get("is_primary_baseline"):
            return snapshot.snapshot_id
    for snapshot in snapshots:
        if snapshot.snapshot_id == "wiki-baseline-2026-08-05":
            return snapshot.snapshot_id
    return snapshots[0].snapshot_id if snapshots else ""


def _collect(db: Session) -> dict[str, Any]:
    """One pass over both sources, deduplicated by node_id."""
    baseline = primary_snapshot_id(db)
    files: dict[str, tuple[str, str]] = {}  # node_id -> (workspace_id, created_at)
    for workspace_id, node_id, created in db.execute(
            select(HistoricalFileNode.workspace_id, HistoricalFileNode.node_id, HistoricalFileNode.source_created_at)
            .where(HistoricalFileNode.snapshot_id == baseline)):
        files[node_id] = (workspace_id, created or "")
    for workspace_id, node_id, created in db.execute(
            select(Document.workspace_id, Document.node_id, Document.source_created_at)
            .where(Document.is_folder.is_(False), Document.is_deleted.is_(False))):
        files.setdefault(node_id, (workspace_id, created or ""))

    monthly = collections.Counter()
    month_days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    day_spaces: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    space_months: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    space_totals = collections.Counter()
    for workspace_id, created in files.values():
        space_totals[workspace_id] += 1
        if len(created) < 7:
            continue
        month, day = created[:7], created[:10]
        monthly[month] += 1
        space_months[workspace_id][month] += 1
        if len(day) == 10:
            month_days[month][day] += 1
            day_spaces[day][workspace_id] += 1

    bulk_days = []
    bulk_by_month = collections.Counter()
    for month in sorted(month_days):
        for day, count in sorted(month_days[month].items()):
            if count >= BULK_DAY_MIN and count / monthly[month] >= BULK_DAY_SHARE:
                bulk_days.append({"day": day, "files": count, "share_of_month": round(count / monthly[month], 3),
                                  "workspace_ids": [ws for ws, _ in day_spaces[day].most_common(5)]})
                bulk_by_month[month] += count

    return {
        "total_files": len(files),
        "monthly": dict(monthly),
        "bulk_by_month": dict(bulk_by_month),
        "bulk_days": bulk_days,
        "space_totals": dict(space_totals),
        "space_months": {ws: dict(months) for ws, months in space_months.items()},
    }


def collected(db: Session) -> dict[str, Any]:
    stamp = _change_stamp(db)
    now = time.monotonic()
    if _cache["stamp"] == stamp and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["value"]
    value = _collect(db)
    _cache.update(stamp=stamp, at=now, value=value)
    return value


def snapshot_context(db: Session) -> dict[str, Any]:
    snapshot = db.get(HistoricalSnapshot, primary_snapshot_id(db)) or \
        db.scalars(select(HistoricalSnapshot).order_by(HistoricalSnapshot.collected_at.desc())).first()
    if not snapshot:
        return {"snapshot_id": None, "definition": {}, "collected_at": None}
    return {"snapshot_id": snapshot.snapshot_id, "definition": snapshot.definition or {},
            "collected_at": snapshot.collected_at.isoformat() if snapshot.collected_at else None}


def monthly_increments(db: Session, year: str = "") -> dict[str, Any]:
    data = collected(db)
    months = sorted(month for month in data["monthly"] if not year or month.startswith(year))
    rows = []
    for month in months:
        total = data["monthly"][month]
        bulk = data["bulk_by_month"].get(month, 0)
        rows.append({"month": month, "total": total, "bulk_import": bulk, "routine": total - bulk,
                     "bulk_days": [b for b in data["bulk_days"] if b["day"][:7] == month]})
    yearly: dict[str, dict[str, int]] = {}
    for month, total in data["monthly"].items():
        bucket = yearly.setdefault(month[:4], {"total": 0, "bulk_import": 0, "routine": 0})
        bulk = data["bulk_by_month"].get(month, 0)
        bucket["total"] += total
        bucket["bulk_import"] += bulk
        bucket["routine"] += total - bulk
    context = snapshot_context(db)
    return {
        "rows": rows,
        "yearly": {y: yearly[y] for y in sorted(yearly)},
        "total_files": data["total_files"],
        "bulk_day_rule": f"单日 >= {BULK_DAY_MIN} 个文件且占当月 >= {BULK_DAY_SHARE:.0%} 判为批量导入日",
        "metric_note": "全量为准；批量导入/日常仅拆分构成，不做扣减。月份按钉钉 createTime（Asia/Shanghai）归属。",
        "baseline": context,
        "caveats": [
            "下限口径：扫描前已删除的文件不可观测。",
            "仅覆盖当前授权可见的知识库，不能表述为全公司。",
            "createTime 是知识库入库时间，不是原文件的创建时间。",
        ],
    }


def coverage(db: Session) -> dict[str, Any]:
    data = collected(db)
    context = snapshot_context(db)
    definition = context["definition"]
    excluded = {item.get("workspace_id"): item for item in definition.get("excluded_workspaces", [])}
    live_counts = {row[0]: row[1] for row in db.execute(
        select(Document.workspace_id, func.count()).where(Document.is_folder.is_(False), Document.is_deleted.is_(False))
        .group_by(Document.workspace_id))}
    items = []
    for ws in db.scalars(select(Workspace).order_by(Workspace.name)).all():
        baseline_count = data["space_totals"].get(ws.workspace_id, 0)
        if ws.workspace_id in excluded:
            status = "excluded"
        elif baseline_count or live_counts.get(ws.workspace_id):
            status = "scanned"
        else:
            status = "empty"
        items.append({
            "workspace_id": ws.workspace_id, "name": ws.name, "url": ws.url,
            "status": status,
            "excluded_reason": excluded.get(ws.workspace_id, {}).get("reason", ""),
            "baseline_files": baseline_count,
            "live_documents": live_counts.get(ws.workspace_id, 0),
            "owner_department_name": ws.owner_department_name,
            "owner_biz_group_name": ws.owner_biz_group_name,
        })
    return {
        "items": items,
        "org_context": definition.get("org_context", {}),
        "unreachable": definition.get("unreachable_top", []),
        "baseline": context,
        "summary": {
            "visible_workspaces": len(items),
            "scanned": sum(1 for item in items if item["status"] == "scanned"),
            "empty": sum(1 for item in items if item["status"] == "empty"),
            "excluded": sum(1 for item in items if item["status"] == "excluded"),
        },
    }


def workspace_months(db: Session, workspace_id: str) -> dict[str, Any]:
    data = collected(db)
    months = data["space_months"].get(workspace_id, {})
    return {"workspace_id": workspace_id,
            "months": [{"month": month, "count": months[month]} for month in sorted(months)],
            "total_files": data["space_totals"].get(workspace_id, 0)}


# ---- uploader attribution (reads pre-aggregated rows only; see UploaderMonthStat) ----

def uploader_snapshot_id(db: Session) -> str:
    return db.scalar(select(func.max(UploaderMonthStat.snapshot_id))) or ""


def _employee_rows(db: Session, user_ids: list[str]) -> dict[str, EmployeeMap]:
    if not user_ids:
        return {}
    rows = db.scalars(select(EmployeeMap).where(EmployeeMap.user_id.in_(user_ids))).all()
    return {row.user_id: row for row in rows}


def _person(user_id: str, employee: EmployeeMap | None) -> dict[str, Any]:
    if user_id in robot_ids():
        return {"user_id": user_id, "name": (employee.name if employee else "") or "数字员工",
                "department_name": "系统/机器人", "biz_group_name": "系统/机器人",
                "matched": False, "include_official": False, "is_robot": True}
    if employee and employee.matched:
        return {"user_id": user_id, "name": employee.name, "department_name": employee.department_name,
                "biz_group_name": employee.biz_group_name, "matched": True,
                "include_official": employee.include_official, "is_robot": False}
    return {"user_id": user_id, "name": (employee.name if employee else "") or "", "department_name": "未映射",
            "biz_group_name": "未映射", "matched": False, "include_official": False, "is_robot": False}


def uploader_months(db: Session) -> dict[str, Any]:
    snapshot = uploader_snapshot_id(db)
    rows = db.execute(select(UploaderMonthStat.month, func.sum(UploaderMonthStat.file_count))
                      .where(UploaderMonthStat.snapshot_id == snapshot)
                      .group_by(UploaderMonthStat.month).order_by(UploaderMonthStat.month)).all()
    uploader_count = db.scalar(select(func.count(func.distinct(UploaderMonthStat.creator_user_id)))
                               .where(UploaderMonthStat.snapshot_id == snapshot)) or 0
    space_count = db.scalar(select(func.count(func.distinct(UploaderMonthStat.workspace_id)))
                            .where(UploaderMonthStat.snapshot_id == snapshot)) or 0
    return {"snapshot_id": snapshot, "months": [{"month": m, "total": int(c)} for m, c in rows],
            "uploader_count": uploader_count, "workspace_count": space_count}


def uploaders(db: Session, month: str = "", exclude_unmatched: bool = True, limit: int = 50, department: str = "") -> dict[str, Any]:
    snapshot = uploader_snapshot_id(db)
    stmt = (select(UploaderMonthStat.creator_user_id,
                   func.sum(UploaderMonthStat.file_count).label("files"),
                   func.count(func.distinct(UploaderMonthStat.workspace_id)))
            .where(UploaderMonthStat.snapshot_id == snapshot))
    if month:
        stmt = stmt.where(UploaderMonthStat.month == month)
    rows = db.execute(stmt.group_by(UploaderMonthStat.creator_user_id)
                      .order_by(func.sum(UploaderMonthStat.file_count).desc()).limit(500)).all()
    employees = _employee_rows(db, [r[0] for r in rows])
    items = []
    # Totals always count everything — robot and unmatched volume is knowledge
    # too; the exclude flag only shapes the person RANKING below.
    total = robot_files = unmatched_files = human_files = 0
    for user_id, files, spaces in rows:
        person = _person(user_id, employees.get(user_id))
        entry = {**person, "files": int(files), "workspaces": int(spaces)}
        total += int(files)
        if person.get("is_robot"):
            robot_files += int(files)
        elif not person["matched"]:
            unmatched_files += int(files)
        else:
            human_files += int(files)
        if exclude_unmatched and not person["matched"]:
            continue
        if department and person["department_name"] != department:
            continue
        items.append(entry)
    return {"snapshot_id": snapshot, "month": month, "exclude_unmatched": exclude_unmatched,
            "items": items[:limit], "total_files": total, "human_files": human_files,
            "robot_files": robot_files, "unmatched_files": unmatched_files,
            "uploader_count": sum(1 for user_id, _f, _s in rows if _person(user_id, employees.get(user_id))["matched"]),
            "note": "总量含机器人与未映射（知识资产口径）；排行按开关剔除。bi_center 仅把 matched 且 includeInOfficialStats 计入正式员工。"}


def uploader_detail(db: Session, user_id: str) -> dict[str, Any]:
    snapshot = uploader_snapshot_id(db)
    months = db.execute(select(UploaderMonthStat.month, func.sum(UploaderMonthStat.file_count))
                        .where(UploaderMonthStat.snapshot_id == snapshot, UploaderMonthStat.creator_user_id == user_id)
                        .group_by(UploaderMonthStat.month).order_by(UploaderMonthStat.month)).all()
    spaces = db.execute(select(UploaderMonthStat.workspace_name, func.sum(UploaderMonthStat.file_count))
                        .where(UploaderMonthStat.snapshot_id == snapshot, UploaderMonthStat.creator_user_id == user_id)
                        .group_by(UploaderMonthStat.workspace_name)
                        .order_by(func.sum(UploaderMonthStat.file_count).desc()).limit(10)).all()
    employee = _employee_rows(db, [user_id]).get(user_id)
    return {**_person(user_id, employee),
            "months": [{"month": m, "count": int(c)} for m, c in months],
            "top_workspaces": [{"name": n or "(未知库)", "files": int(c)} for n, c in spaces]}


def department_rollup(db: Session, month: str = "") -> dict[str, Any]:
    snapshot = uploader_snapshot_id(db)
    stmt = select(UploaderMonthStat.creator_user_id, func.sum(UploaderMonthStat.file_count)) \
        .where(UploaderMonthStat.snapshot_id == snapshot)
    if month:
        stmt = stmt.where(UploaderMonthStat.month == month)
    rows = db.execute(stmt.group_by(UploaderMonthStat.creator_user_id)).all()
    employees = _employee_rows(db, [r[0] for r in rows])
    departments: dict[str, dict[str, Any]] = {}
    for user_id, files in rows:
        person = _person(user_id, employees.get(user_id))
        bucket = departments.setdefault(person["department_name"], {"department_name": person["department_name"], "files": 0, "uploaders": 0})
        bucket["files"] += int(files)
        bucket["uploaders"] += 1
    items = sorted(departments.values(), key=lambda item: -item["files"])
    return {"month": month, "items": items}
