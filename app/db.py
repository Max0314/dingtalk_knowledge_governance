import time
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    source_created_at: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(String(64), default="")
    creator_key: Mapped[str] = mapped_column(String(128), default="")
    owner_department_id: Mapped[str] = mapped_column(String(128), default="")
    owner_department_name: Mapped[str] = mapped_column(String(255), default="未映射")
    owner_biz_group_name: Mapped[str] = mapped_column(String(255), default="未映射")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    documents: Mapped[list["Document"]] = relationship(back_populates="workspace")


class WorkspaceRole(Base):
    __tablename__ = "workspace_roles"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), index=True)
    employee_key: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(32))  # administrator | reviewer
    display_name: Mapped[str] = mapped_column(String(128), default="")


class Document(Base):
    __tablename__ = "documents"
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
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
    uploader_key: Mapped[str] = mapped_column(String(128), default="")
    uploader_name: Mapped[str] = mapped_column(String(128), default="未映射")
    department_name: Mapped[str] = mapped_column(String(255), default="未映射")
    biz_group_name: Mapped[str] = mapped_column(String(255), default="未映射")
    org_matched: Mapped[bool] = mapped_column(Boolean, default=False)
    content_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace: Mapped[Workspace] = relationship(back_populates="documents")
    reviews: Mapped[list["ReviewInstance"]] = relationship(back_populates="document")


class ReviewInstance(Base):
    __tablename__ = "review_instances"
    review_instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    reviewer_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    api_key_env_name: Mapped[str] = mapped_column(String(128), default="KG_MODEL_API_KEY")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String(64), default="v1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class HistoricalSnapshot(Base):
    """An immutable, metadata-only baseline used for historical governance metrics."""
    __tablename__ = "historical_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    __table_args__ = (UniqueConstraint("snapshot_id", "workspace_id", "node_id", name="uq_history_snapshot_node"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("historical_snapshots.snapshot_id"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(512), default="")
    node_type: Mapped[str] = mapped_column(String(64), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    source_created_at: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(String(64), default="")


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
