"""Audit-event -> knowledge-base-node bridge.

The audit trail reliably says *that* a knowledge-base write happened, but —
verified live 2026-08-11 — wiki uploads all carry one shared org-wide
storage-space id, so the event does NOT say in *which* library. The bridge
therefore treats wiki write events as an unaddressed doorbell:

  1. consume unprocessed wiki-write events (action mentions 知识库 or
     module 团队空间);
  2. ring the governed set: every workspace the mirror governs gets a
     debounced targeted walk (the proven watcher code, mode="bridge") —
     node_id-exact diffs enqueue the reviews. In the pilot that is one
     workspace; org rollout will add a wiki-search resolver to route events
     by file name before falling back to the sweep;
  3. backfill matched_node_id where the event's resource name joins a
     mirrored or snapshot node uniquely — retried after the walks so files
     the walk just mirrored match too.

space_map remains as an observability tally (which space ids appear, how
often) and for future per-space modules; wiki routing no longer trusts it.

Cost model: no wiki writes -> no walks. Each ring costs one walk per
governed workspace, amortized by the debounce window.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, FileAuditEvent, HistoricalFileNode, HistoricalSnapshot, SpaceMap
from .service import watch_workspace

logger = logging.getLogger("kg.bridge")

BATCH = 500

# In-memory debounce: workspace_id -> monotonic seconds of the last bridge
# walk. Worker restarts forget it; one extra walk is harmless. Failed walks
# are evicted so the next event retries immediately.
_last_walk: dict[str, float] = {}


def _is_wiki_write(event: FileAuditEvent) -> bool:
    return "知识库" in (event.action_view or "") or (event.module_view or "") == "团队空间"


def _name_candidates(event: FileAuditEvent) -> list[str]:
    names = [event.resource]
    if event.extension and not event.resource.endswith("." + event.extension):
        names.append(f"{event.resource}.{event.extension}")
    return [name for name in names if name]


def _latest_snapshot_id(db: Session) -> str:
    return db.scalar(select(HistoricalSnapshot.snapshot_id)
                     .order_by(HistoricalSnapshot.collected_at.desc()).limit(1)) or ""


def _unique_node_match(db: Session, event: FileAuditEvent, snapshot_id: str) -> str:
    """node_id when the resource name matches exactly one known node."""
    names = _name_candidates(event)
    if not names:
        return ""
    nodes = {doc.node_id for doc in db.scalars(
        select(Document).where(or_(*[Document.name == name for name in names])).limit(5)).all()}
    if snapshot_id:
        nodes |= {row[0] for row in db.execute(
            select(HistoricalFileNode.node_id)
            .where(HistoricalFileNode.snapshot_id == snapshot_id,
                   HistoricalFileNode.name.in_(names)).distinct().limit(5)).all()}
    return nodes.pop() if len(nodes) == 1 else ""


def _tally_space(db: Session, event: FileAuditEvent) -> None:
    space_id = event.target_space_id or ""
    if not space_id:
        return
    entry = db.get(SpaceMap, space_id)
    if not entry:
        entry = SpaceMap(space_id=space_id)
        db.add(entry)
        db.flush()
    entry.event_count += 1
    entry.last_event_gmt = max(entry.last_event_gmt or 0, event.gmt_create or 0)


def _governed_workspaces(db: Session) -> list[str]:
    return [row[0] for row in db.execute(select(Document.workspace_id).distinct()).all() if row[0]]


def process_audit_events(db: Session, settings: Settings) -> dict:
    """One bridge cycle. Returns a summary for the worker log."""
    events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.processed.is_(False))
                        .order_by(FileAuditEvent.gmt_create).limit(BATCH)).all()
    summary = {"events": len(events), "wiki_events": 0, "matched": 0, "walks": []}
    snapshot_id = _latest_snapshot_id(db)
    wiki_events: list[FileAuditEvent] = []
    for event in events:
        event.processed = True
        if not _is_wiki_write(event):
            continue
        summary["wiki_events"] += 1
        wiki_events.append(event)
        _tally_space(db, event)
        node_id = _unique_node_match(db, event, snapshot_id)
        if node_id:
            event.matched_node_id = node_id
            summary["matched"] += 1
    db.commit()
    if not wiki_events:
        return summary

    debounce = max(60, settings.bridge_debounce_seconds)
    for workspace_id in _governed_workspaces(db):
        if time.time() - _last_walk.get(workspace_id, 0) < debounce:
            continue
        _last_walk[workspace_id] = time.time()
        run = asyncio.run(watch_workspace(db, settings, workspace_id, mode="bridge"))
        if run.status != "succeeded":
            _last_walk.pop(workspace_id, None)  # let the next event retry at once
        summary["walks"].append({"workspace_id": workspace_id, "run_id": run.run_id, "mode": run.mode,
                                 "status": run.status, "new": run.documents_new,
                                 "changed": run.documents_changed, "error_code": run.error_code})
    # Files the walks just mirrored can now satisfy the unique-name join.
    for event in wiki_events:
        if not event.matched_node_id:
            node_id = _unique_node_match(db, event, snapshot_id)
            if node_id:
                event.matched_node_id = node_id
                summary["matched"] += 1
    db.commit()
    return summary


def bridge_status(db: Session) -> dict:
    spaces = db.scalars(select(SpaceMap).order_by(SpaceMap.event_count.desc()).limit(10)).all()
    pending = db.scalar(select(FileAuditEvent.id).where(FileAuditEvent.processed.is_(False)).limit(1))
    matched = db.scalar(select(FileAuditEvent.id).where(FileAuditEvent.matched_node_id != "").limit(1))
    return {
        "governed_workspaces": _governed_workspaces(db),
        "space_tallies": [{"space_id": entry.space_id, "events": entry.event_count,
                           "workspace_id": entry.workspace_id, "source": entry.source} for entry in spaces],
        "has_unprocessed_events": bool(pending),
        "has_matched_events": bool(matched),
    }
