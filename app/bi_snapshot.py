"""A single local read view for all aggregate dimensions; no HTTP or bodies."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import func, select
from .bi_push_contract import BUCKETS, Q_COUNTS, content_hash, empty_quality, utc_text, validate_snapshot
from .db import AuditState, FileAuditEvent, ScanCycle, ScanTask, utcnow
from .service import review_excluded_levels

CST = timezone(timedelta(hours=8))


class SnapshotUnavailable(RuntimeError):
    pass


def local_day(value):
    """Persisted source values: offset ISO -> CST; naive ISO/date -> CST."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        if len(value) < 10 or value[4] != "-" or value[7] != "-":
            return ""
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        d = d.replace(tzinfo=CST) if d.tzinfo is None else d.astimezone(CST)
        return d.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return ""


def add_quality(target, other):
    for k in Q_COUNTS:
        target[k] += other[k]
    target["scoreSum"] = format(Decimal(target["scoreSum"]) + Decimal(other["scoreSum"]), ".4f")


def _instant(ms):
    return utc_text(datetime.fromtimestamp(ms / 1000, timezone.utc)) if ms else None


def _collection(db, now):
    audit = db.get(AuditState, 1)
    month = now.astimezone(CST).strftime("%Y-%m")
    cycle = db.get(ScanCycle, month)
    tasks = db.scalars(select(ScanTask).where(ScanTask.month == month, ScanTask.recovery_id == cycle.recovery_id)).all() if cycle and cycle.recovery_id else (db.scalars(select(ScanTask).where(ScanTask.month == month)).all() if cycle else [])
    # Only a full, successful cycle advances the complete-calibration time.
    last = db.scalar(select(func.max(ScanCycle.finished_at)).where(ScanCycle.status == "completed"))
    return {
        "auditCompletedThrough": _instant(audit.completed_end_ms) if audit else None,
        "lastAuditSuccessAt": utc_text(audit.last_run_at) if audit and audit.last_run_at else None,
        "calibrationMonth": month if cycle else None,
        "calibrationStatus": cycle.status if cycle else "not_started",
        "lastCompleteCalibrationAt": utc_text(last) if last else None,
        "remainingWorkspaceCount": sum(t.status in {"pending", "running"} for t in tasks),
        "failedWorkspaceCount": sum(t.status == "dead_letter" for t in tasks),
        "unresolvedEventCount": db.scalar(select(func.count()).select_from(FileAuditEvent).where(FileAuditEvent.processed.is_(False))) or 0,
        "gapSince": _instant(audit.gap_since_ms) if audit else None,
        "inventoryReconciledAt": utc_text(audit.inventory_reconciled_at) if audit and audit.inventory_reconciled_at else None,
    }


def build_snapshot(inputs, identities, settings, versions, allow_empty=False):
    now, months = inputs["now"], inputs["months"]
    current = now.astimezone(CST).strftime("%Y-%m")
    groups, qualities = inputs["groups"], inputs["quality"]
    robots = {x.strip() for x in settings.robot_user_ids.split(",") if x.strip()}
    workspaces = {w: {"workspaceId": w, "workspaceName": n, "fileCount": 0, "currentMonthNewFileCount": 0, **empty_quality()} for w, n in inputs["names"].items()}
    monthly = {m: {"month": m, "uploadedFileCount": 0, "routineUploadedFileCount": 0, "bulkUploadedFileCount": 0, **empty_quality()} for m in months}
    diagnostics = {m: {"month": m, **{k: 0 for k in BUCKETS}} for m in months}
    employees, workspace_sets = {}, defaultdict(set)
    summary = {"workspaceCount": len(workspaces), "totalFileCount": 0, "currentMonthNewFileCount": 0, "undatedFileCount": 0, **empty_quality(), "currentMonthQuality": empty_quality()}
    person_day = Counter()
    for (_, _, source, day), count in groups.items():
        if day:
            person_day[source, day] += count

    def identity(month, kind, source):
        r = identities.get((month, kind, source))
        if source in robots or (r and r.employee_key in robots):
            return None, "robotExcludedFileCount"
        if r and r.resolve_status == "matched" and r.include_official and r.employee_key:
            return r.employee_key, "officialUploadedFileCount"
        if r and r.resolve_status in {"inactive", "ignored", "unassigned", "conflict"}:
            return None, "policyExcludedFileCount"
        return None, "unknownIdentityFileCount" if not source or kind == "unknown" else "unmatchedIdentityFileCount"

    def employee(month, key):
        return employees.setdefault((month, key), {"month": month, "employeeKey": key, "uploadedFileCount": 0, "workspaceCount": 0, **empty_quality()})

    for (ws, kind, source, day), count in groups.items():
        workspaces[ws]["fileCount"] += count
        summary["totalFileCount"] += count
        if not day:
            summary["undatedFileCount"] += count
            continue
        m = day[:7]
        if m == current:
            summary["currentMonthNewFileCount"] += count
            workspaces[ws]["currentMonthNewFileCount"] += count
        if m not in monthly:
            continue
        monthly[m]["uploadedFileCount"] += count
        key, bucket = identity(m, kind, source)
        bulk = bucket == "robotExcludedFileCount" or bool(source) and person_day[source, day] >= 200
        monthly[m]["bulkUploadedFileCount" if bulk else "routineUploadedFileCount"] += count
        diagnostics[m][bucket] += count
        if key:
            employee(m, key)["uploadedFileCount"] += count
            workspace_sets[m, key].add(ws)
    for (ws, kind, source, m), q in qualities.items():
        add_quality(summary, q)
        add_quality(workspaces[ws], q)
        if m == current:
            add_quality(summary["currentMonthQuality"], q)
        if m in monthly:
            add_quality(monthly[m], q)
            key, _ = identity(m, kind, source)
            if key:
                add_quality(employee(m, key), q)
    for key, row in employees.items():
        row["workspaceCount"] = len(workspace_sets[key])
    v = {"contractVersion": 1, "sourceId": settings.bi_push_source_id, "snapshotId": str(uuid4()), "sequence": "1",
         "generatedAt": utc_text(utcnow()), "metadataReadAt": utc_text(now), "contentHash": "",
         "scope": {"timezone": "Asia/Shanghai", "metricVersion": "kg-bi-v3", "maxMonths": 24, "currentMonth": current,
                   "baselineId": inputs["baseline"], "excludedWorkspaceLevels": sorted(review_excluded_levels(settings)),
                   "ignoredPrefixes": sorted({p.strip() for p in settings.ignored_workspace_prefixes.split(",") if p.strip()}),
                   "baselineAttribution": "baseline_wins", "allowEmpty": allow_empty},
         "collection": inputs["collection"], "identityVersions": [versions[m] for m in months],
         "data": {"summary": summary, "monthly": [monthly[m] for m in months],
                  "workspaces": [workspaces[w] for w in sorted(workspaces)], "employees": [employees[k] for k in sorted(employees)],
                  "diagnostics": {"undatedFileCount": summary["undatedFileCount"], "unconfirmedWorkspaceCount": inputs["unconfirmed"], "monthly": [diagnostics[m] for m in months]}}}
    v["contentHash"] = content_hash(v)
    validate_snapshot(v)
    return v
