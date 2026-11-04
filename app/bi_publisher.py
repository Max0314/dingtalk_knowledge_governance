"""Incremental KG publisher. Deployment enables transport only after both releases pass."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from sqlalchemy import delete, select, text, update
from .bi_models import BIDirtyScope, BIProjectionState, BIPushOutbox, BIPushPayload, BIPushState
from .bi_materialize import dirty_plan, month_inputs, publish_partitions, read_meta
from .bi_projection import enqueue, install_change_hooks, project_dirty, seed_slice
from .bi_push import RemoteClient, RemoteFailure, claim_frame, finish_frame, prepare_identities, queue_current
from .bi_push_contract import canonical, partition_manifest, state_hash
from .bi_snapshot import SnapshotUnavailable
from .config import get_settings
from .db import SessionLocal, Workspace, engine, init_db, utcnow
from .integrations import DingtalkClient
from .metadata_mode import naive
from .service import workspace_name_is_ignored

logger = logging.getLogger("kg.bi_publisher")
_local_lock = threading.Lock()


@contextmanager
def publisher_lock():
    if engine.dialect.name != "mysql":
        got = _local_lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                _local_lock.release()
        return
    with engine.connect() as c:
        got = c.execute(text("SELECT GET_LOCK('kg_bi_snapshot_build',0)")).scalar()
        c.commit()  # advisory lock survives commit; no transaction across HTTP
        try:
            yield bool(got)
        finally:
            if got:
                c.execute(text("SELECT RELEASE_LOCK('kg_bi_snapshot_build')"))
                c.commit()


async def refresh_registry(sessions, settings):
    with sessions() as db:
        state = db.get(BIPushState, 1)
        if state and state.workspace_registry_ready_at and naive(state.workspace_registry_ready_at) > naive(utcnow() - timedelta(hours=1)):
            # New audit placeholders need an earlier (at most ten-minute) refresh.
            missing = db.scalar(select(Workspace.workspace_id).where(Workspace.metadata_confirmed_at.is_(None), Workspace.is_active.is_(True)).limit(1))
            if not missing or naive(state.workspace_registry_ready_at) > naive(utcnow() - timedelta(minutes=10)):
                return
    client, token, seen_tokens, spaces = DingtalkClient(settings), "", set(), []
    for _ in range(1000):
        page = await client.list_workspaces(settings.dingtalk_sync_operator_id, token)
        spaces.extend(page["items"])
        token = page.get("next_token", "")
        if not token:
            break
        if token in seen_tokens:
            raise SnapshotUnavailable("workspace_metadata_unready")
        seen_tokens.add(token)
    else:
        raise SnapshotUnavailable("workspace_metadata_unready")
    # Only an entire, successful listing can establish trusted workspace names.
    with sessions.begin() as db:
        state = db.get(BIPushState, 1)
        if state is None:
            state = BIPushState(id=1)
            db.add(state)
        for value in spaces:
            ws_id, name = str(value.get("workspace_id") or ""), str(value.get("name") or "")
            if not ws_id or not name:
                raise SnapshotUnavailable("workspace_metadata_unready")
            ws = db.get(Workspace, ws_id)
            if ws is None:
                ws = Workspace(workspace_id=ws_id, name=name)
                db.add(ws)
            ws.name = name
            ws.metadata_confirmed_at = utcnow()
            ws.is_active = not workspace_name_is_ignored(settings, name)
            ws.unreachable_misses = 0
        state.workspace_registry_ready_at = utcnow()


def reconcile_remote(db, remote):
    """A verified remote head repairs acknowledgements, never guesses a delta base."""
    state = db.scalar(select(BIPushState).where(BIPushState.id == 1).with_for_update())
    if not state:
        return
    seq = int(remote["acceptedSequence"])
    if seq == state.acked_sequence and remote.get("stateHash") == (state_hash(json.loads(state.acked_manifest_json)) if seq else None):
        return
    known = db.scalar(select(BIPushOutbox).where(BIPushOutbox.sequence == seq)) if seq else None
    manifest_json = db.scalar(select(BIPushPayload.result_manifest_json).where(BIPushPayload.snapshot_id == known.snapshot_id)) if known else None
    if (known and known.snapshot_id == remote.get("acceptedSnapshotId") and known.content_hash == remote.get("contentHash")
            and known.result_hash == remote.get("stateHash") and manifest_json and state_hash(json.loads(manifest_json)) == known.result_hash):
        if seq >= state.acked_sequence:
            known.status, known.acked_at = "acked", utcnow()
            known.lease_owner = known.lease_until = None
            state.acked_sequence, state.acked_manifest_json = seq, manifest_json
            state.force_baseline = False
            state.last_success_at = utcnow()
            if not state.send_blocked_code:
                state.send_failures, state.send_not_before = 0, None
            db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "pending", BIPushOutbox.base_sequence != seq).values(status="superseded"))
            return
    # Actual restore/divergence: one controlled baseline from materialized parts.
    # A normal pending delta with an old local base only takes the branch above.
    recovering = db.scalar(select(BIPushOutbox.snapshot_id).where(
        BIPushOutbox.frame_kind == 'baseline', BIPushOutbox.status.in_(('pending', 'sending')),
        BIPushOutbox.sequence > seq).limit(1)) if state.force_baseline else None
    if recovering:
        return
    state.last_issued_sequence = max(state.last_issued_sequence, seq)
    state.force_baseline = True
    db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "pending").values(status="superseded"))


async def refresh_versions(sessions, remote):
    status = await remote.master_status()
    with sessions.begin() as db:
        from .page_identity import refresh_versions as refresh_page_versions
        refresh_page_versions(db, status)
        meta = read_meta(db)
        for row in meta.get("identityVersions", []):
            directory = status["monthlyDirectoryVersions"].get(row["resolvedSnapshotMonth"], "missing")
            if directory != row["directoryVersion"] or (status["policyVersion"] or "none") != row["policyVersion"]:
                enqueue(db, BIDirtyScope, {"month/" + row["month"]})


async def build_cycle(sessions, settings, remote, allow_empty=False):
    await refresh_registry(sessions, settings)
    await refresh_versions(sessions, remote)
    with sessions() as db:
        plan = dirty_plan(db, settings)
        inputs = {m: month_inputs(db, m, plan["names"], plan["projection"], plan["now"], plan["unconfirmed"]) for m in plan["months"]}
    versions = await prepare_identities(sessions, inputs, remote) if inputs else {}
    with sessions.begin() as db:
        # Lock is held by the caller: projector and partition builder cannot race.
        result = publish_partitions(db, settings, versions, allow_empty=allow_empty)
    with sessions.begin() as db:
        # Manifest and partition payloads come from this one committed read view.
        result["queued"] = bool(queue_current(db))
    return result


async def send_once(sessions, remote):
    with sessions.begin() as db:
        claim = claim_frame(db)
    if not claim:
        return None
    confirmation, error = None, None
    try:
        confirmation = await remote.send(claim["request"])
    except RemoteFailure as exc:
        error = exc
    except Exception as exc:
        error = RemoteFailure("transport_" + type(exc).__name__)
    with sessions.begin() as db:
        done = finish_frame(db, claim, confirmation, error)
        if done:
            queue_current(db)
    if error and error.code in {"stale_sequence", "base_mismatch"}:
        head = await remote.status()
        with sessions.begin() as db:
            reconcile_remote(db, head)
            queue_current(db)
    return {"confirmed": done, "frameKind": "delta" if claim["request"]["contractVersion"] == 2 else "baseline",
            "bytes": len(canonical(claim["request"])), "error": error.code if error else None}


def cleanup_outbox(db):
    keep = set(db.scalars(select(BIPushOutbox.snapshot_id).where(BIPushOutbox.status.in_(("pending", "sending")))))
    keep.update(db.scalars(select(BIPushOutbox.snapshot_id).where(BIPushOutbox.status == "acked").order_by(BIPushOutbox.sequence.desc()).limit(2)))
    keep.update(db.scalars(select(BIPushOutbox.snapshot_id).where(BIPushOutbox.status == "blocked").order_by(BIPushOutbox.sequence.desc()).limit(1)))
    clear_ids = list(db.scalars(select(BIPushPayload.snapshot_id).where(BIPushPayload.snapshot_id.not_in(keep)).limit(1000)))
    delete_ids = list(db.scalars(select(BIPushOutbox.snapshot_id).where(BIPushOutbox.snapshot_id.not_in(keep), BIPushOutbox.created_at < utcnow() - timedelta(days=30))
                               .order_by(BIPushOutbox.created_at).limit(1000)))
    if clear_ids:
        db.execute(delete(BIPushPayload).where(BIPushPayload.snapshot_id.in_(clear_ids)))
    if delete_ids:
        db.execute(delete(BIPushOutbox).where(BIPushOutbox.snapshot_id.in_(delete_ids)))


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.bi_push_enabled:
        logger.info("BI publisher disabled")
        return
    init_db()
    remote = RemoteClient(settings)
    next_build = next_status = next_cleanup = 0.0
    page_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kg-page-identities')
    page_future, next_page = None, 0.0
    while True:
        try:
            if page_future is None or page_future.done():
                if page_future is not None:
                    try:
                        page_future.result()
                    except Exception as exc:
                        logger.warning('Page identity cache deferred: %s', type(exc).__name__)
                        next_page = max(next_page, time.monotonic()+60)
                    page_future = None
                if time.monotonic() >= next_page:
                    from .page_identity import run_slice as prepare_page_slice
                    page_future = page_executor.submit(prepare_page_slice, SessionLocal, settings)
                    next_page = time.monotonic()+5
            with publisher_lock() as got:
                if got:
                    with SessionLocal.begin() as db:
                        p = project_dirty(db)
                    if p["dirtyNodes"]:
                        logger.info("metadata projection nodes=%s changed=%s", p["dirtyNodes"], p["changedNodes"])
                    now = time.monotonic()
                    try:
                        if now >= next_status:
                            next_status = now + 600
                            head = asyncio.run(remote.status())
                            with SessionLocal.begin() as db:
                                reconcile_remote(db, head)
                            asyncio.run(refresh_versions(SessionLocal, remote))
                        if now >= next_build:
                            next_build = now + settings.bi_push_interval_seconds
                            async def bounded_build():
                                async with asyncio.timeout(120):
                                    return await build_cycle(SessionLocal, settings, remote)
                            result = asyncio.run(bounded_build())
                            logger.info("incremental partitions changed=%s months=%s queued=%s", result["changedPartitions"], result["months"], result["queued"])
                    except Exception as exc:
                        # Fresh identity/build failure cannot strand a durable
                        # frame already validated by an earlier build.
                        logger.warning("BI build deferred: %s", getattr(exc, "code", None) or type(exc).__name__)
                    result = asyncio.run(send_once(SessionLocal, remote))
                    if result:
                        logger.info("BI transfer kind=%s bytes=%s confirmed=%s error=%s", result["frameKind"], result["bytes"], result["confirmed"], result["error"])
                    if now >= next_cleanup:
                        with SessionLocal.begin() as db:
                            cleanup_outbox(db)
                        next_cleanup = now + 3600
        except Exception as exc:
            logger.warning("BI publisher deferred: %s", getattr(exc, "code", None) or (str(exc) if isinstance(exc, SnapshotUnavailable) else type(exc).__name__))
            next_build = max(next_build, time.monotonic() + 60)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "seed", "retry", "rebuild"), default="run", nargs="?")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--reason")
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    if args.command == "run":
        run()
        return
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("deployment administrator required")
    settings = get_settings()
    install_change_hooks()
    with publisher_lock() as got:
        if not got:
            raise SystemExit("publisher busy")
        if args.command == "seed":
            if settings.audit_pull_enabled or settings.full_scan_enabled:
                raise SystemExit("pause collectors before initial seed")
            while True:
                with SessionLocal.begin() as db:
                    result = seed_slice(db)
                if result["complete"]:
                    print(json.dumps(result))
                    break
                time.sleep(.05)
            return
        if not args.reason:
            raise SystemExit("reason required")
        if args.command == "retry":
            with SessionLocal.begin() as db:
                state = db.get(BIPushState, 1)
                row = db.get(BIPushOutbox, args.snapshot_id)
                payload_exists = db.scalar(select(BIPushPayload.snapshot_id).where(BIPushPayload.snapshot_id == args.snapshot_id))
                if not row or not payload_exists or state.send_blocked_code in {"invalid_snapshot", "payload_too_large", "snapshot_conflict", "invalid_push_confirmation"}:
                    raise SystemExit("validated rebuild required")
                state.send_blocked_code, state.send_not_before = "", None
                row.status, row.next_attempt_at = "pending", None
            print("retry_released=true")
        else:
            remote = RemoteClient(settings)
            with SessionLocal.begin() as db:
                enqueue(db, BIDirtyScope, {"all"})
            asyncio.run(build_cycle(SessionLocal, settings, remote, args.allow_empty))
            with SessionLocal.begin() as db:
                state = db.get(BIPushState, 1)
                state.send_blocked_code, state.send_not_before = "", None
                queue_current(db, force_new=True)
            print("validated_rebuild_queued=true")


if __name__ == "__main__":
    main()
