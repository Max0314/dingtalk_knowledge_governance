"""Audit-event -> knowledge-base-node bridge.

The audit trail says *that* something happened in a numeric storage space,
but reviews need a wiki node_id. Instead of trusting filename heuristics as
the primary key, the bridge treats events as a doorbell:

  1. consume unprocessed wiki-write events;
  2. learn the space_id -> workspaceId mapping by joining the event's
     resource name against mirrored documents (unique match wins; ambiguity
     stays unmapped and the monthly reconciliation arbitrates);
  3. for mapped workspaces inside the configured scope, run a debounced
     targeted walk (the proven watcher code, mode="bridge") — node_id-exact
     new/changed detection enqueues the reviews;
  4. backfill matched_node_id on events where the name join is unique, so
     the audit row links to the exact node it touched.

Cost model: walks happen only for workspaces with actual write activity —
a quiet org costs nothing beyond the CDC pull itself.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, FileAuditEvent, HistoricalFileNode, HistoricalSnapshot, SpaceMap, Workspace, utcnow
from .service import watch_workspace

logger = logging.getLogger("kg.bridge")

BATCH = 500
# Same-name docs created within this window count as join candidates. Wide on
# purpose: source timestamps mix timezones, and the name-uniqueness constraint
# carries the real weight.
JOIN_WINDOW_MS = 48 * 3600 * 1000

# In-memory debounce: workspace_id -> monotonic seconds of the last bridge walk.
# Worker restarts forget it; one extra walk is harmless.
_last_walk: dict[str, float] = {}


def _is_wiki_write(event: FileAuditEvent) -> bool:
    return "知识库" in (event.action_view or "") or (event.module_view or "") == "团队空间"


def _name_candidates(event: FileAuditEvent) -> list[str]:
    names = [event.resource]
    if event.extension and not event.resource.endswith("." + event.extension):
        names.append(f"{event.resource}.{event.extension}")
    return [name for name in names if name]


def _matching_documents(db: Session, event: FileAuditEvent) -> list[Document]:
    names = _name_candidates(event)
    if not names:
        return []
    return db.scalars(select(Document).where(or_(*[Document.name == name for name in names]))
                      .limit(20)).all()


def _snapshot_workspaces(db: Session, names: list[str]) -> set[str]:
    """First-contact bootstrap: workspaces the mirror has never walked still
    exist in the latest reconciliation snapshot (141k+ named nodes), so a new
    space can map itself on its very first event."""
    snapshot_id = db.scalar(select(HistoricalSnapshot.snapshot_id)
                            .order_by(HistoricalSnapshot.collected_at.desc()).limit(1))
    if not snapshot_id or not names:
        return set()
    rows = db.execute(select(HistoricalFileNode.workspace_id)
                      .where(HistoricalFileNode.snapshot_id == snapshot_id,
                             HistoricalFileNode.name.in_(names)).distinct().limit(5)).all()
    return {row[0] for row in rows}


def _learn_mapping(db: Session, entry: SpaceMap, event: FileAuditEvent) -> None:
    """Unique (resource name -> workspace) join teaches the space mapping."""
    names = _name_candidates(event)
    workspaces = {doc.workspace_id for doc in _matching_documents(db, event)} | _snapshot_workspaces(db, names)
    if len(workspaces) != 1:
        return
    workspace_id = workspaces.pop()
    entry.workspace_id = workspace_id
    entry.source = "learned"
    ws = db.get(Workspace, workspace_id)
    entry.workspace_name = ws.name if ws else ""
    logger.info("space mapping learned: %s -> %s (%s) via %r", entry.space_id, workspace_id,
                entry.workspace_name, event.resource[:40])


def _backfill_match(db: Session, event: FileAuditEvent, workspace_id: str) -> None:
    docs = [doc for doc in _matching_documents(db, event) if doc.workspace_id == workspace_id]
    if len(docs) == 1:
        event.matched_node_id = docs[0].node_id


def _in_scope(db: Session, settings: Settings, workspace_id: str) -> bool:
    if settings.bridge_scope == "mapped":
        return True
    # "watched": only workspaces the mirror already governs — i.e. the walk
    # would diff against existing rows instead of silently seeding a stranger.
    return bool(db.scalar(select(Document.node_id).where(Document.workspace_id == workspace_id).limit(1)))


def process_audit_events(db: Session, settings: Settings) -> dict:
    """One bridge cycle. Returns a summary for the worker log."""
    events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.processed.is_(False))
                        .order_by(FileAuditEvent.gmt_create).limit(BATCH)).all()
    summary = {"events": len(events), "wiki_events": 0, "mapped": 0, "learned": 0, "walks": []}
    dirty: dict[str, str] = {}  # workspace_id -> space_id (for logging)
    for event in events:
        event.processed = True
        if not _is_wiki_write(event):
            continue
        summary["wiki_events"] += 1
        space_id = event.target_space_id or ""
        if not space_id:
            continue
        entry = db.get(SpaceMap, space_id)
        if not entry:
            entry = SpaceMap(space_id=space_id)
            db.add(entry)
            db.flush()
        entry.event_count += 1
        entry.last_event_gmt = max(entry.last_event_gmt or 0, event.gmt_create or 0)
        if not entry.workspace_id:
            before = entry.workspace_id
            _learn_mapping(db, entry, event)
            if entry.workspace_id and not before:
                summary["learned"] += 1
        if entry.workspace_id:
            summary["mapped"] += 1
            _backfill_match(db, event, entry.workspace_id)
            if _in_scope(db, settings, entry.workspace_id):
                dirty[entry.workspace_id] = space_id
    db.commit()

    debounce = max(60, settings.bridge_debounce_seconds)
    for workspace_id, space_id in dirty.items():
        if time.time() - _last_walk.get(workspace_id, 0) < debounce:
            continue
        _last_walk[workspace_id] = time.time()
        run = asyncio.run(watch_workspace(db, settings, workspace_id, mode="bridge"))
        summary["walks"].append({"workspace_id": workspace_id, "space_id": space_id, "run_id": run.run_id,
                                 "mode": run.mode, "status": run.status, "new": run.documents_new,
                                 "changed": run.documents_changed, "error_code": run.error_code})
    return summary


def bridge_status(db: Session) -> dict:
    mapped = db.scalars(select(SpaceMap).where(SpaceMap.workspace_id != "")).all()
    unmapped = db.scalars(select(SpaceMap).where(SpaceMap.workspace_id == "")
                          .order_by(SpaceMap.event_count.desc()).limit(10)).all()
    pending = db.scalar(select(FileAuditEvent.id).where(FileAuditEvent.processed.is_(False)).limit(1))
    return {
        "mapped_spaces": [{"space_id": entry.space_id, "workspace_id": entry.workspace_id,
                           "workspace_name": entry.workspace_name, "source": entry.source,
                           "events": entry.event_count} for entry in mapped],
        "top_unmapped_spaces": [{"space_id": entry.space_id, "events": entry.event_count}
                                for entry in unmapped],
        "has_unprocessed_events": bool(pending),
    }
