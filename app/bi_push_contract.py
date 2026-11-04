"""Shared, aggregate-only KG push v1 contract (identical in both services)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

MAX_BYTES = 16 * 1024 * 1024
SOURCE_ID = "kg-internal-production"
Q_COUNTS = ("reviewedDocumentCount", "scoredDocumentCount", "passCount", "manualReviewCount", "returnCount")
Q_KEYS = set(Q_COUNTS) | {"scoreSum"}
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CALIBRATION = {"not_started", "running", "completed", "completed_with_errors", "budget_exhausted", "expired", "unconfigured", "no_targets"}
TOP = {"contractVersion", "sourceId", "snapshotId", "sequence", "generatedAt", "metadataReadAt", "contentHash", "scope", "collection", "identityVersions", "data"}
BUCKETS = ("officialUploadedFileCount", "robotExcludedFileCount", "unknownIdentityFileCount", "unmatchedIdentityFileCount", "policyExcludedFileCount")


class ContractError(ValueError):
    def __init__(self, path="snapshot", code="invalid_snapshot"):
        self.path, self.code = path, code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value):
    return hashlib.sha256(canonical({k: value[k] for k in ("contractVersion", "sourceId", "scope", "collection", "identityVersions", "data")})).hexdigest()


def empty_quality():
    return {**{k: 0 for k in Q_COUNTS}, "scoreSum": "0.0000"}


def mean_score(row):
    n = row["scoredDocumentCount"]
    return float((Decimal(row["scoreSum"]) / n).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if n else None


def utc_text(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def envelope(data, code="status"):
    return {"ok": True, "code": code, "data": data, "error": None}


def failure(code, paths=(), accepted=None, retry=None, state=None):
    return {"ok": False, "code": code, "data": None, "error": {"fieldPaths": list(paths)[:20], "acceptedSequence": accepted, "retryAfterSeconds": retry, "stateHash": state}}


def _require(condition, path):
    if not condition:
        raise ContractError(path)


def _object(value, keys, path):
    _require(type(value) is dict and set(value) == set(keys), path)


def _text(value, path, limit=128):
    _require(type(value) is str and 0 < len(value) <= limit, path)


def _count(value, path):
    _require(type(value) is int and 0 <= value <= 9007199254740991, path)


def _month(value, path):
    _require(type(value) is str and bool(MONTH.fullmatch(value)), path)


def _time(value, path, now, nullable=False):
    if nullable and value is None:
        return
    _text(value, path, 40)
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(value.endswith("Z") and d.tzinfo is not None and d <= now + timedelta(minutes=5), path)
    except (ValueError, OverflowError):
        raise ContractError(path) from None


def _quality(row, path):
    for k in Q_COUNTS:
        _count(row[k], path + "." + k)
    s = row["scoreSum"]
    _require(type(s) is str and bool(re.fullmatch(r"(?:0|[1-9]\d*)\.\d{4}", s)), path + ".scoreSum")
    _require(row["scoredDocumentCount"] <= row["reviewedDocumentCount"], path)
    _require(sum(row[k] for k in ("passCount", "manualReviewCount", "returnCount")) <= row["reviewedDocumentCount"], path)
    _require(0 <= Decimal(s) <= 100 * row["scoredDocumentCount"], path + ".scoreSum")


def _rows(value, keys, row_keys, path):
    _require(type(value) is list, path)
    previous = None
    for i, row in enumerate(value):
        p = f"{path}[{i}]"
        _object(row, keys, p)
        key = tuple(row[k] for k in row_keys)
        for k, v in zip(row_keys, key):
            _text(v, p + "." + k, 160 if k == "key" else 128)
        _require(previous is None or previous < key, p)
        previous = key


def validate_snapshot(v, now=None):
    now = now or datetime.now(timezone.utc)
    _object(v, TOP, "snapshot")
    _require(type(v["contractVersion"]) is int and v["contractVersion"] == 1, "contractVersion")
    _require(v["sourceId"] == SOURCE_ID, "sourceId")
    try:
        _require(str(UUID(v["snapshotId"])) == v["snapshotId"], "snapshotId")
    except (ValueError, TypeError, AttributeError):
        raise ContractError("snapshotId") from None
    seq = v["sequence"]
    _require(type(seq) is str and bool(re.fullmatch(r"[1-9]\d{0,19}", seq)) and int(seq) <= 18446744073709551615, "sequence")
    for k in ("generatedAt", "metadataReadAt"):
        _time(v[k], k, now)
    scope = v["scope"]
    _object(scope, {"timezone", "metricVersion", "maxMonths", "currentMonth", "baselineId", "excludedWorkspaceLevels", "ignoredPrefixes", "baselineAttribution", "allowEmpty"}, "scope")
    _require(scope["timezone"] == "Asia/Shanghai" and scope["metricVersion"] == "kg-bi-v3" and type(scope["maxMonths"]) is int and scope["maxMonths"] == 24 and scope["baselineAttribution"] == "baseline_wins", "scope")
    _month(scope["currentMonth"], "scope.currentMonth")
    _text(scope["baselineId"], "scope.baselineId", 64)
    _require(type(scope["allowEmpty"]) is bool, "scope.allowEmpty")
    levels = scope["excludedWorkspaceLevels"]
    _require(type(levels) is list and all(x in {"C", "D", "P", "I"} for x in levels) and levels == sorted(set(levels)) and "I" in levels, "scope.excludedWorkspaceLevels")
    prefixes = scope["ignoredPrefixes"]
    _require(type(prefixes) is list, "scope.ignoredPrefixes")
    for p in prefixes:
        _text(p, "scope.ignoredPrefixes")
    _require(prefixes == sorted(set(prefixes)), "scope.ignoredPrefixes")
    c = v["collection"]
    dates = {"auditCompletedThrough", "lastAuditSuccessAt", "lastCompleteCalibrationAt", "gapSince", "inventoryReconciledAt"}
    counts = {"remainingWorkspaceCount", "failedWorkspaceCount", "unresolvedEventCount"}
    _object(c, dates | counts | {"calibrationMonth", "calibrationStatus"}, "collection")
    for k in dates:
        _time(c[k], "collection." + k, now, True)
    for k in counts:
        _count(c[k], "collection." + k)
    if c["calibrationMonth"] is not None:
        _month(c["calibrationMonth"], "collection.calibrationMonth")
    _require(c["calibrationStatus"] in CALIBRATION, "collection.calibrationStatus")
    d = v["data"]
    _object(d, {"summary", "monthly", "workspaces", "employees", "diagnostics"}, "data")
    s = d["summary"]
    sc = {"workspaceCount", "totalFileCount", "currentMonthNewFileCount", "undatedFileCount"}
    _object(s, sc | Q_KEYS | {"currentMonthQuality"}, "data.summary")
    for k in sc:
        _count(s[k], "data.summary." + k)
    _quality(s, "data.summary")
    _object(s["currentMonthQuality"], Q_KEYS, "data.summary.currentMonthQuality")
    _quality(s["currentMonthQuality"], "data.summary.currentMonthQuality")
    mc = {"uploadedFileCount", "routineUploadedFileCount", "bulkUploadedFileCount"}
    _rows(d["monthly"], {"month"} | mc | Q_KEYS, ("month",), "data.monthly")
    _require(len(d["monthly"]) <= 24, "data.monthly")
    for r in d["monthly"]:
        _month(r["month"], "data.monthly.month")
        for k in mc:
            _count(r[k], "data.monthly." + k)
        _quality(r, "data.monthly")
        _require(r["routineUploadedFileCount"] + r["bulkUploadedFileCount"] == r["uploadedFileCount"], "data.monthly")
    wc = {"fileCount", "currentMonthNewFileCount"}
    _rows(d["workspaces"], {"workspaceId", "workspaceName"} | wc | Q_KEYS, ("workspaceId",), "data.workspaces")
    for r in d["workspaces"]:
        _text(r["workspaceName"], "data.workspaces.workspaceName", 255)
        _require(not re.match(r"^\s*I[-－]", r["workspaceName"], re.I), "data.workspaces.workspaceName")
        _require(not any(r["workspaceName"].startswith(p) for p in prefixes), "data.workspaces.workspaceName")
        for k in wc:
            _count(r[k], "data.workspaces." + k)
        _quality(r, "data.workspaces")
    _require(len(d["workspaces"]) == s["workspaceCount"] and sum(r["fileCount"] for r in d["workspaces"]) == s["totalFileCount"] and sum(r["currentMonthNewFileCount"] for r in d["workspaces"]) == s["currentMonthNewFileCount"], "data.workspaces")
    for k in Q_KEYS:
        _require(sum(Decimal(r[k]) for r in d["workspaces"]) == Decimal(s[k]), "data.workspaces." + k)
    ec = {"uploadedFileCount", "workspaceCount"}
    _rows(d["employees"], {"month", "employeeKey"} | ec | Q_KEYS, ("month", "employeeKey"), "data.employees")
    months = {r["month"] for r in d["monthly"]}
    for r in d["employees"]:
        _require(r["month"] in months, "data.employees.month")
        for k in ec:
            _count(r[k], "data.employees." + k)
        _quality(r, "data.employees")
    _rows(v["identityVersions"], {"month", "directoryVersion", "policyVersion", "resolvedSnapshotMonth"}, ("month",), "identityVersions")
    _require({r["month"] for r in v["identityVersions"]} == months, "identityVersions")
    for r in v["identityVersions"]:
        for k in ("directoryVersion", "policyVersion"):
            _text(r[k], "identityVersions." + k)
        _month(r["resolvedSnapshotMonth"], "identityVersions.resolvedSnapshotMonth")
    diag = d["diagnostics"]
    _object(diag, {"undatedFileCount", "unconfirmedWorkspaceCount", "monthly"}, "data.diagnostics")
    for k in ("undatedFileCount", "unconfirmedWorkspaceCount"):
        _count(diag[k], "data.diagnostics." + k)
    _require(diag["undatedFileCount"] == s["undatedFileCount"], "data.diagnostics.undatedFileCount")
    _rows(diag["monthly"], {"month"} | set(BUCKETS), ("month",), "data.diagnostics.monthly")
    _require({r["month"] for r in diag["monthly"]} == months, "data.diagnostics.monthly")
    by_month = {r["month"]: r for r in diag["monthly"]}
    for r in d["monthly"]:
        b = by_month[r["month"]]
        for k in BUCKETS:
            _count(b[k], "data.diagnostics.monthly." + k)
        _require(sum(b[k] for k in BUCKETS) == r["uploadedFileCount"], "data.diagnostics.monthly")
        _require(sum(e["uploadedFileCount"] for e in d["employees"] if e["month"] == r["month"]) == b["officialUploadedFileCount"], "data.employees.uploadedFileCount")
    _require(type(v["contentHash"]) is str and bool(re.fullmatch(r"[a-f0-9]{64}", v["contentHash"])) and v["contentHash"] == content_hash(v), "contentHash")
    _require(len(canonical(v)) <= MAX_BYTES, "snapshot.size")
    return v


def split_snapshot(v):
    """Only aggregate partitions, never a second copy of document metadata."""
    d = v["data"]
    parts = {"summary": d["summary"], "meta": {k: v[k] for k in ("scope", "collection", "identityVersions", "metadataReadAt", "generatedAt")}}
    parts["meta"]["unconfirmedWorkspaceCount"] = d["diagnostics"]["unconfirmedWorkspaceCount"]
    for r in d["workspaces"]:
        parts["workspace/" + r["workspaceId"]] = r
    diagnostics = {r["month"]: r for r in d["diagnostics"]["monthly"]}
    versions = {r["month"]: r for r in v["identityVersions"]}
    for r in d["monthly"]:
        m = r["month"]
        parts["month/" + m] = {"monthly": r, "employees": [e for e in d["employees"] if e["month"] == m],
                                "diagnostics": diagnostics[m], "identityVersion": versions[m]}
    return parts


def partition_manifest(parts):
    return {key: hashlib.sha256(canonical(value)).hexdigest() for key, value in sorted(parts.items())}


def state_hash(manifest):
    return hashlib.sha256(canonical(manifest)).hexdigest()


def materialize_parts(parts, snapshot_id, sequence):
    try:
        return _materialize_parts(parts, snapshot_id, sequence)
    except ContractError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, RecursionError):
        raise ContractError('changes') from None


def _materialize_parts(parts, snapshot_id, sequence):
    _require("summary" in parts and "meta" in parts, "changes")
    meta = parts["meta"]
    _object(meta, {"scope", "collection", "identityVersions", "metadataReadAt", "generatedAt", "unconfirmedWorkspaceCount"}, "partition.meta")
    months, workspaces, employees, diagnostics, versions = [], [], [], [], []
    for key, value in sorted(parts.items()):
        _partition_key(key)
        if key.startswith("workspace/"):
            _require(type(value) is dict and value.get("workspaceId") == key[10:], "partition.workspace")
            workspaces.append(value)
        elif key.startswith("month/"):
            _object(value, {"monthly", "employees", "diagnostics", "identityVersion"}, "partition.month")
            _require(all(type(value[k]) is dict for k in ("monthly", "diagnostics", "identityVersion")), "partition.month")
            m = key[6:]
            _require(value["monthly"].get("month") == m and value["diagnostics"].get("month") == m and value["identityVersion"].get("month") == m, "partition.month")
            _require(type(value["employees"]) is list and all(type(r) is dict and r.get("month") == m for r in value["employees"]), "partition.month.employees")
            months.append(value["monthly"])
            employees.extend(value["employees"])
            diagnostics.append(value["diagnostics"])
            versions.append(value["identityVersion"])
    _require(meta["identityVersions"] == versions, "partition.identityVersions")
    v = {"contractVersion": 1, "sourceId": SOURCE_ID, "snapshotId": snapshot_id, "sequence": str(sequence),
         **{k: meta[k] for k in ("scope", "collection", "identityVersions", "metadataReadAt", "generatedAt")},
         "data": {"summary": parts["summary"], "monthly": months, "workspaces": workspaces, "employees": employees,
                  "diagnostics": {"undatedFileCount": parts["summary"]["undatedFileCount"],
                                  "unconfirmedWorkspaceCount": meta["unconfirmedWorkspaceCount"], "monthly": diagnostics}}}
    v["contentHash"] = content_hash(v)
    return validate_snapshot(v)


def _partition_key(key):
    _require(type(key) is str and 0 < len(key) <= 160 and (key in {"summary", "meta"}
             or (key.startswith("workspace/") and 0 < len(key[10:]) <= 128)
             or (key.startswith("month/") and MONTH.fullmatch(key[6:]))), "changes.key")


def delta_hash(v):
    return hashlib.sha256(canonical({k: v[k] for k in ("contractVersion", "sourceId", "baseSequence", "resultHash", "changes")})).hexdigest()


def validate_delta(v, now=None):
    _object(v, {"contractVersion", "sourceId", "snapshotId", "sequence", "baseSequence", "generatedAt", "contentHash", "resultHash", "changes"}, "delta")
    _require(type(v["contractVersion"]) is int and v["contractVersion"] == 2 and v["sourceId"] == SOURCE_ID, "contractVersion")
    try:
        _require(str(UUID(v["snapshotId"])) == v["snapshotId"], "snapshotId")
    except (ValueError, TypeError, AttributeError):
        raise ContractError("snapshotId") from None
    for k in ("sequence", "baseSequence"):
        value = v[k]
        _require(type(value) is str and bool(re.fullmatch(r"0|[1-9]\d{0,19}", value)) and int(value) <= 18446744073709551615, k)
    _require(int(v["sequence"]) > int(v["baseSequence"]), "sequence")
    _time(v["generatedAt"], "generatedAt", now or datetime.now(timezone.utc))
    for k in ("contentHash", "resultHash"):
        _require(type(v[k]) is str and bool(re.fullmatch(r"[a-f0-9]{64}", v[k])), k)
    changes = v["changes"]
    _object(changes, {"upserts", "deletes"}, "changes")
    _rows(changes["upserts"], {"key", "value"}, ("key",), "changes.upserts")
    _require(type(changes["deletes"]) is list, "changes.deletes")
    upserts = {r["key"] for r in changes["upserts"]}
    for k in list(upserts) + changes["deletes"]:
        _partition_key(k)
    _require(changes["deletes"] == sorted(set(changes["deletes"])) and not (upserts & set(changes["deletes"])) and not ({"summary", "meta"} & set(changes["deletes"])), "changes.deletes")
    _require(v["contentHash"] == delta_hash(v), "contentHash")
    _require(len(canonical(v)) <= MAX_BYTES, "delta.size")
    return v


def validate_frame(v):
    if type(v) is dict and v.get("contractVersion") == 2:
        return validate_delta(v)
    return validate_snapshot(v)
