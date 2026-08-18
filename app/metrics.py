"""Governance metrics: one merged view over the frozen baseline and live sync.

The baseline (historical_file_nodes, imported once from the 2026-08-05 full
scan) and live documents (filled by incremental sync) are merged by node_id so
a file is never counted twice. Months attribute by source createTime in
Asia/Shanghai — the knowledge-base ingestion time.

Bulk-import classification (identity + person-day signature): robot uploads
are always bulk; a person creating >= PERSON_DAY_BULK_MIN files in one day
makes that person-day bulk. Bulk files still count — the split annotates the
total and never subtracts from it.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Document, EmployeeMap, HistoricalFileNode, HistoricalSnapshot, UploaderMonthStat, Workspace


def robot_ids() -> set[str]:
    return {value.strip() for value in get_settings().robot_user_ids.split(",") if value.strip()}

# A person creating this many files in one calendar day is doing a migration,
# not daily knowledge work. Robots are bulk at any volume.
PERSON_DAY_BULK_MIN = 200
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
    """One merged view over both sources, deduplicated by node_id — computed
    as SQL GROUP BYs. The request path must never load whole tables into
    Python: the org-wide mirror (~280k merged rows) froze the overview page
    when this was a full-table loop (2026-08-14).

    Dedup semantics (unchanged): baseline wins for a node_id present in both;
    live rows count only when absent from the baseline snapshot; a confirmed
    soft-delete hides the baseline row.

    Bulk classification (2026-08-12 rework, no more "bulk day"):
      * every file created by a robot account counts as bulk import;
      * a person creating >= PERSON_DAY_BULK_MIN files on one day makes that
        whole person-day bulk (the migration signature);
      * everything else is routine.
    """
    baseline = primary_snapshot_id(db)
    deleted_ids = select(Document.node_id).where(Document.is_deleted.is_(True))
    # 不可见库（is_active=False，连续缺席/404）双臂退出当前统计（codex 第九
    # 轮 P1：基线臂漏滤会让已删库继续出现在总览）。历史口径由基线接口保留。
    inactive_ws = select(Workspace.workspace_id).where(Workspace.is_active.is_(False))
    base_where = (HistoricalFileNode.snapshot_id == baseline,
                  HistoricalFileNode.node_type != "folder",  # the 135-lib scan stores folders too
                  HistoricalFileNode.node_id.not_in(deleted_ids),
                  HistoricalFileNode.workspace_id.not_in(inactive_ws))
    # live arm: mirror rows whose node_id the baseline does not know (anti-join
    # rides ix_hfn_snapshot_node).
    live_join = and_(HistoricalFileNode.snapshot_id == baseline,
                     HistoricalFileNode.node_id == Document.node_id)
    live_where = (Document.is_folder.is_(False), Document.is_deleted.is_(False),
                  Document.workspace_id.not_in(inactive_ws),
                  HistoricalFileNode.id.is_(None))

    def live_agg(*columns):
        return (select(*columns, func.count()).select_from(Document)
                .outerjoin(HistoricalFileNode, live_join).where(*live_where))

    space_totals = collections.Counter()
    for workspace_id, count in db.execute(
            select(HistoricalFileNode.workspace_id, func.count()).where(*base_where)
            .group_by(HistoricalFileNode.workspace_id)):
        space_totals[workspace_id] += count
    for workspace_id, count in db.execute(live_agg(Document.workspace_id)
                                          .group_by(Document.workspace_id)):
        space_totals[workspace_id] += count

    base_month = func.substr(HistoricalFileNode.source_created_at, 1, 7)
    base_day = func.substr(HistoricalFileNode.source_created_at, 1, 10)
    live_month = func.substr(Document.source_created_at, 1, 7)
    live_day = func.substr(Document.source_created_at, 1, 10)
    dated_base = base_where + (func.length(HistoricalFileNode.source_created_at) >= 10,)
    dated_live = (func.length(Document.source_created_at) >= 10,)

    space_months: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for workspace_id, month, count in db.execute(
            select(HistoricalFileNode.workspace_id, base_month, func.count()).where(*dated_base)
            .group_by(HistoricalFileNode.workspace_id, base_month)):
        space_months[workspace_id][month] += count
    for workspace_id, month, count in db.execute(
            live_agg(Document.workspace_id, live_month).where(*dated_live)
            .group_by(Document.workspace_id, live_month)):
        space_months[workspace_id][month] += count

    creator_day = collections.Counter()  # (creator, YYYY-MM-DD) -> files
    for creator, day, count in db.execute(
            select(HistoricalFileNode.creator_user_id, base_day, func.count()).where(*dated_base)
            .group_by(HistoricalFileNode.creator_user_id, base_day)):
        creator_day[(creator or "", day)] += count
    for creator, day, count in db.execute(
            live_agg(Document.uploader_key, live_day).where(*dated_live)
            .group_by(Document.uploader_key, live_day)):
        creator_day[(creator or "", day)] += count

    robots = robot_ids()
    monthly = collections.Counter()
    bulk_by_month = collections.Counter()
    for (creator, day), count in creator_day.items():
        month = day[:7]
        monthly[month] += count
        if creator in robots or count >= PERSON_DAY_BULK_MIN:
            bulk_by_month[month] += count

    return {
        "total_files": sum(space_totals.values()),
        "monthly": dict(monthly),
        "bulk_by_month": dict(bulk_by_month),
        "creator_day": dict(creator_day),
        "robots": robots,
        "space_totals": dict(space_totals),
        "space_months": {ws: dict(months) for ws, months in space_months.items()},
    }


def invalidate_cache() -> None:
    """Drop the cached aggregate INCLUDING the stale-serve value — tests and
    admin actions that must observe fresh numbers call this; a bare stamp
    reset would still serve the stale value under stale-while-revalidate."""
    _cache.update(stamp=None, at=0.0, value=None)


_refresh_lock = threading.Lock()


def _refresh_in_background() -> None:
    from .db import SessionLocal
    try:
        with SessionLocal() as db:
            stamp = _change_stamp(db)
            value = _collect(db)
        _cache.update(stamp=stamp, at=time.monotonic(), value=value)
    finally:
        _refresh_lock.release()


def collected(db: Session) -> dict[str, Any]:
    """Stale-while-revalidate：请求路径只读缓存，过期时返回旧值并由后台线程
    刷新——任何页面都不因指标重算而卡住。冷启动的第一次同步计算（SQL 聚合
    后为亚秒级）。

    TTL 先判、变更戳后判：`_change_stamp` 本身是两次 COUNT(*) 全表扫描（基线
    表约 28 万行），而它原先跑在每一次读缓存之前——守卫比它保护的缓存还贵，
    单次总览请求要为此扫 6 次表。命中窗口内现在完全不发 SQL。

    可观测行为不变：戳变化时旧逻辑同样只是"触发后台刷新 + 继续返回旧值"，
    差别仅是刷新时机推迟到 TTL 边界。"""
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["value"]
    stamp = _change_stamp(db)
    if _cache["value"] is not None:
        if _cache["stamp"] == stamp:  # 没变：续期，不重算
            _cache["at"] = now
            return _cache["value"]
        if _refresh_lock.acquire(blocking=False):
            threading.Thread(target=_refresh_in_background, daemon=True).start()
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
        rows.append({"month": month, "total": total, "bulk_import": bulk, "routine": total - bulk})
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
        "bulk_day_rule": f"批量口径：数字员工导入全量计批量；人工同一人单日 ≥{PERSON_DAY_BULK_MIN} 份计批量",
        "metric_note": "全量为准；批量导入/日常仅拆分构成，不做扣减。月份按钉钉 createTime（Asia/Shanghai）归属。",
        "baseline": context,
        "caveats": [
            "下限口径：扫描前已删除的文件不可观测。",
            "覆盖服务身份已加入的知识库。",
            "createTime 是知识库入库时间，不是原文件的创建时间。",
        ],
    }


def increments_tree(db: Session, year: str = "", month: str = "", department: str = "",
                    biz_group: str = "", person: str = "") -> dict[str, Any]:
    """Year -> month -> day drill over the cached creator-day counters — no
    extra SQL beyond the shared 60-second collect cache (plus one small
    employee_map read when a people filter is active)."""
    data = collected(db)
    robots = data["robots"]
    creators: set[str] | None = None
    filter_label = ""
    if department or biz_group or person:
        rows = db.scalars(select(EmployeeMap)).all()
        picked = [row for row in rows
                  if (not department or department in (row.department_name or ""))
                  and (not biz_group or biz_group in (row.biz_group_name or ""))
                  and (not person or person in (row.name or "") or person == row.user_id)]
        creators = {row.user_id for row in picked}
        parts = [part for part in (department, biz_group, person) if part]
        filter_label = " / ".join(parts) + f"（{len(picked)} 人）"
    buckets: dict[str, list[int]] = {}
    for (creator, day), count in data["creator_day"].items():
        if creators is not None and creator not in creators:
            continue
        if month:
            if not day.startswith(month):
                continue
            key = day
        elif year:
            if not day.startswith(year):
                continue
            key = day[:7]
        else:
            key = day[:4]
        bucket = buckets.setdefault(key, [0, 0])
        bucket[0] += count
        if creator in robots or count >= PERSON_DAY_BULK_MIN:
            bucket[1] += count
    level = "day" if month else ("month" if year else "year")
    keys = sorted(buckets, reverse=(level == "year"))  # recent years on top
    return {"level": level, "year": year, "month": month, "filter_label": filter_label,
            "person_day_bulk_min": PERSON_DAY_BULK_MIN,
            "rows": [{"key": key, "total": buckets[key][0], "bulk": buckets[key][1],
                      "routine": buckets[key][0] - buckets[key][1]} for key in keys]}


def _scan_status(workspace_id: str, space_totals: dict, live_counts: dict, excluded) -> str:
    """扫描状态判定的唯一实现——完整清单（coverage）和只要四个计数的注册表页
    （coverage_summary）共用，口径不会各自漂移。"""
    if workspace_id in excluded:
        return "excluded"
    if space_totals.get(workspace_id, 0) or live_counts.get(workspace_id):
        return "scanned"
    return "empty"


def _live_workspace_counts(db: Session) -> dict[str, int]:
    return {row[0]: row[1] for row in db.execute(
        select(Document.workspace_id, func.count()).where(Document.is_folder.is_(False), Document.is_deleted.is_(False))
        .group_by(Document.workspace_id))}


def coverage_summary(db: Session, context: dict[str, Any] | None = None) -> dict[str, int]:
    """只算四个计数。知识库管理页此前为了这四个整数调用完整的 coverage()，
    连带构造每个知识库的完整明细再整包丢掉。

    调用方已经取过 snapshot_context 就传进来——它背后是"读出全部快照行连同
    完整 definition JSON"，一次请求里不该跑两遍。"""
    data = collected(db)
    context = context if context is not None else snapshot_context(db)
    excluded = {item.get("workspace_id") for item in context["definition"].get("excluded_workspaces", [])}
    live_counts = _live_workspace_counts(db)
    summary = {"visible_workspaces": 0, "scanned": 0, "empty": 0, "excluded": 0}
    for workspace_id in db.scalars(select(Workspace.workspace_id)):
        summary["visible_workspaces"] += 1
        summary[_scan_status(workspace_id, data["space_totals"], live_counts, excluded)] += 1
    return summary


def coverage(db: Session) -> dict[str, Any]:
    data = collected(db)
    context = snapshot_context(db)
    definition = context["definition"]
    excluded = {item.get("workspace_id"): item for item in definition.get("excluded_workspaces", [])}
    live_counts = _live_workspace_counts(db)
    items = []
    for ws in db.scalars(select(Workspace).order_by(Workspace.name)).all():
        baseline_count = data["space_totals"].get(ws.workspace_id, 0)
        status = _scan_status(ws.workspace_id, data["space_totals"], live_counts, excluded)
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
    yearly: dict[str, int] = {}
    for m, c in rows:
        yearly[m[:4]] = yearly.get(m[:4], 0) + int(c)
    return {"snapshot_id": snapshot, "months": [{"month": m, "total": int(c)} for m, c in rows],
            "yearly": {y: yearly[y] for y in sorted(yearly)},
            "total_files": sum(yearly.values()),
            "uploader_count": uploader_count, "workspace_count": space_count}


def uploader_breakdown(db: Session, user_id: str, year: str = "", month: str = "") -> dict[str, Any]:
    """One person's uploads: monthly series, per-day series for a chosen month,
    and workspace distribution for the chosen period. Day granularity reads the
    node table through the (snapshot, creator) index — a few thousand rows at
    most for one person."""
    snapshot = uploader_snapshot_id(db)
    period = month or year
    months = db.execute(select(UploaderMonthStat.month, func.sum(UploaderMonthStat.file_count))
                        .where(UploaderMonthStat.snapshot_id == snapshot, UploaderMonthStat.creator_user_id == user_id)
                        .group_by(UploaderMonthStat.month).order_by(UploaderMonthStat.month)).all()
    ws_stmt = select(UploaderMonthStat.workspace_name, func.sum(UploaderMonthStat.file_count)) \
        .where(UploaderMonthStat.snapshot_id == snapshot, UploaderMonthStat.creator_user_id == user_id)
    if period:
        ws_stmt = ws_stmt.where(UploaderMonthStat.month.startswith(period))
    workspaces = db.execute(ws_stmt.group_by(UploaderMonthStat.workspace_name)
                            .order_by(func.sum(UploaderMonthStat.file_count).desc()).limit(30)).all()
    days: list[dict[str, Any]] = []
    if month:
        day_rows = db.execute(select(func.substr(HistoricalFileNode.source_created_at, 1, 10), func.count())
                              .where(HistoricalFileNode.snapshot_id == snapshot,
                                     HistoricalFileNode.creator_user_id == user_id,
                                     HistoricalFileNode.node_type == "file",
                                     HistoricalFileNode.source_created_at.startswith(month))
                              .group_by(func.substr(HistoricalFileNode.source_created_at, 1, 10))).all()
        days = [{"day": d, "count": int(c)} for d, c in sorted(day_rows)]
    employee = _employee_rows(db, [user_id]).get(user_id)
    period_total = sum(int(c) for m, c in months if not period or m.startswith(period))
    return {**_person(user_id, employee), "year": year, "month": month,
            "months": [{"month": m, "count": int(c)} for m, c in months],
            "days": days,
            "workspaces": [{"name": n or "(未知库)", "files": int(c)} for n, c in workspaces],
            "period_total": period_total,
            "all_total": sum(int(c) for _, c in months)}


def org_rollup(db: Session, year: str = "", month: str = "") -> dict[str, Any]:
    """Department -> business-group -> person tree with stock (all-time) and
    period delta. Built from the small aggregate table plus the identity cache."""
    snapshot = uploader_snapshot_id(db)
    period = month or year
    rows = db.execute(select(UploaderMonthStat.creator_user_id, UploaderMonthStat.month, func.sum(UploaderMonthStat.file_count))
                      .where(UploaderMonthStat.snapshot_id == snapshot)
                      .group_by(UploaderMonthStat.creator_user_id, UploaderMonthStat.month)).all()
    employees = _employee_rows(db, list({r[0] for r in rows}))
    departments: dict[str, dict[str, Any]] = {}
    for user_id, m, files in rows:
        person = _person(user_id, employees.get(user_id))
        dept = departments.setdefault(person["department_name"], {
            "department_name": person["department_name"], "stock": 0, "delta": 0,
            "uploaders": set(), "is_robot": person.get("is_robot", False), "groups": {}})
        group = dept["groups"].setdefault(person["biz_group_name"], {
            "biz_group_name": person["biz_group_name"], "stock": 0, "delta": 0, "uploaders": set(), "people": {}})
        people = group["people"].setdefault(user_id, {"user_id": user_id, "name": person["name"] or user_id,
                                                      "matched": person["matched"], "stock": 0, "delta": 0})
        count = int(files)
        in_period = (not period) or m.startswith(period)
        for bucket in (dept, group):
            bucket["stock"] += count
            if in_period:
                bucket["delta"] += count
            bucket["uploaders"].add(user_id)
        people["stock"] += count
        if in_period:
            people["delta"] += count
    items = []
    for dept in departments.values():
        groups = []
        for group in dept["groups"].values():
            people = sorted(group["people"].values(), key=lambda p: (-p["delta"], -p["stock"]))
            groups.append({"biz_group_name": group["biz_group_name"], "stock": group["stock"],
                           "delta": group["delta"], "uploaders": len(group["uploaders"]),
                           "people": people})
        groups.sort(key=lambda g: (-g["delta"], -g["stock"]))
        items.append({"department_name": dept["department_name"], "stock": dept["stock"], "delta": dept["delta"],
                      "uploaders": len(dept["uploaders"]), "is_robot": dept["is_robot"], "groups": groups})
    items.sort(key=lambda d: (-d["delta"], -d["stock"]))
    return {"snapshot_id": snapshot, "year": year, "month": month, "items": items}


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
