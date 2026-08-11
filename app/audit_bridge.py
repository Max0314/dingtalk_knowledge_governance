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
import uuid as uuid_module

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, FileAuditEvent, HistoricalFileNode, HistoricalSnapshot, ReviewJob, SpaceMap
from .fileclass import review_classes
from .integrations import DingtalkClient, IntegrationError
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


def _attach_numeric_id(db: Session, event: FileAuditEvent, settings: Settings) -> None:
    """The event's bizId IS the file's numeric storage dentry id (verified by
    cross-download); hand it to the mirrored document so reviews can fetch
    the body through the numeric-only download API. A document whose review
    ran before its key arrived gets an automatic content-scope re-review."""
    if not event.matched_node_id or not (event.biz_id or "").isdigit():
        return
    doc = db.get(Document, event.matched_node_id)
    if not doc or doc.storage_dentry_id:
        return
    doc.storage_dentry_id = event.biz_id
    if doc.is_folder or doc.file_class not in review_classes(settings.review_classes):
        return
    pending = db.scalar(select(ReviewJob).where(ReviewJob.node_id == doc.node_id,
                                                ReviewJob.status.in_(("pending", "running"))))
    if not pending:
        db.add(ReviewJob(job_id=str(uuid_module.uuid4()), node_id=doc.node_id,
                         trigger="content_key", requested_by="system"))


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

    # Locator: a wiki-search by file name gives the doorbell an address. Only
    # events the search cannot place fall back to sweeping the governed set.
    governed = set(_governed_workspaces(db))
    ring: set[str] = set()
    located_ungoverned: set[str] = set()
    unlocated = 0
    if settings.bridge_locator_enabled and settings.wiki_storage_space_id:
        client = DingtalkClient(settings)
        operator = settings.dingtalk_sync_operator_id
        for event in wiki_events:
            names = _name_candidates(event)
            try:
                # Storage search returns dentryUuids (== wiki nodeIds); the wiki
                # batch query then names the workspace each hit lives in.
                dentries = asyncio.run(client.search_dentries(names[0], operator,
                                                              [settings.wiki_storage_space_id]))
                exact_ids = [d["dentry_uuid"] for d in dentries if d.get("name") in names and d.get("dentry_uuid")]
                nodes = asyncio.run(client.batch_query_wiki_nodes(exact_ids, operator)) if exact_ids else []
            except (IntegrationError, RuntimeError):
                unlocated += 1
                continue
            hits = [node for node in nodes if node.get("name") in names]
            workspaces = {node.get("workspace_id") for node in hits if node.get("workspace_id")}
            if not workspaces:
                unlocated += 1  # brand-new files may not be indexed yet -> sweep
                continue
            for workspace_id in workspaces:
                if workspace_id in governed or settings.bridge_scope == "mapped":
                    ring.add(workspace_id)
                else:
                    located_ungoverned.add(workspace_id)
            node_ids = {node["node_id"] for node in hits if node.get("node_id")}
            if not event.matched_node_id and len(node_ids) == 1:
                event.matched_node_id = node_ids.pop()
                summary["matched"] += 1
            _attach_numeric_id(db, event, settings)
    else:
        unlocated = len(wiki_events)
    if unlocated:
        ring |= governed
    summary["unlocated"] = unlocated
    summary["located_ungoverned"] = sorted(located_ungoverned)[:5]

    debounce = max(60, settings.bridge_debounce_seconds)
    for workspace_id in sorted(ring):
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
        _attach_numeric_id(db, event, settings)
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
