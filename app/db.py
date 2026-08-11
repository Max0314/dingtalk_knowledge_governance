import time
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def id_string(length: int = 128):
    """Case-sensitive string type for external identifiers.

    DingTalk node/workspace ids are case-sensitive random strings; under
    MySQL's default *_ci collation, ids differing only in case collide (the
    2026-08 full scan contained 10 such pairs). SQLite keeps the plain type
    because it does not know MySQL collation names.
    """
    return String(length).with_variant(String(length, collation="utf8mb4_bin"), "mysql")


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    source_created_at: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(String(64), default="")
    creator_key: Mapped[str] = mapped_column(id_string(), default="")
    owner_department_id: Mapped[str] = mapped_column(String(128), default="")
    owner_department_name: Mapped[str] = mapped_column(String(255), default="未映射")
    owner_biz_group_name: Mapped[str] = mapped_column(String(255), default="未映射")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    documents: Mapped[list["Document"]] = relationship(back_populates="workspace")


class WorkspaceRole(Base):
    __tablename__ = "workspace_roles"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), index=True)
    employee_key: Mapped[str] = mapped_column(id_string(), index=True)
    role: Mapped[str] = mapped_column(String(32))  # administrator | reviewer
    display_name: Mapped[str] = mapped_column(String(128), default="")


class Document(Base):
    __tablename__ = "documents"
    node_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    category: Mapped[str] = mapped_column(String(64), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    is_folder: Mapped[bool] = mapped_column(Boolean, default=False)
    source_created_at: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(String(64), default="")
    uploader_key: Mapped[str] = mapped_column(id_string(), default="")
    uploader_name: Mapped[str] = mapped_column(String(128), default="未映射")
    department_name: Mapped[str] = mapped_column(String(255), default="未映射")
    biz_group_name: Mapped[str] = mapped_column(String(255), default="未映射")
    org_matched: Mapped[bool] = mapped_column(Boolean, default=False)
    content_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Consecutive complete watcher walks that failed to see this node; the
    # watcher soft-deletes at the configured threshold and resets on sight.
    watch_misses: Mapped[int] = mapped_column(Integer, default=0)
    # Asset class from app.fileclass (document/sheet/image/engineering/...).
    # Only configured classes enter the review queue automatically.
    file_class: Mapped[str] = mapped_column(String(32), default="", index=True)
    # Numeric storage dentry id (== the audit trail's bizId for the upload
    # event — verified by cross-download 2026-08-12). The download API only
    # accepts this numeric form; empty means no event seen yet.
    storage_dentry_id: Mapped[str] = mapped_column(String(64), default="")
    workspace: Mapped[Workspace] = relationship(back_populates="documents")
    reviews: Mapped[list["ReviewInstance"]] = relationship(back_populates="document")


class ReviewInstance(Base):
    __tablename__ = "review_instances"
    review_instance_id: Mapped[str] = mapped_column(id_string(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("documents.node_id"), index=True)
    ai_score: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(32))
    review_scope: Mapped[str] = mapped_column(String(32))  # full_content | metadata_only
    rule_version: Mapped[str] = mapped_column(String(32), default="V1.1")
    model_config_version: Mapped[str] = mapped_column(String(64), default="rule-engine")
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    content_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    document: Mapped[Document] = relationship(back_populates="reviews")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    review_instance_id: Mapped[str] = mapped_column(ForeignKey("review_instances.review_instance_id"), index=True)
    decision: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str] = mapped_column(Text, default="")
    reviewer_key: Mapped[str] = mapped_column(id_string())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    job_id: Mapped[str] = mapped_column(id_string(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("documents.node_id"), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(128), default="")
    result_review_instance_id: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    run_id: Mapped[str] = mapped_column(id_string(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(32), default="incremental")
    workspaces_seen: Mapped[int] = mapped_column(Integer, default=0)
    documents_seen: Mapped[int] = mapped_column(Integer, default=0)
    documents_new: Mapped[int] = mapped_column(Integer, default=0)
    documents_changed: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(64), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(1024), default="")
    model_name: Mapped[str] = mapped_column(String(255), default="")
    # Stored key (operator decision 2026-08-07). Never returned in full by any
    # API — reads expose only a masked tail. Empty means fall back to the env
    # variable named below.
    api_key: Mapped[str] = mapped_column(Text, default="")
    api_key_env_name: Mapped[str] = mapped_column(String(128), default="KG_MODEL_API_KEY")
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    thinking_mode: Mapped[str] = mapped_column(String(16), default="")  # "" | on | off
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String(64), default="v1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ModelConfigHistory(Base):
    """Every save keeps the previous state, so any config can be rolled back."""
    __tablename__ = "model_config_history"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32), default="update")  # create | update | rollback
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    saved_by: Mapped[str] = mapped_column(String(128), default="")
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HistoricalSnapshot(Base):
    """An immutable, metadata-only baseline used for historical governance metrics."""
    __tablename__ = "historical_snapshots"
    snapshot_id: Mapped[str] = mapped_column(id_string(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), default="dingtalk")
    scope: Mapped[str] = mapped_column(String(128), default="accessible_org_wiki_spaces")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(32), default="completed")
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    total_file_nodes: Mapped[int] = mapped_column(Integer, default=0)
    created_2025: Mapped[int] = mapped_column(Integer, default=0)
    created_2026: Mapped[int] = mapped_column(Integer, default=0)


class HistoricalFileNode(Base):
    """Metadata only: no document body, attachment bytes, or extracted content."""
    __tablename__ = "historical_file_nodes"
    __table_args__ = (UniqueConstraint("snapshot_id", "workspace_id", "node_id", name="uq_history_snapshot_node"),
                      Index("ix_hfn_snapshot_creator", "snapshot_id", "creator_user_id"))
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("historical_snapshots.snapshot_id"), index=True)
    workspace_id: Mapped[str] = mapped_column(id_string(), index=True)
    node_id: Mapped[str] = mapped_column(id_string(), index=True)
    parent_node_id: Mapped[str] = mapped_column(id_string(), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    node_type: Mapped[str] = mapped_column(String(64), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    creator_user_id: Mapped[str] = mapped_column(id_string(), default="")
    modifier_user_id: Mapped[str] = mapped_column(id_string(), default="")
    source_created_at: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(String(64), default="")


class UploaderMonthStat(Base):
    """Pre-aggregated (uploader x workspace x month) file counts.

    Dashboards read ONLY this small table — never the raw node rows — so a
    page load costs a few indexed reads on the shared MySQL, not a scan.
    Rows are rebuilt per workspace when a scan finishes that workspace.
    """
    __tablename__ = "uploader_month_stats"
    __table_args__ = (UniqueConstraint("snapshot_id", "workspace_id", "creator_user_id", "month", name="uq_uploader_ws_month"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(id_string(64), index=True)
    workspace_id: Mapped[str] = mapped_column(id_string(), index=True)
    workspace_name: Mapped[str] = mapped_column(String(255), default="")
    creator_user_id: Mapped[str] = mapped_column(id_string(), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)


class EmployeeMap(Base):
    """bi_center identity resolution cache: DingTalk userId -> org attribution."""
    __tablename__ = "employee_map"
    user_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    employee_key: Mapped[str] = mapped_column(id_string(), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    department_name: Mapped[str] = mapped_column(String(255), default="")
    biz_group_name: Mapped[str] = mapped_column(String(255), default="")
    matched: Mapped[bool] = mapped_column(Boolean, default=False)
    include_official: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    """Server-side login sessions. Only the SHA-256 of the cookie token is stored."""
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    union_id: Mapped[str] = mapped_column(id_string(), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    avatar: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StreamEvent(Base):
    """Raw DingTalk push events (Stream mode), kept for processing and audit."""
    __tablename__ = "stream_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    biz_id: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FileAuditEvent(Base):
    """Write-type operations from the exclusive audit trail (pillar B CDC)."""
    __tablename__ = "file_audit_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    biz_id: Mapped[str] = mapped_column(String(64), unique=True)
    gmt_create: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), index=True)
    operator_user_id: Mapped[str] = mapped_column(id_string(), index=True, default="")
    operator_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(32), default="")
    action_view: Mapped[str] = mapped_column(String(64), default="", index=True)
    module_view: Mapped[str] = mapped_column(String(64), default="")
    resource: Mapped[str] = mapped_column(String(512), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    target_space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")
    matched_node_id: Mapped[str] = mapped_column(id_string(), default="")  # filled by the future matcher
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditDailyAgg(Base):
    """Read-op volumes (previews/downloads) aggregated per day and action."""
    __tablename__ = "audit_daily_aggs"
    __table_args__ = (UniqueConstraint("day", "module_view", "action_view", name="uq_audit_agg"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    module_view: Mapped[str] = mapped_column(String(64), default="")
    action_view: Mapped[str] = mapped_column(String(64), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)


class SpaceMap(Base):
    """Learned mapping from audit-trail numeric space ids to wiki workspaces.

    Audit events carry only a numeric storage-space id; the review pipeline
    needs a workspaceId. Rows start unmapped (workspace_id="") and are filled
    by the bridge's resource-name learner, a manual seed, or reconciliation —
    the per-space event tally shows which unmapped spaces matter most.
    """
    __tablename__ = "space_map"
    space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(id_string(), default="", index=True)
    workspace_name: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(32), default="")  # learned | manual | seed
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    last_event_gmt: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditState(Base):
    """Singleton cursor row for the audit CDC puller."""
    __tablename__ = "audit_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    last_gmt_create: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rows: Mapped[int] = mapped_column(Integer, default=0)
    silence_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """Outbox for review-result pushes. Rows are auditable and immutable-ish:
    the worker only moves status pending -> sent/failed and stamps the error."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), default="robot_o2o")
    node_id: Mapped[str] = mapped_column(id_string(), default="", index=True)
    review_instance_id: Mapped[str] = mapped_column(id_string(64), default="")
    target_union_id: Mapped[str] = mapped_column(id_string(), default="")
    target_user_id: Mapped[str] = mapped_column(id_string(), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def build_engine():
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite"):
        return create_engine(settings.database_url, connect_args={"check_same_thread": False})
    # pool_pre_ping is an engine option, not a DBAPI connect arg; pool_recycle
    # keeps pooled connections younger than MySQL's wait_timeout.
    return create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=3600)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db(max_attempts: int = 30, retry_delay: float = 2.0) -> None:
    """Create tables, waiting for the external database to accept connections.

    The platform stack has no database container, so `depends_on: healthy`
    cannot order startup; the MySQL server may be briefly unreachable while
    containers restart. Retrying here beats crash-looping the whole service.
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with engine.connect():
                break
        except OperationalError as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                raise RuntimeError(f"数据库在 {max_attempts * retry_delay:.0f} 秒内不可达。") from last_error
            time.sleep(retry_delay)
    Base.metadata.create_all(engine)
