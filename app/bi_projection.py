"""Transactional node deltas and small preaggregates; idle never scans source files."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timezone
from decimal import Decimal
from sqlalchemy import delete, event, func, inspect, or_, select, tuple_
from sqlalchemy.orm import Session
from .bi_models import (BIDirtyNode, BIDirtyScope, BIFileFact, BIInventory, BIProjectionState,
                        BIQualityMonth, BIUploadDay)
from .bi_push_contract import canonical, empty_quality
from .bi_snapshot import CST, SnapshotUnavailable, local_day
from .config import get_settings
from .db import Document, HistoricalFileNode, ReviewInstance, Workspace, utcnow
from .metrics import primary_snapshot_id

_installed = False
WATCHED = {"workspace_id", "source_creator_key", "source_creator_kind", "uploader_key", "source_created_at", "is_deleted", "is_folder"}
Q_MAP = {"reviewedDocumentCount": "reviewed_count", "scoredDocumentCount": "scored_count", "passCount": "pass_count", "manualReviewCount": "manual_count", "returnCount": "return_count", "scoreSum": "score_sum"}


def _insert_for(db, table, rows):
    if db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table).values(rows)


def enqueue(db, model, keys):
    keys = sorted(set(keys))
    if not keys:
        return
    table = model.__table__
    pk = list(table.primary_key)[0].name
    requested = utcnow()
    rows = [{pk: key, "generation": 1, "requested_at": requested} for key in keys]
    stmt = _insert_for(db, table, rows)
    values = {"generation": table.c.generation + 1, "requested_at": utcnow()}
    stmt = stmt.on_duplicate_key_update(**values) if db.bind.dialect.name == "mysql" else stmt.on_conflict_do_update(index_elements=[pk], set_=values)
    db.execute(stmt)


def _change_hook(db, *_):
    s = get_settings()
    if not (s.metadata_only or s.bi_push_enabled):
        return
    nodes, scopes = set(), set()
    for obj in list(db.new) + list(db.dirty) + list(db.deleted):
        state = inspect(obj)
        if isinstance(obj, Document) and (obj in db.new or obj in db.deleted or any(state.attrs[k].history.has_changes() for k in WATCHED)):
            if obj.node_id:
                nodes.add(obj.node_id)
        elif isinstance(obj, ReviewInstance) and (obj in db.new or obj in db.deleted):
            nodes.add(obj.node_id)
        elif isinstance(obj, Workspace):
            changed = obj in db.new or obj in db.deleted or any(state.attrs[k].history.has_changes() for k in ("name", "is_active"))
            confirmation = state.attrs.metadata_confirmed_at.history
            changed = changed or (confirmation.has_changes() and (not confirmation.deleted or confirmation.deleted[0] is None))
            if changed and obj.workspace_id:
                scopes.add("registry/" + obj.workspace_id)
    enqueue(db, BIDirtyNode, nodes)
    enqueue(db, BIDirtyScope, scopes)


def install_change_hooks():
    global _installed
    if not _installed:
        event.listen(Session, "before_flush", _change_hook)
        _installed = True


def _counter_upsert(db, model, changes, pk, fields):
    rows = []
    for key, values in changes.items():
        if not any(values[k] for k in fields):
            continue
        row = dict(zip(pk, key))
        row.update(values)
        if model is BIUploadDay:
            row["month"] = row["day"][:7]
        rows.append(row)
    if not rows:
        return
    t = model.__table__
    stmt = _insert_for(db, t, rows)
    incoming = stmt.inserted if db.bind.dialect.name == "mysql" else stmt.excluded
    updates = {k: t.c[k] + incoming[k] for k in fields}
    stmt = stmt.on_duplicate_key_update(**updates) if db.bind.dialect.name == "mysql" else stmt.on_conflict_do_update(index_elements=pk, set_=updates)
    db.execute(stmt)
    keys = [tuple(r[k] for k in pk) for r in rows]
    predicate = tuple_(*(t.c[k] for k in pk)).in_(keys)
    if db.scalar(select(func.count()).select_from(t).where(predicate, or_(*(t.c[k] < 0 for k in fields)))):
        raise SnapshotUnavailable("projection_negative_count")
    if model is BIUploadDay:
        db.execute(delete(t).where(predicate, t.c.file_count == 0))
    elif model is BIQualityMonth:
        db.execute(delete(t).where(predicate, t.c.reviewed_count == 0))


def _projection_rows(db, ids, baseline):
    docs = {r.node_id: r for r in db.scalars(select(Document).where(Document.node_id.in_(ids))).all()}
    bases = {}
    for r in db.execute(select(HistoricalFileNode.node_id, HistoricalFileNode.workspace_id, HistoricalFileNode.creator_user_id,
                 HistoricalFileNode.node_type, HistoricalFileNode.source_created_at, HistoricalFileNode.id)
                 .where(HistoricalFileNode.snapshot_id == baseline, HistoricalFileNode.node_id.in_(ids)).order_by(HistoricalFileNode.id)):
        bases.setdefault(r.node_id, r)
    from .db import HistoricalSnapshot
    b = db.get(HistoricalSnapshot, baseline) if baseline else None
    baseline_kind = (b.definition or {}).get("creator_identity_kind", "unknown") if b else "unknown"
    if baseline_kind not in {"user_id", "union_id"}:
        baseline_kind = "unknown"
    ranked = select(ReviewInstance.node_id, ReviewInstance.ai_score, ReviewInstance.verdict, ReviewInstance.created_at,
                    func.row_number().over(partition_by=ReviewInstance.node_id, order_by=(ReviewInstance.created_at.desc(), ReviewInstance.review_instance_id.desc())).label("rn"))
    ranked = ranked.where(ReviewInstance.node_id.in_(ids), ReviewInstance.review_scope == "full_content").subquery()
    qualities = {r.node_id: r for r in db.execute(select(ranked).where(ranked.c.rn == 1))}
    result = {}
    for node in ids:
        d, base = docs.get(node), bases.get(node)
        if (d and d.is_deleted) or (base and str(base.node_type).lower() == "folder") or (not base and (not d or d.is_folder)):
            result[node] = None
            continue
        if base:
            ws, creator, kind, created = base.workspace_id, base.creator_user_id or "", baseline_kind, base.source_created_at
            if kind == "unknown" and d and d.source_creator_key == creator and d.source_creator_kind in {"user_id", "union_id"}:
                kind = d.source_creator_kind
        else:
            ws, creator, kind, created = d.workspace_id, d.source_creator_key or d.uploader_key or "", d.source_creator_kind or "unknown", d.source_created_at
        kind = kind if kind in {"user_id", "union_id"} else "unknown"
        row = {"workspaceId": ws, "creator": creator, "kind": kind, "day": local_day(created), "quality": None}
        q = qualities.get(node)
        if q:
            fields = empty_quality()
            fields["reviewedDocumentCount"] = 1
            if q.ai_score is not None:
                score = Decimal(str(q.ai_score))
                if score.is_finite() and 0 <= score <= 100:
                    fields.update(scoredDocumentCount=1, scoreSum=format(score.quantize(Decimal("0.0001")), ".4f"))
            key = {"pass": "passCount", "manual_review": "manualReviewCount", "return": "returnCount"}.get(q.verdict)
            if key:
                fields[key] = 1
            when = q.created_at.replace(tzinfo=q.created_at.tzinfo or timezone.utc).astimezone(CST)
            row["quality"] = {"month": when.strftime("%Y-%m"), **fields}
        result[node] = row
    return result


def apply_projections(db, projections):
    if not projections:
        return 0
    old = {r.node_id: r for r in db.scalars(select(BIFileFact).where(BIFileFact.node_id.in_(projections))).all()}
    days = defaultdict(lambda: {"file_count": 0})
    inventory = defaultdict(lambda: {"file_count": 0, "undated_count": 0})
    quality = defaultdict(lambda: {k: Decimal(0) if k == "score_sum" else 0 for k in Q_MAP.values()})
    scopes, writes = set(), []
    for node, value in projections.items():
        payload = canonical(value).decode()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        previous = old.get(node)
        if previous and previous.content_hash == digest:
            continue
        if previous is None and value is None:
            continue
        prior = json.loads(previous.projection_json) if previous else None
        for sign, p in ((-1, prior), (1, value)):
            if p is None:
                continue
            ws, kind, creator, day = p["workspaceId"], p["kind"], p["creator"], p["day"]
            inventory[(ws,)]["file_count"] += sign
            inventory[(ws,)]["undated_count"] += sign * (not day)
            scopes.add("workspace/" + ws)
            scopes.add("summary")
            if day:
                days[(ws, kind, creator, day)]["file_count"] += sign
                scopes.add("month/" + day[:7])
            q = p["quality"]
            if q:
                bucket = quality[(ws, kind, creator, q["month"])]
                for source, target in Q_MAP.items():
                    bucket[target] += sign * (Decimal(q[source]) if target == "score_sum" else q[source])
                scopes.add("month/" + q["month"])
        writes.append({"node_id": node, "content_hash": digest, "projection_json": payload})
    if writes:
        stmt = _insert_for(db, BIFileFact.__table__, writes)
        incoming = stmt.inserted if db.bind.dialect.name == "mysql" else stmt.excluded
        changes = {k: incoming[k] for k in ("content_hash", "projection_json")}
        db.execute(stmt.on_duplicate_key_update(**changes) if db.bind.dialect.name == "mysql" else stmt.on_conflict_do_update(index_elements=["node_id"], set_=changes))
        _counter_upsert(db, BIUploadDay, days, ["workspace_id", "identity_kind", "source_key", "day"], ["file_count"])
        _counter_upsert(db, BIInventory, inventory, ["workspace_id"], ["file_count", "undated_count"])
        _counter_upsert(db, BIQualityMonth, quality, ["workspace_id", "identity_kind", "source_key", "month"], list(Q_MAP.values()))
        enqueue(db, BIDirtyScope, scopes | {'page_month/'+s[6:] for s in scopes if s.startswith('month/')})
    return len(writes)


def seed_slice(db, limit=500):
    state = db.get(BIProjectionState, 1)
    baseline = primary_snapshot_id(db)
    if state is None:
        state = BIProjectionState(id=1, baseline_id=baseline)
        db.add(state)
        db.flush()
    if state.baseline_id != baseline:
        raise SnapshotUnavailable("baseline_changed")
    if state.seed_complete:
        return {"complete": True, "sourceRows": 0, "changedNodes": 0}
    cursor = state.seed_cursor or ""
    a = list(db.scalars(select(HistoricalFileNode.node_id).where(HistoricalFileNode.snapshot_id == baseline,
                    HistoricalFileNode.node_id > cursor).distinct().order_by(HistoricalFileNode.node_id).limit(limit)))
    b = list(db.scalars(select(Document.node_id).where(Document.node_id > cursor).order_by(Document.node_id).limit(limit)))
    ids = sorted(set(a) | set(b))[:limit]
    if not ids:
        state.seed_complete, state.last_projected_at = True, utcnow()
        enqueue(db, BIDirtyScope, {"all"})
        return {"complete": True, "sourceRows": len(a) + len(b), "changedNodes": 0}
    changed = apply_projections(db, _projection_rows(db, ids, baseline))
    state.seed_cursor, state.last_projected_at = ids[-1], utcnow()
    return {"complete": False, "sourceRows": len(a) + len(b), "changedNodes": changed}


def project_dirty(db, limit=500):
    state = db.get(BIProjectionState, 1)
    if not state or not state.seed_complete:
        raise SnapshotUnavailable("projection_seed_unready")
    # A primary baseline change is exceptional, never an automatic full rescan.
    rows = db.execute(select(BIDirtyNode.node_id, BIDirtyNode.generation).order_by(BIDirtyNode.requested_at, BIDirtyNode.node_id).limit(limit)).all()
    if not rows:
        return {"sourceRows": 0, "changedNodes": 0, "dirtyNodes": 0}
    if primary_snapshot_id(db) != state.baseline_id:
        raise SnapshotUnavailable("baseline_changed")
    ids = [r.node_id for r in rows]
    changed = apply_projections(db, _projection_rows(db, ids, state.baseline_id))
    for r in rows:
        db.execute(delete(BIDirtyNode).where(BIDirtyNode.node_id == r.node_id, BIDirtyNode.generation == r.generation))
    if changed:
        state.last_projected_at = utcnow()
    return {"sourceRows": len(ids), "changedNodes": changed, "dirtyNodes": len(ids)}
