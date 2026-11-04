"""Rebuild only dirty aggregate partitions. Never queries source file tables."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import timezone
from decimal import Decimal
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session
from .bi_models import (BIDirtyNode, BIDirtyScope, BIIdentityMonth, BIInventory, BIProjectionState, BIPushState,
                        BIQualityMonth, BIStatPartition, BIUploadDay)
from .bi_projection import Q_MAP, enqueue
from .bi_push_contract import canonical, empty_quality, materialize_parts, partition_manifest, state_hash, utc_text
from .bi_snapshot import CST, SnapshotUnavailable, _collection, add_quality, build_snapshot
from .db import Workspace, utcnow
from .service import review_excluded_levels, workspace_level, workspace_name_is_ignored


def eligible_spaces(db, settings):
    rows = db.scalars(select(Workspace)).all()
    names = {w.workspace_id: w.name for w in rows if w.is_active and w.metadata_confirmed_at is not None
             and not workspace_name_is_ignored(settings, w.name) and workspace_level(w.name) not in review_excluded_levels(settings)}
    return names, sum(w.is_active and w.metadata_confirmed_at is None for w in rows)


def available_months(db, names):
    uploads = set(db.scalars(select(BIUploadDay.month).where(BIUploadDay.workspace_id.in_(names)).distinct()))
    quality = set(db.scalars(select(BIQualityMonth.month).where(BIQualityMonth.workspace_id.in_(names)).distinct()))
    return sorted(uploads | quality)[-24:]


def month_inputs(db, month, names, projection, now, unconfirmed=0):
    ids = set(names)
    groups = Counter()
    for r in db.scalars(select(BIUploadDay).where(BIUploadDay.month == month, BIUploadDay.workspace_id.in_(ids)).execution_options(yield_per=2000)):
        groups[(r.workspace_id, r.identity_kind, r.source_key, r.day)] = r.file_count
    qualities = {}
    for r in db.scalars(select(BIQualityMonth).where(BIQualityMonth.month == month, BIQualityMonth.workspace_id.in_(ids)).execution_options(yield_per=2000)):
        q = {src: format(r.score_sum, ".4f") if src == "scoreSum" else getattr(r, dst) for src, dst in Q_MAP.items()}
        qualities[r.workspace_id, r.identity_kind, r.source_key, month] = q
    return {"names": names, "groups": groups, "quality": qualities, "baseline": projection.baseline_id or "none",
            "months": [month], "now": now, "collection": _collection(db, now), "unconfirmed": unconfirmed}


def identity_keys(inputs):
    month = inputs["months"][0]
    return sorted({(month, kind, source) for (_, kind, source, _) in inputs["groups"] if source}
                  | {(month, kind, source) for (_, kind, source, _) in inputs["quality"] if source})


def assert_partition_read_ready(db):
    if db.info.get("kg_partitions_uncommitted") or any(isinstance(r, BIStatPartition) for r in list(db.new) + list(db.dirty) + list(db.deleted)):
        raise SnapshotUnavailable("partition_commit_required")


def read_partitions(db):
    assert_partition_read_ready(db)
    return {r.partition_key: json.loads(r.payload_json) for r in db.scalars(select(BIStatPartition)).all()}


def _clear_partition_transaction(db):
    db.info.pop("kg_partitions_uncommitted", None)


event.listen(Session, "after_commit", _clear_partition_transaction)
event.listen(Session, "after_rollback", _clear_partition_transaction)


def read_meta(db):
    payload = db.scalar(select(BIStatPartition.payload_json).where(BIStatPartition.partition_key == "meta"))
    return json.loads(payload) if payload else {}


def partition_headers(db):
    return dict(db.execute(select(BIStatPartition.partition_key, BIStatPartition.content_hash)).all())


def scope_value(settings, projection, current, allow_empty=False):
    return {"timezone": "Asia/Shanghai", "metricVersion": "kg-bi-v3", "maxMonths": 24, "currentMonth": current,
            "baselineId": projection.baseline_id or "none", "excludedWorkspaceLevels": sorted(review_excluded_levels(settings)),
            "ignoredPrefixes": sorted({p.strip() for p in settings.ignored_workspace_prefixes.split(",") if p.strip()}),
            "baselineAttribution": "baseline_wins", "allowEmpty": allow_empty}


def dirty_plan(db, settings, now=None):
    now = now or utcnow()
    projection = db.get(BIProjectionState, 1)
    if not projection or not projection.seed_complete:
        raise SnapshotUnavailable("projection_seed_unready")
    state = db.get(BIPushState, 1)
    if not state or state.workspace_registry_ready_at is None:
        raise SnapshotUnavailable("workspace_metadata_unready")
    if db.scalar(select(BIDirtyNode.node_id).limit(1)):
        raise SnapshotUnavailable("projection_pending")
    headers = partition_headers(db)
    scopes = db.execute(select(BIDirtyScope.scope_key, BIDirtyScope.generation).where(~BIDirtyScope.scope_key.like('page_month/%'))).all()
    names, unconfirmed = eligible_spaces(db, settings)
    old_meta = read_meta(db)
    current = now.astimezone(CST).strftime("%Y-%m")
    expected_scope = scope_value(settings, projection, current, old_meta.get("scope", {}).get("allowEmpty", False))
    changed_scope = {k: v for k, v in old_meta.get("scope", {}).items() if k != "currentMonth"} != {k: v for k, v in expected_scope.items() if k != "currentMonth"}
    calendar = old_meta.get("scope", {}).get("currentMonth") != current
    full = not headers or any(r.scope_key == "all" for r in scopes) or changed_scope
    selected = available_months(db, names) if full or scopes else sorted(k[6:] for k in headers if k.startswith("month/"))
    months = set(selected) if full else {r.scope_key[6:] for r in scopes if r.scope_key.startswith("month/") and r.scope_key[6:] in selected}
    months |= set(selected) - {k[6:] for k in headers if k.startswith("month/")}
    workspaces = set(names) if full or calendar else {r.scope_key[10:] for r in scopes if r.scope_key.startswith("workspace/")}
    # A workspace becoming visible/hidden changes every month it contributes to.
    scope_ws = {r.scope_key[9:] for r in scopes if r.scope_key.startswith("registry/")}
    workspaces |= scope_ws
    if scope_ws:
        months |= set(db.scalars(select(BIUploadDay.month).where(BIUploadDay.workspace_id.in_(scope_ws)).distinct())) & set(selected)
        months |= set(db.scalars(select(BIQualityMonth.month).where(BIQualityMonth.workspace_id.in_(scope_ws)).distinct())) & set(selected)
    return {"headers": headers, "meta": old_meta, "scopes": scopes, "names": names, "unconfirmed": unconfirmed, "months": sorted(months),
            "workspaces": workspaces, "selected": selected, "projection": projection, "now": now, "full": full, "calendar": calendar}


def _workspace_rows(db, ids, names, current):
    if not ids:
        return {}
    inventory = {r.workspace_id: r for r in db.scalars(select(BIInventory).where(BIInventory.workspace_id.in_(ids))).all()}
    current_counts = dict(db.execute(select(BIUploadDay.workspace_id, func.sum(BIUploadDay.file_count))
                         .where(BIUploadDay.workspace_id.in_(ids), BIUploadDay.month == current).group_by(BIUploadDay.workspace_id)).all())
    cols = [func.sum(getattr(BIQualityMonth, dst)).label(dst) for dst in Q_MAP.values()]
    qualities = {r.workspace_id: r for r in db.execute(select(BIQualityMonth.workspace_id, *cols)
                 .where(BIQualityMonth.workspace_id.in_(ids)).group_by(BIQualityMonth.workspace_id)).all()}
    result = {}
    for w in ids:
        if w not in names:
            continue
        i, q = inventory.get(w), qualities.get(w)
        row = {"workspaceId": w, "workspaceName": names[w], "fileCount": i.file_count if i else 0,
               "currentMonthNewFileCount": int(current_counts.get(w) or 0), **empty_quality()}
        if q:
            for src, dst in Q_MAP.items():
                row[src] = format(getattr(q, dst) or Decimal(0), ".4f") if src == "scoreSum" else int(getattr(q, dst) or 0)
        result[w] = row
    return result


def publish_partitions(db, settings, versions, now=None, allow_empty=False):
    """No HTTP: all dependent partition updates commit as one generation."""
    plan = dirty_plan(db, settings, now)
    names, projection, now = plan["names"], plan["projection"], plan["now"]
    parts = {}
    current = now.astimezone(CST).strftime("%Y-%m")
    for month in plan["months"]:
        inputs = month_inputs(db, month, names, projection, now, plan["unconfirmed"])
        wanted = identity_keys(inputs)
        cached = db.scalars(select(BIIdentityMonth).where(BIIdentityMonth.month == month)).all()
        identities = {(r.month, r.identity_kind, r.source_key): r for r in cached}
        version = versions[month]
        for key in wanted:
            r = identities.get(key)
            if not r or r.resolve_status in {"error", "snapshot_missing"} or r.directory_version != version["directoryVersion"] or r.policy_version != version["policyVersion"]:
                raise SnapshotUnavailable("identity_unready")
        v = build_snapshot(inputs, identities, settings, {month: version}, allow_empty)
        parts["month/" + month] = {"monthly": v["data"]["monthly"][0], "employees": v["data"]["employees"],
                                   "diagnostics": v["data"]["diagnostics"]["monthly"][0], "identityVersion": version}
    # The retained time window may move; explicit deletions are sent downstream.
    removed = {k for k in plan["headers"] if (k.startswith("month/") and k[6:] not in plan["selected"])
               or (k.startswith("workspace/") and k[10:] not in names)}
    for w, row in _workspace_rows(db, plan["workspaces"], names, current).items():
        parts["workspace/" + w] = row
    if plan["scopes"] or plan["full"] or plan["calendar"] or "summary" not in plan["headers"]:
        existing_ws = {key: json.loads(payload) for key, payload in db.execute(select(BIStatPartition.partition_key, BIStatPartition.payload_json)
                       .where(BIStatPartition.partition_key.like("workspace/%")))}
        existing_ws.update({k: v for k, v in parts.items() if k.startswith("workspace/")})
        rows = [v for k, v in existing_ws.items() if k not in removed]
        summary = {"workspaceCount": len(rows), "totalFileCount": sum(r["fileCount"] for r in rows),
                   "currentMonthNewFileCount": sum(r["currentMonthNewFileCount"] for r in rows), "undatedFileCount": 0,
                   **empty_quality(), "currentMonthQuality": empty_quality()}
        summary["undatedFileCount"] = int(db.scalar(select(func.sum(BIInventory.undated_count)).where(BIInventory.workspace_id.in_(names))) or 0)
        for r in rows:
            add_quality(summary, r)
        cq = db.execute(select(*[func.sum(getattr(BIQualityMonth, dst)).label(dst) for dst in Q_MAP.values()])
                        .where(BIQualityMonth.month == current, BIQualityMonth.workspace_id.in_(names))).one()
        for src, dst in Q_MAP.items():
            summary["currentMonthQuality"][src] = format(getattr(cq, dst) or Decimal(0), ".4f") if src == "scoreSum" else int(getattr(cq, dst) or 0)
        parts["summary"] = summary
    version_map = {r["month"]: r for r in plan["meta"].get("identityVersions", [])}
    version_map.update({m: versions[m] for m in plan["months"]})
    selected_versions = [version_map[m] for m in plan["selected"]]
    prior_empty_allowed = plan["meta"].get("scope", {}).get("allowEmpty", False)
    allow_empty = allow_empty or (prior_empty_allowed and ("summary" not in parts or parts["summary"]["totalFileCount"] == 0))
    meta_core = {"scope": scope_value(settings, projection, current, allow_empty), "collection": _collection(db, now),
                 "identityVersions": selected_versions, "unconfirmedWorkspaceCount": plan["unconfirmed"]}
    old_meta = plan["meta"]
    if plan["scopes"] or plan["full"] or any(old_meta.get(k) != v for k, v in meta_core.items()):
        parts["meta"] = {**meta_core, "metadataReadAt": utc_text(projection.last_projected_at or now), "generatedAt": utc_text(now)}
    manifest = partition_manifest(parts)
    changed = 0
    for key, value in parts.items():
        if plan["headers"].get(key) == manifest[key]:
            continue
        from .bi_projection import _insert_for
        t = BIStatPartition.__table__
        statement = _insert_for(db, t, [{"partition_key": key, "content_hash": manifest[key], "payload_json": canonical(value).decode(), "updated_at": now}])
        incoming = statement.inserted if db.bind.dialect.name == "mysql" else statement.excluded
        fields = {k: incoming[k] for k in ("content_hash", "payload_json", "updated_at")}
        db.execute(statement.on_duplicate_key_update(**fields) if db.bind.dialect.name == "mysql" else statement.on_conflict_do_update(index_elements=["partition_key"], set_=fields))
        plan["headers"][key] = manifest[key]
        changed += 1
    if removed:
        db.execute(delete(BIStatPartition).where(BIStatPartition.partition_key.in_(removed)))
        for key in removed:
            plan["headers"].pop(key, None)
        changed += len(removed)
    for r in plan["scopes"]:
        db.execute(delete(BIDirtyScope).where(BIDirtyScope.scope_key == r.scope_key, BIDirtyScope.generation == r.generation))
    if changed:
        db.info["kg_partitions_uncommitted"] = True
    return {"changedPartitions": changed, "resultHash": state_hash(plan["headers"]), "months": len(plan["months"])}
