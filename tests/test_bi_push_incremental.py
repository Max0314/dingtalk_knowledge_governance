"""Synthetic facts only: incremental load, transport and paused side effects."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import bi_projection
from app.bi_models import (BIIdentityMonth, BIPushState, BIPushOutbox, BIProjectionState, BIUploadDay,
                          BIDirtyNode, BIDirtyScope, BIFileFact)
from app.bi_materialize import publish_partitions, read_partitions
from app.bi_projection import enqueue, project_dirty, seed_slice
from app.bi_push import claim_frame, finish_frame, queue_current, queue_frame, RemoteFailure
from app.bi_push_contract import (ContractError, canonical, content_hash, materialize_parts, partition_manifest,
                                  state_hash, validate_delta, validate_snapshot)
from app.bi_publisher import reconcile_remote
from app.bi_snapshot import SnapshotUnavailable
from app.config import Settings
from app.db import Base, Document, HistoricalFileNode, HistoricalSnapshot, Workspace, utcnow

NOW = datetime(2026, 11, 4, 0, tzinfo=timezone.utc)
VERSION = {"month": "2026-11", "directoryVersion": "directory-test-1", "policyVersion": "policy-test-1", "resolvedSnapshotMonth": "2026-11"}


@pytest.fixture
def local(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, metadata_only=True, content_extract_enabled=False)
    bind = create_engine("sqlite:///" + str(tmp_path / "facts.db"))
    Base.metadata.create_all(bind)
    sessions = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(bi_projection, "get_settings", lambda: settings)
    bi_projection.install_change_hooks()
    with sessions.begin() as db:
        db.add_all([Workspace(workspace_id="space-a", name="C-Synthetic", metadata_confirmed_at=NOW),
                    Workspace(workspace_id="space-zero", name="D-Empty synthetic", metadata_confirmed_at=NOW),
                    BIPushState(id=1, workspace_registry_ready_at=NOW),
                    HistoricalSnapshot(snapshot_id="baseline-test", source="test", definition={"is_primary_baseline": True, "creator_identity_kind": "union_id"})])
        db.flush()
        db.add_all([HistoricalFileNode(snapshot_id="baseline-test", node_id="baseline-a", workspace_id="space-a", node_type="file",
                                     creator_user_id="synthetic-union-a", source_created_at="2026-10-31T18:00:00Z"),
                    Document(node_id="baseline-a", workspace_id="space-a", name="synthetic", uploader_key="synthetic-union-a", source_created_at="2026-01-01T00:00:00Z"),
                    Document(node_id="live-b", workspace_id="space-a", name="synthetic", source_creator_key="synthetic-union-b", source_creator_kind="union_id", source_created_at="2026-11-03T09:00:00+08:00")])
    while True:
        with sessions.begin() as db:
            result = seed_slice(db, limit=2)
        if result["complete"]:
            break
    with sessions.begin() as db:
        project_dirty(db)
        for who in ("synthetic-union-a", "synthetic-union-b"):
            db.add(BIIdentityMonth(month="2026-11", identity_kind="union_id", source_key=who, employee_key=who,
                resolve_status="matched", include_official=True, directory_version=VERSION["directoryVersion"], policy_version=VERSION["policyVersion"], resolved_snapshot_month="2026-11"))
    with sessions.begin() as db:
        publish_partitions(db, settings, {"2026-11": VERSION}, now=NOW)
    yield sessions, bind, settings
    bind.dispose()


def test_seed_dedup_cst_and_atomic_parts(local):
    sessions, _, _ = local
    with sessions() as db:
        parts = read_partitions(db)
        v = materialize_parts(parts, "11111111-1111-4111-8111-111111111111", 1)
        assert v["data"]["summary"]["totalFileCount"] == 2
        assert v["data"]["monthly"][0]["month"] == "2026-11"
        assert v["data"]["monthly"][0]["uploadedFileCount"] == 2
        assert len(v["data"]["employees"]) == 2
        assert db.get(BIProjectionState, 1).seed_complete
        assert not db.scalars(select(BIDirtyScope)).all()


def test_idle_has_no_source_reads_or_business_writes(local):
    sessions, bind, settings = local
    sql = []
    payload_reads = []
    def record(_, __, statement, *args):
        sql.append(statement.lower())
        if "payload_json" in statement.lower() and "kg_bi_partition" in statement.lower():
            payload_reads.append(args[0])
    event.listen(bind, "before_cursor_execute", record)
    try:
        with sessions.begin() as db:
            queue_current(db)
        ack_claim(sessions)
        sql.clear()
        payload_reads.clear()
        for _ in range(3):
            with sessions.begin() as db:
                assert project_dirty(db)["sourceRows"] == 0
                result = publish_partitions(db, settings, {}, now=NOW)
                assert result["changedPartitions"] == 0
                assert queue_current(db) is None
        assert not any(" from documents" in s or " from historical_file_nodes" in s for s in sql)
        assert not any(s.lstrip().startswith(("insert ", "update ", "delete ")) for s in sql)
        assert all(params == ("meta",) for params in payload_reads)
    finally:
        event.remove(bind, "before_cursor_execute", record)


def ack_claim(sessions):
    with sessions.begin() as db:
        claim = claim_frame(db)
    assert claim
    v = claim["request"]
    confirmation = {"snapshotId": v["snapshotId"], "sequence": str(v["sequence"]), "contentHash": v["contentHash"], "resultHash": claim["resultHash"]}
    with sessions.begin() as db:
        assert finish_frame(db, claim, confirmation)
    return claim


def test_one_change_emits_delta_and_keeps_unchanged_space(local):
    sessions, _, settings = local
    with sessions.begin() as db:
        queue_current(db)
    baseline = ack_claim(sessions)
    assert baseline["request"]["contractVersion"] == 1
    with sessions.begin() as db:
        doc = db.get(Document, "live-b")
        doc.source_created_at = "2026-10-20T08:00:00+08:00"
        version_oct = {**VERSION, "month": "2026-10", "resolvedSnapshotMonth": "2026-10"}
        db.add(BIIdentityMonth(month="2026-10", identity_kind="union_id", source_key="synthetic-union-b", employee_key="synthetic-union-b", resolve_status="matched", include_official=True,
                              directory_version=VERSION["directoryVersion"], policy_version=VERSION["policyVersion"], resolved_snapshot_month="2026-10"))
    with sessions.begin() as db:
        p = project_dirty(db)
        assert p["sourceRows"] == p["changedNodes"] == 1
        publish_partitions(db, settings, {"2026-10": version_oct, "2026-11": VERSION}, now=NOW)
        with pytest.raises(SnapshotUnavailable, match="partition_commit_required"):
            read_partitions(db)
    with sessions.begin() as db:
        queue_current(db)
    delta = ack_claim(sessions)["request"]
    validate_delta(delta)
    assert delta["contractVersion"] == 2 and delta["baseSequence"] == baseline["request"]["sequence"]
    changed = {r["key"] for r in delta["changes"]["upserts"]}
    assert "month/2026-10" in changed and "month/2026-11" in changed
    assert "workspace/space-zero" not in changed
    assert delta['changes']['deletes'] == []


def test_restore_keeps_one_immutable_baseline(local):
    sessions, _, _ = local
    with sessions.begin() as db:
        queue_current(db)
    ack_claim(sessions)
    remote = {'acceptedSequence': '0', 'stateHash': None, 'contentHash': None, 'acceptedSnapshotId': None}
    with sessions.begin() as db:
        reconcile_remote(db, remote)
        recovery_id = queue_current(db)
    with sessions.begin() as db:
        reconcile_remote(db, remote)
        assert queue_current(db) == recovery_id
        assert db.get(BIPushOutbox, recovery_id).status == 'pending'


def test_month_entering_retained_window_is_built(local):
    from app.bi_materialize import dirty_plan
    from app.bi_models import BIStatPartition
    sessions, _, settings = local
    with sessions.begin() as db:
        # Simulate a previously retained 24-month window and a deletion which
        # makes an older aggregate month enter it. It has no dirty marker itself.
        db.add(BIUploadDay(workspace_id='space-a',identity_kind='unknown',source_key='',day='2024-10-01',month='2024-10',file_count=1))
        enqueue(db,BIDirtyScope,{'month/2026-11'})
    with sessions() as db:
        plan=dirty_plan(db,settings,now=NOW)
        assert '2024-10' in plan['months']


def test_duplicate_projection_does_not_rewrite_facts(local):
    sessions, bind, _ = local
    with sessions.begin() as db:
        previous = db.get(BIProjectionState, 1).last_projected_at
        enqueue(db, BIDirtyNode, {"live-b"})
    sql = []
    def record(_, __, statement, *args):
        sql.append(statement.lower())
    event.listen(bind, "before_cursor_execute", record)
    try:
        with sessions.begin() as db:
            assert project_dirty(db)["changedNodes"] == 0
            assert db.get(BIProjectionState, 1).last_projected_at == previous
        assert not any(s.startswith(("update kg_bi_file_fact", "insert into kg_bi_file_fact", "update kg_bi_upload_day", "insert into kg_bi_upload_day")) for s in sql)
    finally:
        event.remove(bind, "before_cursor_execute", record)


def test_pause_configuration_and_strict_contract(local):
    sessions, _, _ = local
    with pytest.raises(ValueError):
        Settings(_env_file=None, metadata_only=True, content_extract_enabled=True)
    with sessions() as db:
        v = materialize_parts(read_partitions(db), "11111111-1111-4111-8111-111111111111", 1)
    v["data"]["summary"]["totalFileCount"] = True
    v["contentHash"] = content_hash(v)
    with pytest.raises(ContractError):
        validate_snapshot(v)


def test_source_block_cannot_be_bypassed_by_new_frame(local):
    sessions, _, _ = local
    with sessions.begin() as db:
        queue_frame(db, read_partitions(db))
    with sessions.begin() as db:
        claim = claim_frame(db)
    with sessions.begin() as db:
        finish_frame(db, claim, error=RemoteFailure("invalid_push_credential", 401))
        queue_frame(db, read_partitions(db), force_new=True)
    with sessions.begin() as db:
        assert claim_frame(db) is None
        assert db.get(BIPushState, 1).send_blocked_code == "invalid_push_credential"
