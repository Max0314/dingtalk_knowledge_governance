"""HTTPS integration, identity preparation and durable incremental outbox."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import contextmanager
import random
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
import httpx
from sqlalchemy import delete, select, update, text
from .bi_models import BIIdentityMonth, BIPushOutbox, BIPushPayload, BIPushState, BIStatPartition
from .bi_push_contract import (MAX_BYTES, canonical, delta_hash, materialize_parts, partition_manifest,
                              state_hash, utc_text, validate_frame)
from .bi_materialize import identity_keys, assert_partition_read_ready, partition_headers
from .bi_snapshot import SnapshotUnavailable
from .db import utcnow
from .http_client import outbound_client
from .metadata_mode import naive


class RemoteFailure(RuntimeError):
    def __init__(self, code, status=503, body=None, retry_after=0):
        self.code, self.status, self.body, self.retry_after = code, status, body or {}, retry_after
        super().__init__(code)


def retry_after_seconds(value):
    try:
        if value.strip().isdigit():
            return min(3600, int(value.strip()))
        when = parsedate_to_datetime(value)
        return min(3600, max(0, int((when - datetime.now(timezone.utc)).total_seconds()) + 1))
    except (ValueError, TypeError, OverflowError):
        return 0


class RemoteClient:
    def __init__(self, settings):
        self.settings = settings

    async def _request(self, method, base, path, token, payload=None, params=None):
        async with asyncio.timeout(45):
            async with outbound_client(self.settings, timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as http:
                r = await http.request(method, base.rstrip("/") + path,
                                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                                       content=canonical(payload) if payload is not None else None, params=params)
        retry = retry_after_seconds(r.headers.get('retry-after', ''))
        try:
            body = r.json()
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            if r.status_code >= 300:
                raise RemoteFailure('remote_http_error', r.status_code, retry_after=retry) from None
            raise RemoteFailure("invalid_remote_response", r.status_code) from None
        if r.status_code >= 300:
            known = {'invalid_push_credential', 'push_disabled', 'payload_too_large', 'invalid_snapshot',
                     'snapshot_conflict', 'stale_sequence', 'base_mismatch'}
            code = body.get('code')
            raise RemoteFailure(code if isinstance(code, str) and code in known else 'remote_http_error',
                                r.status_code, body, retry)
        return body

    async def status(self):
        b = await self._request("GET", self.settings.bi_push_base_url, "/api/internal/v1/knowledge-governance/status", self.settings.bi_push_token)
        if b.get("ok") is not True or not b.get("data", {}).get("capabilities", {}).get("incrementalPartitionsV2"):
            raise RemoteFailure("incremental_receiver_unready")
        return b["data"]

    async def send(self, frame):
        try:
            b = await self._request("POST", self.settings.bi_push_base_url, "/api/internal/v1/knowledge-governance/snapshots", self.settings.bi_push_token, frame)
        except RemoteFailure as exc:
            if 200 <= exc.status < 300:
                raise RemoteFailure('invalid_push_confirmation', 422) from None
            raise
        if b.get("ok") is not True or b.get("code") not in ("accepted", "already_accepted") or not isinstance(b.get('data'), dict):
            raise RemoteFailure("invalid_push_confirmation", 422)
        return b["data"]

    async def employee(self, path, payload=None, params=None):
        b = await self._request("POST" if payload is not None else "GET", self.settings.bi_center_base_url, path,
                                self.settings.bi_center_internal_token, payload, params)
        if b.get("code") != 0 or not isinstance(b.get("data"), dict):
            raise RemoteFailure("identity_contract_invalid")
        return b["data"]

    async def master_status(self):
        return await self.employee("/api/internal/v1/employee-master-data/status")

    async def directory_version(self, month):
        data = await self.employee("/api/internal/v1/employee-directory/monthly", params={"month": month, "limit": 1, "includeInactive": "true"})
        return {"month": month, "directoryVersion": data["directoryVersion"] or "none",
                "policyVersion": data["policyVersion"] or "none", "resolvedSnapshotMonth": data["resolvedSnapshotMonth"]}

    async def resolve(self, keys):
        typed = {"user_id": "userId", "union_id": "unionId", "unknown": "sourceId"}
        body = {"items": [{"sourceSystem": "dingtalk_knowledge_governance", "monthKey": month,
                           "eligibilityBasis": "fact_month_v1", "identity": {typed[kind]: source}}
                          for month, kind, source in keys]}
        data = await self.employee("/api/internal/v1/employee-identity/resolve-batch", body)
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) != len(keys) or {r.get("index") for r in rows} != set(range(len(keys))):
            raise RemoteFailure("identity_batch_incomplete")
        return sorted(rows, key=lambda r: r["index"])


_identity_lock = threading.Lock()


@contextmanager
def identity_lock(sessions):
    with sessions() as db:
        bind = db.get_bind()
    if bind.dialect.name != 'mysql':
        got = _identity_lock.acquire(blocking=False)
        if not got:
            raise SnapshotUnavailable('identity_prepare_busy')
        try:
            yield
        finally:
            _identity_lock.release()
        return
    with bind.connect() as conn:
        got = conn.scalar(text("SELECT GET_LOCK('kg_identity_prepare',0)")) == 1
        conn.commit()
        if not got:
            raise SnapshotUnavailable('identity_prepare_busy')
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK('kg_identity_prepare')"))
            conn.commit()


async def prepare_identities(sessions, inputs_by_month, remote):
    with identity_lock(sessions):
        return await _prepare_identities(sessions, inputs_by_month, remote)


async def _prepare_identities(sessions, inputs_by_month, remote):
    """Only affected aggregate months; commits bounded cache batches."""
    before = await remote.master_status()
    versions, org_shared = {}, {}
    for month, inputs in inputs_by_month.items():
        version = await remote.directory_version(month)
        from .org_month_cache import prepare_month
        await prepare_month(sessions, month, version, remote, org_shared)
        versions[month] = version
        wanted = identity_keys(inputs)
        with sessions() as db:
            rows = {(r.month, r.identity_kind, r.source_key): r for r in db.scalars(select(BIIdentityMonth).where(BIIdentityMonth.month == month)).all()}
            due = [k for k in wanted if k not in rows or rows[k].directory_version != version["directoryVersion"]
                   or rows[k].policy_version != version["policyVersion"]
                   or rows[k].resolve_status in {"error", "snapshot_missing"}
                   or naive(rows[k].resolved_at) < naive(utcnow() - timedelta(hours=24))]
        for offset in range(0, len(due), 500):
            keys = due[offset:offset + 500]
            resolved = await remote.resolve(keys)
            with sessions.begin() as db:
                for key, result in zip(keys, resolved):
                    if result.get("status") in {"error", "snapshot_missing"} or result.get("directoryVersion") != version["directoryVersion"] or result.get("policyVersion") != version["policyVersion"]:
                        raise SnapshotUnavailable("identity_version_unready")
                    row = db.get(BIIdentityMonth, key)
                    if row is None:
                        row = BIIdentityMonth(month=key[0], identity_kind=key[1], source_key=key[2])
                        db.add(row)
                    row.employee_key = result.get("employeeKey") or ""
                    row.resolve_status = result["status"]
                    row.include_official = result.get("matched") is True and result.get("includeInOfficialStats") is True
                    row.directory_version, row.policy_version = version["directoryVersion"], version["policyVersion"]
                    row.resolved_snapshot_month, row.resolved_at = version["resolvedSnapshotMonth"], utcnow()
    after = await remote.master_status()
    if before.get("policyVersion") != after.get("policyVersion") or before.get("monthlyDirectoryVersions") != after.get("monthlyDirectoryVersions"):
        raise SnapshotUnavailable("identity_versions_changed")
    return versions


def queue_frame(db, parts, *, force_new=False, manifest=None):
    state = db.scalar(select(BIPushState).where(BIPushState.id == 1).with_for_update())
    if state is None:
        state = BIPushState(id=1)
        db.add(state)
        db.flush()
    manifest = manifest if manifest is not None else partition_manifest(parts)
    result = state_hash(manifest)
    acked = json.loads(state.acked_manifest_json or "{}")
    if manifest == acked and not state.force_baseline and not force_new:
        return None
    latest = db.execute(select(BIPushOutbox.snapshot_id, BIPushOutbox.result_hash, BIPushOutbox.base_sequence)
                        .where(BIPushOutbox.status.in_(("pending", "sending"))).order_by(BIPushOutbox.sequence.desc()).limit(1)).first()
    if latest and latest.result_hash == result and latest.base_sequence == state.acked_sequence and not force_new:
        return latest.snapshot_id
    sequence = max(state.last_issued_sequence, state.acked_sequence) + 1
    if sequence > 18446744073709551615:
        raise SnapshotUnavailable("sequence_exhausted")
    snapshot_id = str(uuid4())
    if state.acked_sequence == 0 or state.force_baseline:
        frame = materialize_parts(parts, snapshot_id, sequence)
        kind = "baseline"
    else:
        changes = {"upserts": [{"key": k, "value": parts[k]} for k in sorted(manifest) if acked.get(k) != manifest[k]],
                   "deletes": sorted(set(acked) - set(manifest))}
        frame = {"contractVersion": 2, "sourceId": state.source_id, "snapshotId": snapshot_id,
                 "sequence": str(sequence), "baseSequence": str(state.acked_sequence), "generatedAt": utc_text(utcnow()),
                 "contentHash": "", "resultHash": result, "changes": changes}
        frame["contentHash"] = delta_hash(frame)
        kind = "delta"
    validate_frame(frame)
    wire = canonical(frame)
    if len(wire) > MAX_BYTES:
        raise SnapshotUnavailable("payload_too_large")
    db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "pending").values(status="superseded"))
    db.add(BIPushOutbox(snapshot_id=snapshot_id, sequence=sequence, content_hash=frame["contentHash"],
                       frame_kind=kind, base_sequence=state.acked_sequence, result_hash=result,
                       status="pending"))
    db.add(BIPushPayload(snapshot_id=snapshot_id, request_json=wire.decode(), result_manifest_json=canonical(manifest).decode()))
    state.last_issued_sequence, state.last_build_at = sequence, utcnow()
    return snapshot_id


def queue_current(db, *, force_new=False):
    """Read payloads only after a changed hash proves they are needed."""
    assert_partition_read_ready(db)
    state = db.scalar(select(BIPushState).where(BIPushState.id == 1).with_for_update())
    if not state:
        return None
    manifest = partition_headers(db)
    if not manifest:
        return None
    acked = json.loads(state.acked_manifest_json or "{}")
    if manifest == acked and not state.force_baseline and not force_new:
        return None
    if state.send_blocked_code and not force_new:
        return None
    latest = db.execute(select(BIPushOutbox.snapshot_id, BIPushOutbox.result_hash, BIPushOutbox.base_sequence, BIPushOutbox.frame_kind)
                        .where(BIPushOutbox.status.in_(("pending", "sending"))).order_by(BIPushOutbox.sequence.desc()).limit(1)).first()
    # Keep the first immutable baseline during a transport outage. After its
    # confirmation the current partitions become a small delta, not a stream
    # of rewritten full JSON baselines while the remote remains unavailable.
    if latest and not force_new and (latest.frame_kind == "baseline" and (not state.acked_sequence or state.force_baseline) or
                                     latest.result_hash == state_hash(manifest) and latest.base_sequence == state.acked_sequence):
        return latest.snapshot_id
    keys = list(manifest) if not state.acked_sequence or state.force_baseline else [k for k in manifest if manifest[k] != acked.get(k)]
    parts = {r.partition_key: json.loads(r.payload_json) for r in db.scalars(select(BIStatPartition).where(BIStatPartition.partition_key.in_(keys)))}
    return queue_frame(db, parts, force_new=force_new, manifest=manifest)


def claim_frame(db):
    now = utcnow()
    state = db.scalar(select(BIPushState).where(BIPushState.id == 1).with_for_update())
    if state is None or state.send_blocked_code or (state.send_not_before and naive(state.send_not_before) > naive(now)):
        return None
    active = db.scalar(select(BIPushOutbox.snapshot_id).where(BIPushOutbox.status == "sending", BIPushOutbox.lease_until > now).limit(1))
    if active:
        return None
    db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "sending", BIPushOutbox.lease_until <= now).values(status="pending"))
    # A successful in-flight frame can invalidate a previously prepared delta.
    db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "pending", BIPushOutbox.base_sequence != state.acked_sequence).values(status="superseded"))
    row = db.scalar(select(BIPushOutbox).where(BIPushOutbox.status == "pending").order_by(BIPushOutbox.sequence.desc()).limit(1))
    if not row or (row.next_attempt_at and naive(row.next_attempt_at) > naive(now)):
        return None
    lease = str(uuid4())
    row.status, row.lease_owner, row.lease_until = "sending", lease, now + timedelta(seconds=120)
    row.attempts += 1
    payload = db.get(BIPushPayload, row.snapshot_id)
    if payload is None:
        raise SnapshotUnavailable("outbox_payload_missing")
    return {"snapshotId": row.snapshot_id, "sequence": row.sequence, "lease": lease,
            "request": json.loads(payload.request_json), "resultHash": row.result_hash}


def finish_frame(db, claim, confirmation=None, error=None):
    state = db.scalar(select(BIPushState).where(BIPushState.id == 1).with_for_update())
    row = db.get(BIPushOutbox, claim["snapshotId"])
    now = utcnow()
    if not row or row.status != "sending" or row.lease_owner != claim["lease"] or naive(row.lease_until) < naive(now):
        return False
    if confirmation is not None and not isinstance(confirmation, dict):
        confirmation, error = None, RemoteFailure('invalid_push_confirmation', 422)
    if confirmation is not None:
        if (confirmation.get("snapshotId") != row.snapshot_id or confirmation.get("sequence") != str(row.sequence)
                or confirmation.get("contentHash") != row.content_hash or confirmation.get("resultHash") != row.result_hash):
            error = RemoteFailure("invalid_push_confirmation", 422)
        else:
            row.status, row.acked_at, row.error_code = "acked", now, ""
            if row.sequence > state.acked_sequence:
                manifest_json = db.scalar(select(BIPushPayload.result_manifest_json).where(BIPushPayload.snapshot_id == row.snapshot_id))
                if manifest_json is None:
                    raise SnapshotUnavailable("outbox_payload_missing")
                state.acked_sequence, state.acked_manifest_json = row.sequence, manifest_json
                state.force_baseline = False
                db.execute(update(BIPushOutbox).where(BIPushOutbox.status == "pending", BIPushOutbox.base_sequence != row.sequence).values(status="superseded"))
            state.last_success_at, state.send_failures, state.send_not_before = now, 0, None
            row.lease_owner = row.lease_until = None
            return True
    if error is None:
        error = RemoteFailure("transport_failure")
    row.lease_owner = row.lease_until = None
    row.error_code = error.code
    if error.code in {"stale_sequence", "base_mismatch"}:
        row.status = "superseded"
        return False
    blocked = error.status in {401, 403, 413, 422} or error.code == "snapshot_conflict"
    state.send_failures += 1
    wait = [30, 60, 120, 300, 600][min(state.send_failures - 1, 4)]
    wait = max(wait, error.retry_after) * (1 + random.random() / 10)
    state.send_not_before = row.next_attempt_at = now + timedelta(seconds=wait)
    row.status = "blocked" if blocked else "pending"
    if blocked:
        state.send_blocked_code = error.code
    state.last_error_at, state.last_error_code = now, error.code
    return False
