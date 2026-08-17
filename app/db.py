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
    # Set only by a COMPLETE seed walk. Inferring "already seeded" from a
    # non-empty mirror mistakes an interrupted seed for done and floods the
    # review queue with stock files on the next walk.
    watch_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    # False = 当前不可见（连续两次探测缺席/404，如已删除或失权的库）：退出
    # 补种集合、知识库列表与当前统计，历史数据保留；恢复可见后自动回归。
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 连续缺席计数（整轮全量没见到 / 详情+列表都 404 各计一次）；见到即清零。
    # 两次才判不可见——单次列表不完整不能误停正常库（codex 第八轮 P1）。
    unreachable_misses: Mapped[int] = mapped_column(Integer, default=0)
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
    # 目录架构：walk 遍历时可直接得到父节点；审计直建的文档先记 path、
    # 置 directory_pending，父节点由每月 10/24 全量核对补准。
    parent_node_id: Mapped[str] = mapped_column(id_string(), default="")
    path: Mapped[str] = mapped_column(String(1024), default="")
    directory_pending: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # 修改合并窗（2026-08-14 定稿）：正文修改事件只置脏与到期时间，收割器
    # 到点合并评一次；dirty_since + 6h 封顶防止持续编辑永不评审。带索引：
    # 收割查询按到期筛选+排序，绝大多数行为 NULL。
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 最近一次操作人（审计事件）；绝不覆盖 uploader_key——通知归属属于上传人。
    last_modifier_key: Mapped[str] = mapped_column(id_string(), default="")
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
    # Which parameter set scored this instance: "builtin"、"global@v3" or
    # "department:研发中心@v2" — reviews stay traceable across rule edits.
    rule_config_ref: Mapped[str] = mapped_column(String(160), default="")
    model_config_version: Mapped[str] = mapped_column(String(64), default="rule-engine")
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    content_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    # Why the body was unavailable when scope is metadata_only
    # (no_numeric_id / unsupported / too_large / disabled / fetch_failed:*).
    content_note: Mapped[str] = mapped_column(String(64), default="")
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
    # Which workspace a watch run walked, and the failure detail — without
    # these a failed run cannot be attributed (2026-08-13 finding).
    workspace_id: Mapped[str] = mapped_column(id_string(), default="")
    workspace_name: Mapped[str] = mapped_column(String(255), default="")
    error_detail: Mapped[str] = mapped_column(String(512), default="")
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


class ScoringRuleConfig(Base):
    """Parameter overrides for the V1.1 rule engine (app.scoring.RULE_CATALOG).

    One row with scope="global" is the org default; scope="department" rows are
    full independent copies keyed by the bi_center 一级部门名称 — an uploader's
    department picks its row at review time, falling back to global, then to
    builtin defaults. ``config`` stores a complete effective_config() dict so a
    later change to the global row never silently shifts a department's rules.
    ``editors`` ([{"union_id","name"}]) lists who may edit a department row
    besides global admins.
    """
    __tablename__ = "scoring_rule_configs"
    __table_args__ = (UniqueConstraint("scope", "department_name", name="uq_rule_scope_department"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), default="global")  # global | department
    department_name: Mapped[str] = mapped_column(String(255), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    editors: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScoringRuleConfigHistory(Base):
    """Pre-change snapshots of scoring rule configs, enabling audit and rollback."""
    __tablename__ = "scoring_rule_config_history"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32), default="update")  # create | update | rollback | delete
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
                      Index("ix_hfn_snapshot_creator", "snapshot_id", "creator_user_id"),
                      # newest-first paging of the merged document list
                      Index("ix_hfn_snapshot_created", "snapshot_id", "source_created_at"),
                      # anti-join for baseline∪live dedup (metrics aggregation, /api/v1/files)
                      Index("ix_hfn_snapshot_node", "snapshot_id", "node_id"))
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
    # Match confidence: "" (none) / "provisional" (name join — advisory only,
    # never attaches keys: a same-named NEW upload must not be pinned to an
    # old node) / "confirmed" (locator exact node id — authoritative).
    match_status: Mapped[str] = mapped_column(String(16), default="")
    # Terminal outcome when processed: "done" or a dead_letter_* reason —
    # a give-up must be observable, not disguised as success.
    resolution: Mapped[str] = mapped_column(String(32), default="")
    # Fair retry rotation: locator picks the least-recently attempted first.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Retry-window base for dead-letter reopens; received_at stays untouched
    # as the immutable ingestion audit field.
    retry_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchPlan(Base):
    """Singleton schedule cursor for org-wide scans. `completed_for` names the
    scan-day (YYYY-MM-DD) whose full walk finished — persisted so a container
    restart during the idle period never re-triggers a whole-org scan."""
    __tablename__ = "watch_plan"
    id: Mapped[int] = mapped_column(primary_key=True)
    completed_for: Mapped[str] = mapped_column(String(10), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BridgeWalk(Base):
    """Persistent bridge-walk queue: workspaces the audit bridge owes a fast
    targeted walk. Rows survive restarts and per-pass walk budgets — the
    sixth workspace of a burst is walked next pass, not forgotten. Attempted
    rows rotate to the back (least-recently-attempted first), so five
    persistently failing libraries cannot starve the rest."""
    __tablename__ = "bridge_walk_queue"
    workspace_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)


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
    if engine.dialect.name == "mysql":
        # api 与 worker 同时启动会并发跑 ALTER/CREATE INDEX（曾出现重复索引
        # 竞争反复重启，codex 第八轮 P1）。MySQL 命名锁串行化迁移段：后到者
        # 等前者做完再进（届时重新 inspect 一切已就位、全部跳过）。锁超时
        # （返回 0）按无锁继续——迁移本身幂等，退化等价于旧行为。
        from sqlalchemy import text
        with engine.connect() as lock_conn:
            got = lock_conn.execute(text("SELECT GET_LOCK('kg_schema_migration', 60)")).scalar()
            try:
                Base.metadata.create_all(engine)
                _ensure_columns()
            finally:
                if got:
                    lock_conn.execute(text("SELECT RELEASE_LOCK('kg_schema_migration')"))
    else:
        Base.metadata.create_all(engine)
        _ensure_columns()


# create_all only creates missing tables; columns added to an existing model
# need an explicit ALTER. Keep this list tiny and append-only.
EXTRA_COLUMNS = {
    "review_instances": {"rule_config_ref": "VARCHAR(160) NOT NULL DEFAULT ''",
                         "content_note": "VARCHAR(64) NOT NULL DEFAULT ''"},
    "sync_runs": {"workspace_id": "VARCHAR(128) NOT NULL DEFAULT ''",
                  "workspace_name": "VARCHAR(255) NOT NULL DEFAULT ''",
                  "error_detail": "VARCHAR(512) NOT NULL DEFAULT ''"},
    "workspaces": {"watch_seeded": "TINYINT(1) NOT NULL DEFAULT 0",
                   "is_active": "TINYINT(1) NOT NULL DEFAULT 1",
                   "unreachable_misses": "INTEGER NOT NULL DEFAULT 0"},
    "file_audit_events": {"match_status": "VARCHAR(16) NOT NULL DEFAULT ''",
                          "resolution": "VARCHAR(32) NOT NULL DEFAULT ''",
                          "last_attempt_at": "DATETIME NULL",
                          "retry_started_at": "DATETIME NULL"},
    "bridge_walk_queue": {"last_attempt_at": "DATETIME NULL",
                          "failures": "INTEGER NOT NULL DEFAULT 0"},
    "documents": {"parent_node_id": "VARCHAR(128) NOT NULL DEFAULT ''",
                  "path": "VARCHAR(1024) NOT NULL DEFAULT ''",
                  "directory_pending": "TINYINT(1) NOT NULL DEFAULT 0",
                  "review_due_at": "DATETIME NULL",
                  "dirty_since": "DATETIME NULL",
                  "deleted_at": "DATETIME NULL",
                  "last_modifier_key": "VARCHAR(128) NOT NULL DEFAULT ''"},
}


EXTRA_INDEXES = {
    "historical_file_nodes": {
        "ix_hfn_snapshot_node": "CREATE INDEX ix_hfn_snapshot_node ON historical_file_nodes (snapshot_id, node_id)",
    },
    "documents": {
        "ix_documents_review_due_at": "CREATE INDEX ix_documents_review_due_at ON documents (review_due_at)",
    },
}


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    for table, columns in EXTRA_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    # create_all only builds indexes for NEW tables; existing tables get
    # model-declared indexes here (idempotent by name).
    for table, indexes in EXTRA_INDEXES.items():
        if not inspector.has_table(table):
            continue
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table)}
        for name, ddl in indexes.items():
            if name not in existing_indexes:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
