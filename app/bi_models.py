"""Additive tables for aggregate delivery; migrated explicitly before release."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.mysql import BIGINT, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, id_string, utcnow

SEQUENCE = BigInteger().with_variant(BIGINT(unsigned=True), "mysql").with_variant(Integer, "sqlite")
PAYLOAD = Text().with_variant(LONGTEXT(), "mysql")


class BIIdentityMonth(Base):
    __tablename__ = "kg_bi_identity_month"
    __table_args__ = (Index('ix_kg_bi_identity_employee', 'employee_key', 'month'),)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    identity_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_key: Mapped[str] = mapped_column(id_string(), primary_key=True)
    employee_key: Mapped[str] = mapped_column(id_string(), default="")
    resolve_status: Mapped[str] = mapped_column(String(32), default="unmatched")
    include_official: Mapped[bool] = mapped_column(Boolean, default=False)
    directory_version: Mapped[str] = mapped_column(String(128), default="")
    policy_version: Mapped[str] = mapped_column(String(128), default="")
    resolved_snapshot_month: Mapped[str] = mapped_column(String(7), default="")
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BIPushState(Base):
    __tablename__ = "kg_bi_push_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), default="kg-internal-production")
    last_issued_sequence: Mapped[int] = mapped_column(SEQUENCE, default=0)
    send_blocked_code: Mapped[str] = mapped_column(String(64), default="")
    send_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_failures: Mapped[int] = mapped_column(Integer, default=0)
    workspace_registry_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_build_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    acked_sequence: Mapped[int] = mapped_column(SEQUENCE, default=0)
    acked_manifest_json: Mapped[str] = mapped_column(PAYLOAD, default="{}")
    force_baseline: Mapped[bool] = mapped_column(Boolean, default=False)


class BIPushOutbox(Base):
    __tablename__ = "kg_bi_push_outbox"
    __table_args__ = (Index("ix_kg_bi_outbox_due", "status", "next_attempt_at"), Index("ix_kg_bi_outbox_created", "created_at"))
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(SEQUENCE, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    frame_kind: Mapped[str] = mapped_column(String(16), default="baseline")
    base_sequence: Mapped[int] = mapped_column(SEQUENCE, default=0)
    result_hash: Mapped[str] = mapped_column(String(64), default="")


class BIPushPayload(Base):
    """Immutable large values, separate from lease/retry updates for row binlogs."""
    __tablename__ = "kg_bi_push_payload"
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_json: Mapped[str] = mapped_column(PAYLOAD)
    result_manifest_json: Mapped[str] = mapped_column(PAYLOAD)


class ScanRecoveryRun(Base):
    __tablename__ = "scan_recovery_runs"
    recovery_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    month: Mapped[str] = mapped_column(String(7))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requested_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(255))
    target_workspace_ids_json: Mapped[str] = mapped_column(PAYLOAD)
    prior_tasks_json: Mapped[str] = mapped_column(PAYLOAD)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BIDirtyNode(Base):
    __tablename__ = "kg_bi_dirty_node"
    __table_args__ = (Index("ix_kg_bi_dirty_node_due", "requested_at", "node_id"),)
    node_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=1)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BIDirtyScope(Base):
    __tablename__ = "kg_bi_dirty_scope"
    scope_key: Mapped[str] = mapped_column(id_string(160), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=1)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BIFileFact(Base):
    __tablename__ = "kg_bi_file_fact"
    node_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    projection_json: Mapped[str] = mapped_column(PAYLOAD)


class BIUploadDay(Base):
    __tablename__ = "kg_bi_upload_day"
    __table_args__ = (Index("ix_kg_bi_day_month", "month", "workspace_id"), Index("ix_kg_bi_day_person", "source_key", "day"))
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    identity_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_key: Mapped[str] = mapped_column(id_string(), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    month: Mapped[str] = mapped_column(String(7))
    file_count: Mapped[int] = mapped_column(BigInteger)


class BIQualityMonth(Base):
    __tablename__ = "kg_bi_quality_month"
    __table_args__ = (Index("ix_kg_bi_quality_month", "month", "workspace_id"),)
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    identity_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_key: Mapped[str] = mapped_column(id_string(), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    reviewed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    scored_count: Mapped[int] = mapped_column(BigInteger, default=0)
    score_sum: Mapped[object] = mapped_column(Numeric(20, 4), default=0)
    pass_count: Mapped[int] = mapped_column(BigInteger, default=0)
    manual_count: Mapped[int] = mapped_column(BigInteger, default=0)
    return_count: Mapped[int] = mapped_column(BigInteger, default=0)


class BIInventory(Base):
    __tablename__ = "kg_bi_inventory"
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    file_count: Mapped[int] = mapped_column(BigInteger, default=0)
    undated_count: Mapped[int] = mapped_column(BigInteger, default=0)


class BIStatPartition(Base):
    __tablename__ = "kg_bi_partition"
    partition_key: Mapped[str] = mapped_column(id_string(160), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(PAYLOAD)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BIProjectionState(Base):
    __tablename__ = "kg_bi_projection_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    baseline_id: Mapped[str] = mapped_column(String(64), default="")
    seed_cursor: Mapped[str] = mapped_column(id_string(), default="")
    seed_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PageIdentityState(Base):
    __tablename__ = 'kg_page_identity_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_hash: Mapped[str] = mapped_column(String(64), default='')


class FileInventoryVersion(Base):
    __tablename__ = 'kg_file_inventory_version'
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
