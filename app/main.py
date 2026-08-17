from __future__ import annotations
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from . import metrics, orgmap
from .config import get_settings
from .db import Document, EmployeeMap, HistoricalFileNode, HistoricalSnapshot, ModelConfig, Notification, ReviewDecision, ReviewInstance, ReviewJob, ScoringRuleConfig, ScoringRuleConfigHistory, SessionLocal, SyncRun, Workspace, WorkspaceRole, init_db
from .integrations import BiCenterClient, DingtalkClient, IntegrationError, model_connection_check
from .scoring import RULE_VERSION, catalog_dict, effective_config
from .service import document_dict, review_dict, run_watch_cycle_async, seed_demo, sync_from_dingtalk, workspace_dict

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if get_settings().demo_mode:
        with SessionLocal() as db: seed_demo(db)
    yield


app = FastAPI(title="DingTalk Knowledge Governance", version="1.0.0", lifespan=lifespan)

from .auth import guard_middleware, register_auth_routes  # noqa: E402

app.middleware("http")(guard_middleware)
register_auth_routes(app)


def db_session():
    db = SessionLocal()
    try: yield db
    finally: db.close()


def actor() -> str:
    return get_settings().default_actor


def require_admin(request: Request):
    """Model configs and diagnostics are operator-only. With auth off (local
    dev) everything stays open; with auth on, an empty admin list fails closed."""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    admins = {token.strip() for token in settings.admin_union_ids.split(",") if token.strip()}
    user = getattr(request.state, "user", None) or {}
    if user.get("union_id") not in admins:
        raise HTTPException(403, "仅管理员可执行此操作。")


class GovernancePatch(BaseModel):
    owner_department_id: str = ""
    owner_department_name: str = ""
    owner_biz_group_name: str = ""
    administrators: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    # Public callers can only request a manual rerun. Internal audit/watch
    # triggers are assigned server-side and must not be spoofable via the API.
    trigger: str = Field(default="manual_rerun", pattern="^manual_rerun$")


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(pass|return|manual_review)$")
    comment: str = Field(default="", max_length=1000)


class ModelRequest(BaseModel):
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""  # blank keeps the stored key
    api_key_env_name: str = "KG_MODEL_API_KEY"
    temperature: float | None = Field(default=None, ge=0, le=2)
    thinking_mode: str = Field(default="", pattern="^$|^(on|off)$")
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    enabled: bool = False
    version: str = "v1"


class RuleConfigRequest(BaseModel):
    config: dict = Field(default_factory=dict)


class RuleDepartmentCreateRequest(BaseModel):
    department_name: str = Field(min_length=1, max_length=255)


class RuleEditorsRequest(BaseModel):
    editors: list[dict] = Field(default_factory=list, max_length=50)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dingtalk_knowledge_governance", "document_body_persistence": "disabled"}


@app.get("/api/v1/dashboard/overview")
def dashboard(db: Session = Depends(db_session)):
    workspaces = db.scalar(select(func.count()).select_from(Workspace)) or 0
    # 均值口径：每份文档只取最新实例——旧实例是审计留痕（含早期 35/0 分误判
    # 存成 pass 的历史），不再参与平均分。
    latest_per_doc: dict[str, ReviewInstance] = {}
    for item in db.scalars(select(ReviewInstance).order_by(ReviewInstance.created_at.desc())).all():
        latest_per_doc.setdefault(item.node_id, item)
    reviews = list(latest_per_doc.values())
    average = round(sum(x.ai_score for x in reviews) / len(reviews), 1) if reviews else None
    increments = metrics.monthly_increments(db)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    month_reviews = [x for x in reviews if x.created_at and x.created_at.strftime("%Y-%m") == current_month]
    month_average = round(sum(x.ai_score for x in month_reviews) / len(month_reviews), 1) if month_reviews else None
    month_row = next((row for row in increments["rows"] if row["month"] == current_month), None)
    latest = []
    for doc in db.scalars(select(Document).where(Document.is_folder.is_(False)).order_by(Document.discovered_at.desc()).limit(8)).all():
        review = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == doc.node_id).order_by(ReviewInstance.created_at.desc()))
        count = db.scalar(select(func.count()).select_from(ReviewInstance).where(ReviewInstance.node_id == doc.node_id)) or 0
        latest.append(document_dict(doc, review, max(0, count - 1)))
    coverage_summary = metrics.coverage(db)["summary"] if workspaces else {"visible_workspaces": 0, "scanned": 0, "empty": 0, "excluded": 0}
    # The snapshot-frozen org note dates from the personal-authorization era
    # (23.5% coverage); rebuild it from whichever snapshot is the primary
    # baseline right now so a baseline switch never leaves a stale banner.
    org_context = dict(increments["baseline"]["definition"].get("org_context", {}))
    primary = db.get(HistoricalSnapshot, metrics.primary_snapshot_id(db))
    baseline_libs = len(((primary.definition or {}).get("workspaces") or {})) if primary else 0
    if primary and not baseline_libs:  # older snapshots store no workspace map
        baseline_libs = db.scalar(select(func.count(func.distinct(HistoricalFileNode.workspace_id)))
                                  .where(HistoricalFileNode.snapshot_id == primary.snapshot_id)) or 0
    org_context["note"] = (f"文件总量与月度增量按全量基线 {primary.snapshot_id if primary else '—'}"
                           f"（{baseline_libs or '—'} 库）+ 实时增量计算；服务身份已登记 {workspaces} 个知识库。")
    return {
        "metrics": {
            "workspace_count": workspaces,
            "total_files": increments["total_files"],
            "month_increment": month_row["total"] if month_row else 0,
            "average_ai_score": average,
            "month_average_score": month_average,
        },
        "coverage_summary": coverage_summary,
        "org_context": org_context,
        "monthly": increments["rows"][-14:],
        "yearly": increments["yearly"],
        "latest_documents": latest,
    }


@app.get("/api/v1/metrics/monthly-increments")
def metrics_monthly(year: str = Query(default="", pattern=r"^$|^\d{4}$"), db: Session = Depends(db_session)):
    return metrics.monthly_increments(db, year)


@app.get("/api/v1/metrics/coverage")
def metrics_coverage(db: Session = Depends(db_session)):
    return metrics.coverage(db)


@app.get("/api/v1/metrics/increments/tree")
def metrics_increments_tree(year: str = Query(default="", pattern=r"^$|^\d{4}$"),
                            month: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}$"),
                            department: str = "", biz_group: str = "", person: str = "",
                            db: Session = Depends(db_session)):
    """Drillable increment composition: no params -> years (recent first);
    year -> its months; month -> its days. People filters narrow the
    population via the bi_center employee cache."""
    return metrics.increments_tree(db, year=year, month=month, department=department,
                                   biz_group=biz_group, person=person)


@app.get("/api/v1/metrics/workspaces/{workspace_id}/months")
def metrics_workspace_months(workspace_id: str, db: Session = Depends(db_session)):
    return metrics.workspace_months(db, workspace_id)


@app.get("/api/v1/metrics/uploaders/months")
def uploader_months_api(db: Session = Depends(db_session)):
    return metrics.uploader_months(db)


@app.get("/api/v1/metrics/uploaders")
def uploaders_api(month: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}$"),
                  exclude_unmatched: bool = True, limit: int = Query(default=50, ge=1, le=200),
                  department: str = "", db: Session = Depends(db_session)):
    preview = metrics.uploaders(db, month, exclude_unmatched=False, limit=200)
    orgmap.ensure_employees(db, get_settings(), [item["user_id"] for item in preview["items"]])
    return metrics.uploaders(db, month, exclude_unmatched=exclude_unmatched, limit=limit, department=department)


@app.get("/api/v1/metrics/uploaders/{user_id}")
def uploader_detail_api(user_id: str, db: Session = Depends(db_session)):
    orgmap.ensure_employees(db, get_settings(), [user_id])
    return metrics.uploader_detail(db, user_id)


@app.get("/api/v1/metrics/uploaders/{user_id}/breakdown")
def uploader_breakdown_api(user_id: str, year: str = Query(default="", pattern=r"^$|^\d{4}$"),
                           month: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}$"),
                           db: Session = Depends(db_session)):
    orgmap.ensure_employees(db, get_settings(), [user_id])
    return metrics.uploader_breakdown(db, user_id, year=year, month=month)


@app.get("/api/v1/metrics/org")
def org_rollup_api(year: str = Query(default="", pattern=r"^$|^\d{4}$"),
                   month: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}$"),
                   db: Session = Depends(db_session)):
    preview = metrics.uploaders(db, "", exclude_unmatched=False, limit=500)
    orgmap.ensure_employees(db, get_settings(), [item["user_id"] for item in preview["items"]])
    return metrics.org_rollup(db, year=year, month=month)


@app.get("/api/v1/metrics/departments")
def departments_api(month: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}$"), db: Session = Depends(db_session)):
    return metrics.department_rollup(db, month)


@app.get("/api/v1/baseline/workspaces/{workspace_id}/folders")
def baseline_folders(workspace_id: str, snapshot_id: str = "", limit: int = Query(default=200, ge=1, le=500), db: Session = Depends(db_session)):
    """Directory groups within one snapshot. When the snapshot recorded folder
    nodes (the 2026-08 uploader scan does), folder names come back too."""
    snapshot = snapshot_id or metrics.uploader_snapshot_id(db) or metrics.primary_snapshot_id(db)
    rows = db.execute(
        select(HistoricalFileNode.parent_node_id, func.count(), func.min(HistoricalFileNode.source_created_at), func.max(HistoricalFileNode.source_created_at))
        .where(HistoricalFileNode.workspace_id == workspace_id, HistoricalFileNode.snapshot_id == snapshot,
               HistoricalFileNode.node_type != "folder")
        .group_by(HistoricalFileNode.parent_node_id)
        .order_by(func.count().desc()).limit(limit)).all()
    folder_names = {r[0]: r[1] for r in db.execute(
        select(HistoricalFileNode.node_id, HistoricalFileNode.name)
        .where(HistoricalFileNode.workspace_id == workspace_id, HistoricalFileNode.snapshot_id == snapshot,
               HistoricalFileNode.node_type == "folder")).all()}
    total_folders = len(rows)
    return {"workspace_id": workspace_id, "snapshot_id": snapshot, "total_folders": total_folders,
            "note": "" if folder_names else "该快照未记录目录名称，目录以节点 ID 标识。",
            "items": [{"parent_node_id": r[0] or "(根目录)", "folder_name": folder_names.get(r[0], ""),
                       "file_count": r[1], "earliest": (r[2] or "")[:10], "latest": (r[3] or "")[:10]} for r in rows]}


@app.get("/api/v1/baseline/files")
def baseline_files(workspace_id: str = "", folder: str = "", query: str = "", snapshot_id: str = "",
                   offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(db_session)):
    stmt = select(HistoricalFileNode).where(
        HistoricalFileNode.snapshot_id == (snapshot_id or metrics.uploader_snapshot_id(db) or metrics.primary_snapshot_id(db)),
        HistoricalFileNode.node_type != "folder")
    if workspace_id:
        stmt = stmt.where(HistoricalFileNode.workspace_id == workspace_id)
    if folder:
        stmt = stmt.where(HistoricalFileNode.parent_node_id == ("" if folder == "(根目录)" else folder))
    if query:
        stmt = stmt.where(HistoricalFileNode.name.contains(query))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(HistoricalFileNode.source_created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit,
            "items": [{"node_id": r.node_id, "workspace_id": r.workspace_id, "parent_node_id": r.parent_node_id,
                       "name": r.name, "extension": r.extension, "url": r.url, "size": r.size,
                       "creator_user_id": r.creator_user_id,
                       "created_at": r.source_created_at, "updated_at": r.source_updated_at} for r in rows]}


@app.get("/api/v1/files")
def files_unified(workspace_id: str = "", folder: str = "", query: str = "",
                  department: str = "", uploader: str = "",
                  offset: int = Query(default=0, ge=0),
                  limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(db_session)):
    """The single merged document list: primary-baseline snapshot rows plus the
    live increment mirror, deduplicated by node_id (the live row wins — it has
    fresher attribution and review state), soft-deleted nodes hidden. Newest
    first, so page one is "最新入库".

    Paging never materializes the union: each source is read newest-first with
    its own LIMIT (the snapshot side rides ix_hfn_snapshot_created) and the two
    ordered streams are merged in Python — page one costs ~2×(offset+limit)
    indexed rows instead of a 140k-row temp-table sort."""
    snapshot = metrics.uploader_snapshot_id(db) or metrics.primary_snapshot_id(db)
    # 不可见库（连续缺席/404 自动标记）双臂都不进当前检索——基线臂也要滤
    # （codex 第九轮 P1）；历史数据仍可走基线专用接口查询。
    inactive_ws = select(Workspace.workspace_id).where(Workspace.is_active.is_(False))
    base = select(
        HistoricalFileNode.node_id, HistoricalFileNode.workspace_id, HistoricalFileNode.name,
        HistoricalFileNode.extension, HistoricalFileNode.url,
        HistoricalFileNode.source_created_at.label("created_at"),
        HistoricalFileNode.creator_user_id.label("creator"),
    ).where(HistoricalFileNode.snapshot_id == snapshot, HistoricalFileNode.node_type != "folder",
            HistoricalFileNode.workspace_id.not_in(inactive_ws))
    live = select(
        Document.node_id, Document.workspace_id, Document.name,
        Document.extension, Document.url,
        Document.source_created_at.label("created_at"),
        Document.uploader_key.label("creator"),
    ).where(Document.is_folder.is_(False), Document.is_deleted.is_(False),
            Document.workspace_id.not_in(inactive_ws))  # 不可见库的增量不进检索
    if workspace_id:
        base = base.where(HistoricalFileNode.workspace_id == workspace_id)
        live = live.where(Document.workspace_id == workspace_id)
    if query:
        base = base.where(HistoricalFileNode.name.contains(query))
        live = live.where(Document.name.contains(query))
    if department:  # 知识库归属部门（workspaces.owner_department_name，宜搭回填）
        dept_ws = select(Workspace.workspace_id).where(Workspace.owner_department_name.contains(department))
        base = base.where(HistoricalFileNode.workspace_id.in_(dept_ws))
        live = live.where(Document.workspace_id.in_(dept_ws))
    if uploader:  # 姓名经 bi_center 员工缓存解析；同时接受原始 userId/uploader_key
        emp_ids = select(EmployeeMap.user_id).where(EmployeeMap.name.contains(uploader))
        base = base.where(or_(HistoricalFileNode.creator_user_id.in_(emp_ids),
                              HistoricalFileNode.creator_user_id == uploader))
        live = live.where(or_(Document.uploader_name.contains(uploader), Document.uploader_key == uploader))
    if folder:  # folder browsing is a snapshot feature; the live mirror stores no parent
        base = base.where(HistoricalFileNode.parent_node_id == ("" if folder == "(根目录)" else folder))
        total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = db.execute(base.order_by(HistoricalFileNode.source_created_at.desc(), HistoricalFileNode.node_id.desc())
                          .offset(offset).limit(limit)).all()
    else:
        base = base.where(HistoricalFileNode.node_id.not_in(select(Document.node_id)))
        need = offset + limit
        merged = sorted(
            db.execute(base.order_by(HistoricalFileNode.source_created_at.desc(),
                                     HistoricalFileNode.node_id.desc()).limit(need)).all() +
            db.execute(live.order_by(Document.source_created_at.desc(),
                                     Document.node_id.desc()).limit(need)).all(),
            key=lambda r: ((r.created_at or ""), r.node_id), reverse=True)
        rows = merged[offset:offset + limit]
        total = (db.scalar(select(func.count()).select_from(base.subquery())) or 0) + \
                (db.scalar(select(func.count()).select_from(live.subquery())) or 0)
    ids = [r.node_id for r in rows]
    docs = {d.node_id: d for d in db.scalars(select(Document).where(Document.node_id.in_(ids)))} if ids else {}
    latest_review: dict[str, ReviewInstance] = {}
    if ids:
        for rv in db.scalars(select(ReviewInstance).where(ReviewInstance.node_id.in_(ids))
                             .order_by(ReviewInstance.created_at.desc())):
            latest_review.setdefault(rv.node_id, rv)
    creators = {r.creator for r in rows if r.creator and r.node_id not in docs}
    emp = {e.user_id: e for e in db.scalars(select(EmployeeMap).where(EmployeeMap.user_id.in_(creators)))} if creators else {}
    items = []
    for r in rows:
        doc, rv, person = docs.get(r.node_id), latest_review.get(r.node_id), emp.get(r.creator)
        items.append({
            "node_id": r.node_id, "workspace_id": r.workspace_id, "name": r.name,
            "extension": r.extension, "url": (doc.url if doc and doc.url else r.url),
            "created_at": r.created_at, "source": "live" if doc else "baseline",
            "uploader_name": (doc.uploader_name if doc and doc.uploader_name else (person.name if person else "")),
            "department_name": (doc.department_name if doc and doc.department_name else (person.department_name if person else "")),
            "ai_score": round(rv.ai_score, 1) if rv else None,
            "verdict": rv.verdict if rv else "",
            "has_detail": doc is not None,
        })
    return {"total": total, "offset": offset, "limit": limit, "snapshot_id": snapshot, "items": items}


class NotifyTestRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    title: str = "知识库治理推送测试"
    text: str = "### 知识库治理推送测试\n如果你看到这条消息，机器人发送链路已打通。"


@app.post("/api/v1/notifications/test", dependencies=[Depends(require_admin)])
async def notification_test(payload: NotifyTestRequest):
    try:
        result = await DingtalkClient(get_settings()).send_robot_markdown([payload.user_id], payload.title, payload.text)
        return {"status": "sent", "result": result}
    except IntegrationError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


@app.get("/api/v1/reviews")
def reviews_list(verdict: str = Query(default="", pattern="^$|^(pass|manual_review|return)$"),
                 query: str = "", department: str = "", uploader: str = "",
                 offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(db_session)):
    stmt = select(ReviewInstance, Document).join(Document, Document.node_id == ReviewInstance.node_id)
    if verdict:
        stmt = stmt.where(ReviewInstance.verdict == verdict)
    if query:
        stmt = stmt.where(Document.name.contains(query))
    if department:  # 知识库归属部门
        stmt = stmt.where(Document.workspace_id.in_(
            select(Workspace.workspace_id).where(Workspace.owner_department_name.contains(department))))
    if uploader:
        stmt = stmt.where(or_(Document.uploader_name.contains(uploader), Document.uploader_key == uploader))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(ReviewInstance.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit,
            "items": [{"review_instance_id": r.review_instance_id, "node_id": d.node_id, "document_name": d.name,
                       "workspace_id": d.workspace_id, "uploader_name": d.uploader_name,
                       "department_name": d.department_name, "ai_score": round(r.ai_score, 1),
                       "verdict": r.verdict, "review_scope": r.review_scope, "trigger": r.trigger,
                       "rule_version": r.rule_version, "rule_config_ref": r.rule_config_ref,
                       "created_at": r.created_at.isoformat() if r.created_at else None}
                      for r, d in rows]}


@app.get("/api/v1/audit/status", dependencies=[Depends(require_admin)])
def audit_status_api(db: Session = Depends(db_session)):
    from .audit_bridge import bridge_status
    from .audit_pull import audit_status
    settings = get_settings()
    return {"enabled": settings.audit_pull_enabled, "interval_seconds": settings.audit_pull_interval_seconds,
            **audit_status(db),
            "bridge": {"enabled": settings.bridge_enabled, "scope": settings.bridge_scope,
                       "debounce_seconds": settings.bridge_debounce_seconds, **bridge_status(db)}}


@app.get("/api/v1/stream-events", dependencies=[Depends(require_admin)])
def stream_events(limit: int = Query(default=20, ge=1, le=100), event_type: str = "", db: Session = Depends(db_session)):
    from .db import StreamEvent
    stmt = select(StreamEvent).order_by(StreamEvent.received_at.desc()).limit(limit)
    if event_type:
        stmt = stmt.where(StreamEvent.event_type == event_type)
    return {"stream_enabled": get_settings().stream_enabled,
            "items": [{"id": e.id, "event_type": e.event_type, "biz_id": e.biz_id,
                       "received_at": e.received_at.isoformat() if e.received_at else None,
                       "payload": e.payload[:2000]} for e in db.scalars(stmt).all()]}


@app.get("/api/v1/notifications", dependencies=[Depends(require_admin)])
def notifications(status: str = "", limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(db_session)):
    stmt = select(Notification).order_by(Notification.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Notification.status == status)
    settings = get_settings()
    return {"notify_enabled": settings.notify_enabled,
            "robot_code": settings.robot_code or settings.dingtalk_app_key or "(未配置)",
            "items": [{"id": n.id, "node_id": n.node_id, "status": n.status, "error_code": n.error_code,
                       "title": n.title, "target_user_id": n.target_user_id,
                       "created_at": n.created_at.isoformat() if n.created_at else None,
                       "sent_at": n.sent_at.isoformat() if n.sent_at else None} for n in db.scalars(stmt).all()]}


@app.get("/api/v1/filters/departments")
def department_options(db: Session = Depends(db_session)):
    """知识库归属部门选项（workspaces.owner_department_name，来源：宜搭知识库登记）。"""
    rows = db.execute(select(Workspace.owner_department_name, func.count())
                      .where(Workspace.owner_department_name != "")
                      .group_by(Workspace.owner_department_name)
                      .order_by(func.count().desc())).all()
    return {"items": [{"name": r[0], "count": r[1]} for r in rows]}


WORKSPACE_LEVEL_PATTERN = re.compile(r"^([CDPIcdpi])[\-_—－]")
WORKSPACE_LEVEL_LABELS = {"C": "C-公司级", "D": "D-部门级", "P": "P-项目级", "I": "I-个人级"}


def workspace_level(name: str) -> str:
    match = WORKSPACE_LEVEL_PATTERN.match((name or "").strip())
    return match.group(1).upper() if match else "其他"


@app.get("/api/v1/workspaces")
def workspaces(query: str = "", level: str = "", department: str = "", creator: str = "", admin: str = "",
               offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
               db: Session = Depends(db_session)):
    """Registry listing with level classification (C/D/P/I by name prefix),
    search, filters and pagination. Counts and role rows are prefetched in
    bulk so the page costs a handful of queries, not one per workspace.
    不可见库（is_active=False，连续缺席/404 自动标记）不进列表。"""
    rows = db.scalars(select(Workspace).where(Workspace.is_active.is_(True))
                      .order_by(Workspace.name)).all()
    doc_counts = {ws_id: count for ws_id, count in db.execute(
        select(Document.workspace_id, func.count()).where(Document.is_folder.is_(False), Document.is_deleted.is_(False))
        .group_by(Document.workspace_id)).all()}
    roles: dict[str, dict[str, list[str]]] = {}
    for role_row in db.scalars(select(WorkspaceRole)).all():
        bucket = roles.setdefault(role_row.workspace_id, {"administrator": [], "reviewer": []})
        bucket.setdefault(role_row.role, []).append(role_row.display_name or role_row.employee_key)
    creator_names = {row.user_id: row.name for row in db.scalars(select(EmployeeMap)).all()}

    items = []
    for ws in rows:
        level_code = workspace_level(ws.name)
        admins = roles.get(ws.workspace_id, {}).get("administrator", [])
        creator_name = creator_names.get(ws.creator_key, "") or ws.creator_key
        entry = {"workspace_id": ws.workspace_id, "name": ws.name, "url": ws.url,
                 "level": level_code, "level_label": WORKSPACE_LEVEL_LABELS.get(level_code, "其他"),
                 "department_name": ws.owner_department_name, "biz_group_name": ws.owner_biz_group_name,
                 "creator": creator_name, "administrators": admins,
                 "reviewers": roles.get(ws.workspace_id, {}).get("reviewer", []),
                 "document_count": doc_counts.get(ws.workspace_id, 0),
                 "source_created_at": ws.source_created_at,
                 "synced_at": ws.synced_at.isoformat() if ws.synced_at else None}
        items.append(entry)

    def keep(entry: dict) -> bool:
        if query and query.lower() not in entry["name"].lower():
            return False
        if level and entry["level"] != level.upper():
            return False
        if department and department not in (entry["department_name"] or ""):
            return False
        if creator and creator not in (entry["creator"] or ""):
            return False
        if admin and not any(admin in (name or "") for name in entry["administrators"]):
            return False
        return True

    filtered = [entry for entry in items if keep(entry)]
    level_facets: dict[str, int] = {}
    for entry in filtered:
        level_facets[entry["level"]] = level_facets.get(entry["level"], 0) + 1
    return {"total": len(filtered), "offset": offset, "limit": limit,
            "levels": [{"level": key, "label": WORKSPACE_LEVEL_LABELS.get(key, "其他"), "count": value}
                       for key, value in sorted(level_facets.items())],
            "items": filtered[offset:offset + limit]}


@app.get("/api/v1/workspaces/{workspace_id}")
def workspace_detail(workspace_id: str, db: Session = Depends(db_session)):
    ws = db.get(Workspace, workspace_id)
    if not ws: raise HTTPException(404, "知识库不存在")
    result = workspace_dict(ws, db)
    result["monthly_document_counts"] = [{"month": x[0] or "未知", "count": x[1]} for x in db.execute(select(func.substr(Document.source_created_at, 1, 7), func.count()).where(Document.workspace_id == workspace_id, Document.is_folder.is_(False)).group_by(func.substr(Document.source_created_at, 1, 7))).all()]
    return result


@app.patch("/api/v1/workspaces/{workspace_id}/governance")
def patch_governance(workspace_id: str, body: GovernancePatch, db: Session = Depends(db_session)):
    ws = db.get(Workspace, workspace_id)
    if not ws: raise HTTPException(404, "知识库不存在")
    ws.owner_department_id, ws.owner_department_name, ws.owner_biz_group_name = body.owner_department_id, body.owner_department_name or "未映射", body.owner_biz_group_name or "未映射"
    db.query(WorkspaceRole).filter(WorkspaceRole.workspace_id == workspace_id).delete()
    db.add_all([WorkspaceRole(workspace_id=workspace_id, employee_key=x, display_name=x, role="administrator") for x in body.administrators] + [WorkspaceRole(workspace_id=workspace_id, employee_key=x, display_name=x, role="reviewer") for x in body.reviewers])
    db.commit()
    return workspace_dict(ws, db)


@app.get("/api/v1/documents")
def documents(workspace_id: str = "", query: str = "", db: Session = Depends(db_session)):
    stmt = select(Document).where(Document.is_folder.is_(False), Document.is_deleted.is_(False))
    if workspace_id: stmt = stmt.where(Document.workspace_id == workspace_id)
    if query: stmt = stmt.where(Document.name.contains(query))
    items = []
    for doc in db.scalars(stmt.order_by(Document.discovered_at.desc()).limit(200)).all():
        review = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == doc.node_id).order_by(ReviewInstance.created_at.desc()))
        total = db.scalar(select(func.count()).select_from(ReviewInstance).where(ReviewInstance.node_id == doc.node_id)) or 0
        items.append(document_dict(doc, review, max(0, total - 1)))
    return {"items": items}


@app.get("/api/v1/documents/{node_id}")
def document_detail(node_id: str, db: Session = Depends(db_session)):
    doc = db.get(Document, node_id)
    if not doc: raise HTTPException(404, "文档不存在")
    reviews = db.scalars(select(ReviewInstance).where(ReviewInstance.node_id == node_id).order_by(ReviewInstance.created_at.desc())).all()
    data = document_dict(doc, reviews[0] if reviews else None, max(0, len(reviews) - 1))
    data["reviews"] = [review_dict(item, max(0, len(reviews) - 1 - i)) for i, item in enumerate(reviews)]
    return data


@app.post("/api/v1/documents/{node_id}/reviews", status_code=202)
def enqueue_review(node_id: str, body: ReviewRequest, db: Session = Depends(db_session)):
    if not db.get(Document, node_id): raise HTTPException(404, "文档不存在")
    job = ReviewJob(job_id=str(uuid.uuid4()), node_id=node_id, trigger=body.trigger, requested_by=actor())
    db.add(job); db.commit()
    return {"job_id": job.job_id, "status": job.status, "document_body_persistence": "disabled"}


@app.get("/api/v1/review-jobs/{job_id}")
def review_job(job_id: str, db: Session = Depends(db_session)):
    job = db.get(ReviewJob, job_id)
    if not job: raise HTTPException(404, "任务不存在")
    return {"job_id": job.job_id, "status": job.status, "review_instance_id": job.result_review_instance_id, "error_code": job.error_code, "created_at": job.created_at.isoformat(), "finished_at": job.finished_at.isoformat() if job.finished_at else None}


@app.post("/api/v1/reviews/{review_instance_id}/decision")
def review_decision(review_instance_id: str, body: DecisionRequest, db: Session = Depends(db_session)):
    if not db.get(ReviewInstance, review_instance_id): raise HTTPException(404, "评审实例不存在")
    db.add(ReviewDecision(review_instance_id=review_instance_id, decision=body.decision, comment=body.comment, reviewer_key=actor())); db.commit()
    return {"review_instance_id": review_instance_id, "decision": body.decision, "reviewer_key": actor()}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return ("*" * 6 + key[-4:]) if len(key) > 8 else "*" * 8


def _model_dict(x: ModelConfig) -> dict:
    return {"id": x.id, "name": x.name, "provider": x.provider, "base_url": x.base_url,
            "model_name": x.model_name, "api_key_masked": _mask_key(x.api_key), "has_key": bool(x.api_key),
            "api_key_env_name": x.api_key_env_name, "temperature": x.temperature,
            "thinking_mode": x.thinking_mode, "timeout_seconds": x.timeout_seconds,
            "enabled": x.enabled, "version": x.version, "updated_at": x.updated_at.isoformat()}


def _model_snapshot(x: ModelConfig) -> dict:
    return {"name": x.name, "provider": x.provider, "base_url": x.base_url, "model_name": x.model_name,
            "api_key": x.api_key, "api_key_env_name": x.api_key_env_name, "temperature": x.temperature,
            "thinking_mode": x.thinking_mode, "timeout_seconds": x.timeout_seconds,
            "enabled": x.enabled, "version": x.version}


def _record_history(db: Session, item: ModelConfig, action: str) -> None:
    from .db import ModelConfigHistory
    db.add(ModelConfigHistory(config_id=item.id, action=action, snapshot=_model_snapshot(item), saved_by=actor()))


@app.get("/api/v1/model-configs", dependencies=[Depends(require_admin)])
def model_configs(db: Session = Depends(db_session)):
    return {"items": [_model_dict(x) for x in db.scalars(select(ModelConfig).order_by(ModelConfig.updated_at.desc())).all()],
            "rule_version": "V1.1",
            "api_key_policy": "API Key 可页面配置（仅存数据库、接口只回掩码）或环境变量注入；留空表示沿用已存密钥。"}


def _apply_model_body(item: ModelConfig, body: "ModelRequest") -> None:
    data = body.model_dump()
    if not data.get("api_key"):
        data.pop("api_key", None)  # blank means keep the stored key
    for key, value in data.items():
        setattr(item, key, value)


@app.post("/api/v1/model-configs", dependencies=[Depends(require_admin)])
def create_model(body: ModelRequest, db: Session = Depends(db_session)):
    if db.scalar(select(ModelConfig).where(ModelConfig.name == body.name)):
        raise HTTPException(409, "模型配置名称已存在")
    if body.enabled:
        db.query(ModelConfig).update({ModelConfig.enabled: False})
    item = ModelConfig()
    _apply_model_body(item, body)
    db.add(item); db.flush()
    _record_history(db, item, "create")
    db.commit(); db.refresh(item)
    return {"id": item.id, "name": item.name, "version": item.version}


@app.put("/api/v1/model-configs/{config_id}", dependencies=[Depends(require_admin)])
def update_model(config_id: int, body: ModelRequest, db: Session = Depends(db_session)):
    item = db.get(ModelConfig, config_id)
    if not item: raise HTTPException(404, "模型配置不存在")
    _record_history(db, item, "update")  # keep the pre-change state
    if body.enabled:
        db.query(ModelConfig).filter(ModelConfig.id != config_id).update({ModelConfig.enabled: False})
    _apply_model_body(item, body)
    db.commit(); return {"id": item.id, "name": item.name, "version": item.version}


@app.get("/api/v1/model-configs/{config_id}/history", dependencies=[Depends(require_admin)])
def model_history(config_id: int, db: Session = Depends(db_session)):
    from .db import ModelConfigHistory
    rows = db.scalars(select(ModelConfigHistory).where(ModelConfigHistory.config_id == config_id)
                      .order_by(ModelConfigHistory.saved_at.desc()).limit(30)).all()
    return {"items": [{"id": h.id, "action": h.action, "saved_by": h.saved_by,
                       "saved_at": h.saved_at.isoformat() if h.saved_at else None,
                       "model_name": (h.snapshot or {}).get("model_name", ""),
                       "base_url": (h.snapshot or {}).get("base_url", ""),
                       "temperature": (h.snapshot or {}).get("temperature"),
                       "thinking_mode": (h.snapshot or {}).get("thinking_mode", ""),
                       "version": (h.snapshot or {}).get("version", ""),
                       "api_key_masked": _mask_key((h.snapshot or {}).get("api_key", ""))} for h in rows]}


@app.post("/api/v1/model-configs/{config_id}/rollback/{history_id}", dependencies=[Depends(require_admin)])
def model_rollback(config_id: int, history_id: int, db: Session = Depends(db_session)):
    from .db import ModelConfigHistory
    item = db.get(ModelConfig, config_id)
    entry = db.get(ModelConfigHistory, history_id)
    if not item or not entry or entry.config_id != config_id:
        raise HTTPException(404, "配置或历史记录不存在")
    _record_history(db, item, "update")
    snapshot = dict(entry.snapshot or {})
    snapshot.pop("name", None)  # name is identity, not part of a rollback
    if snapshot.get("enabled"):
        db.query(ModelConfig).filter(ModelConfig.id != config_id).update({ModelConfig.enabled: False})
    for key, value in snapshot.items():
        setattr(item, key, value)
    _record_history(db, item, "rollback")
    db.commit()
    return {"id": item.id, "rolled_back_to": history_id}


@app.post("/api/v1/model-configs/{config_id}/connection-check", dependencies=[Depends(require_admin)])
async def model_check(config_id: int, db: Session = Depends(db_session)):
    item = db.get(ModelConfig, config_id)
    if not item: raise HTTPException(404, "模型配置不存在")
    return await model_connection_check({"enabled": item.enabled, "base_url": item.base_url, "model_name": item.model_name,
                                         "api_key": item.api_key, "api_key_env_name": item.api_key_env_name,
                                         "timeout_seconds": item.timeout_seconds}, get_settings())


# ---------------------------------------------------------------------------
# Scoring rule configuration: global default + per-department overrides.
# Viewing is open to every logged-in user (评分标准透明); editing requires the
# global admin list or, for a department row, its registered 维护人.
# ---------------------------------------------------------------------------

def _rule_identity(request: Request) -> dict:
    """Admin follows require_admin semantics; with auth off everything is open."""
    settings = get_settings()
    if not settings.auth_enabled:
        return {"union_id": "", "name": settings.default_actor, "is_admin": True}
    user = getattr(request.state, "user", None) or {}
    admins = {token.strip() for token in settings.admin_union_ids.split(",") if token.strip()}
    return {"union_id": user.get("union_id", ""), "name": user.get("name", ""),
            "is_admin": user.get("union_id", "") in admins}


def _rule_actor(identity: dict) -> str:
    return identity["name"] or identity["union_id"] or get_settings().default_actor


def _is_rule_editor(row: ScoringRuleConfig, union_id: str) -> bool:
    return bool(union_id) and any((e or {}).get("union_id") == union_id for e in (row.editors or []))


def _require_rule_edit(row: ScoringRuleConfig, identity: dict) -> None:
    if identity["is_admin"]:
        return
    if row.scope == "department" and _is_rule_editor(row, identity["union_id"]):
        return
    raise HTTPException(403, "仅管理员可修改全局规则；部门规则还需是该部门登记的维护人。")


def _rule_history(db: Session, row: ScoringRuleConfig, action: str, saved_by: str) -> None:
    db.add(ScoringRuleConfigHistory(config_id=row.id, action=action, saved_by=saved_by,
                                    snapshot={"scope": row.scope, "department_name": row.department_name,
                                              "config": row.config, "editors": row.editors, "version": row.version}))


def _rule_row_dict(row: ScoringRuleConfig) -> dict:
    return {"config_id": row.id, "scope": row.scope, "department_name": row.department_name,
            "config": effective_config(row.config), "editors": row.editors or [], "version": row.version,
            "updated_by": row.updated_by, "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _department_row(db: Session, department_name: str) -> ScoringRuleConfig:
    row = db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "department",
                                                    ScoringRuleConfig.department_name == department_name))
    if not row:
        raise HTTPException(404, "该部门尚无独立规则配置。")
    return row


def _sanitize_editors(raw: list[dict]) -> list[dict]:
    editors, seen = [], set()
    for item in raw:
        union_id = str((item or {}).get("union_id", "")).strip()[:128]
        if not union_id or union_id in seen:
            continue
        seen.add(union_id)
        editors.append({"union_id": union_id, "name": str((item or {}).get("name", "")).strip()[:128]})
    return editors


@app.get("/api/v1/scoring-rules")
def scoring_rules(request: Request, db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    rows = db.scalars(select(ScoringRuleConfig).order_by(ScoringRuleConfig.department_name)).all()
    global_row = next((r for r in rows if r.scope == "global"), None)
    departments = [r for r in rows if r.scope == "department"]
    candidates = {name for (name,) in db.execute(
        select(func.distinct(EmployeeMap.department_name)).where(EmployeeMap.matched.is_(True))).all()
        if name and name != "未映射"} | {d.department_name for d in departments}
    editable = [d.department_name for d in departments if _is_rule_editor(d, identity["union_id"])]
    return {
        "rule_version": RULE_VERSION,
        "catalog": catalog_dict(),
        "defaults": effective_config(None),
        "settings_rule_weight": get_settings().score_rule_weight,
        "global": _rule_row_dict(global_row) if global_row else
                  {"config_id": None, "scope": "global", "department_name": "", "config": effective_config(None),
                   "editors": [], "version": 0, "updated_by": "", "updated_at": None},
        "departments": [_rule_row_dict(d) for d in departments],
        "department_candidates": sorted(candidates),
        "permissions": {"is_admin": identity["is_admin"], "union_id": identity["union_id"],
                        "editable_departments": editable},
        "match_note": "评审时按上传人一级部门（bi_center 归属）匹配部门规则；无部门配置回落全局默认，再回落内置 V1.1。规则修改仅影响之后的评审。",
    }


@app.put("/api/v1/scoring-rules/global")
def save_global_rules(body: RuleConfigRequest, request: Request, db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    if not identity["is_admin"]:
        raise HTTPException(403, "仅管理员可修改全局评分规则。")
    actor_name = _rule_actor(identity)
    row = db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "global"))
    if row:
        _rule_history(db, row, "update", actor_name)
        row.version += 1
    else:
        row = ScoringRuleConfig(scope="global", department_name="", version=1)
        db.add(row)
    row.config = effective_config(body.config)
    row.updated_by = actor_name
    db.flush()
    if row.version == 1:
        _rule_history(db, row, "create", actor_name)
    db.commit()
    return _rule_row_dict(row)


@app.post("/api/v1/scoring-rules/departments", status_code=201)
def create_department_rules(body: RuleDepartmentCreateRequest, request: Request, db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    if not identity["is_admin"]:
        raise HTTPException(403, "仅管理员可为部门创建独立规则。")
    name = body.department_name.strip()
    if not name or name == "未映射":
        raise HTTPException(400, "部门名称无效。")
    if db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "department",
                                                 ScoringRuleConfig.department_name == name)):
        raise HTTPException(409, "该部门已有独立规则配置。")
    actor_name = _rule_actor(identity)
    global_row = db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "global"))
    # A department starts as a full copy of today's global effective config, so
    # later global edits never silently shift a department that opted out.
    row = ScoringRuleConfig(scope="department", department_name=name,
                            config=effective_config(global_row.config if global_row else None),
                            editors=[], version=1, updated_by=actor_name)
    db.add(row)
    db.flush()
    _rule_history(db, row, "create", actor_name)
    db.commit()
    return _rule_row_dict(row)


@app.put("/api/v1/scoring-rules/departments/{department_name}")
def save_department_rules(department_name: str, body: RuleConfigRequest, request: Request,
                          db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    row = _department_row(db, department_name)
    _require_rule_edit(row, identity)
    actor_name = _rule_actor(identity)
    _rule_history(db, row, "update", actor_name)
    row.config = effective_config(body.config)
    row.version += 1
    row.updated_by = actor_name
    db.commit()
    return _rule_row_dict(row)


@app.put("/api/v1/scoring-rules/departments/{department_name}/editors")
def save_department_editors(department_name: str, body: RuleEditorsRequest, request: Request,
                            db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    if not identity["is_admin"]:
        raise HTTPException(403, "仅管理员可指定部门规则维护人。")
    row = _department_row(db, department_name)
    actor_name = _rule_actor(identity)
    _rule_history(db, row, "update", actor_name)
    row.editors = _sanitize_editors(body.editors)
    row.version += 1
    row.updated_by = actor_name
    db.commit()
    return _rule_row_dict(row)


@app.delete("/api/v1/scoring-rules/departments/{department_name}")
def delete_department_rules(department_name: str, request: Request, db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    if not identity["is_admin"]:
        raise HTTPException(403, "仅管理员可删除部门规则。")
    row = _department_row(db, department_name)
    _rule_history(db, row, "delete", _rule_actor(identity))
    db.delete(row)
    db.commit()
    return {"deleted": department_name, "fallback": "global"}


@app.get("/api/v1/scoring-rules/{config_id}/history")
def scoring_rule_history(config_id: int, db: Session = Depends(db_session)):
    rows = db.scalars(select(ScoringRuleConfigHistory).where(ScoringRuleConfigHistory.config_id == config_id)
                      .order_by(ScoringRuleConfigHistory.saved_at.desc()).limit(30)).all()
    return {"items": [{"id": h.id, "action": h.action, "saved_by": h.saved_by,
                       "saved_at": h.saved_at.isoformat() if h.saved_at else None,
                       "version": (h.snapshot or {}).get("version"),
                       "scope": (h.snapshot or {}).get("scope", ""),
                       "department_name": (h.snapshot or {}).get("department_name", "")} for h in rows]}


@app.post("/api/v1/scoring-rules/{config_id}/rollback/{history_id}")
def scoring_rule_rollback(config_id: int, history_id: int, request: Request, db: Session = Depends(db_session)):
    identity = _rule_identity(request)
    row = db.get(ScoringRuleConfig, config_id)
    entry = db.get(ScoringRuleConfigHistory, history_id)
    if not row or not entry or entry.config_id != config_id:
        raise HTTPException(404, "配置或历史记录不存在。")
    _require_rule_edit(row, identity)
    actor_name = _rule_actor(identity)
    _rule_history(db, row, "update", actor_name)
    # Restore rule parameters only — editor lists stay admin-managed.
    row.config = effective_config((entry.snapshot or {}).get("config"))
    row.version += 1
    row.updated_by = actor_name
    _rule_history(db, row, "rollback", actor_name)
    db.commit()
    return {"config_id": row.id, "rolled_back_to": history_id, "version": row.version}


@app.get("/api/v1/diagnostics/connectivity", dependencies=[Depends(require_admin)])
async def connectivity(db: Session = Depends(db_session)):
    settings = get_settings(); ding = DingtalkClient(settings)
    if ding.configured() and settings.dingtalk_sync_operator_id:
        try: await ding.list_workspaces(settings.dingtalk_sync_operator_id, max_results=1); ding_result = {"status": "healthy", "message": "知识库读取权限与 operatorId 可用。"}
        except IntegrationError as exc: ding_result = {"status": "failed", "message": str(exc), "code": exc.code}
    else: ding_result = {"status": "not_configured", "message": "缺少钉钉应用凭据或 DINGTALK_SYNC_OPERATOR_ID。"}
    model = db.scalar(select(ModelConfig).where(ModelConfig.enabled.is_(True)))
    model_result = await model_connection_check({"enabled": bool(model), "base_url": model.base_url if model else "", "model_name": model.model_name if model else "", "api_key_env_name": model.api_key_env_name if model else "KG_MODEL_API_KEY", "timeout_seconds": model.timeout_seconds if model else 30}, settings)
    if not settings.watch_workspaces:
        watch_result = {"status": "not_configured", "message": "未配置 KG_WATCH_WORKSPACES，定向监控关闭。"}
    else:
        last_watch = db.scalar(select(SyncRun).where(SyncRun.mode.in_(("watch", "watch_seed", "bridge", "bridge_seed"))).order_by(SyncRun.created_at.desc()).limit(1))
        if not last_watch:
            watch_result = {"status": "pending", "message": f"已配置目标「{settings.watch_workspaces}」，等待 worker 首轮扫描。"}
        elif last_watch.status == "succeeded":
            watch_result = {"status": "healthy", "message": f"最近扫描（{last_watch.mode}）：文件 {last_watch.documents_seen}，新增 {last_watch.documents_new}，变更 {last_watch.documents_changed}，{last_watch.created_at.isoformat()}。"}
        else:
            watch_result = {"status": "failed", "message": f"最近扫描失败：{last_watch.error_code}", "code": last_watch.error_code}
    return {"items": [{"name": "钉钉知识库", **ding_result}, {"name": "bi_center 组织架构", **(await BiCenterClient(settings).check())}, {"name": "AI 评审模型", **model_result}, {"name": "定向监控 watcher", **watch_result}], "body_storage": "正文仅在 worker 内存/tmpfs 临时处理，数据库不保存正文。"}


@app.get("/api/v1/diagnostics/sync-runs", dependencies=[Depends(require_admin)])
def sync_runs(status: str = "", db: Session = Depends(db_session)):
    stmt = select(SyncRun).order_by(SyncRun.created_at.desc()).limit(30)
    if status:
        stmt = stmt.where(SyncRun.status == status)
    return {"items": [{"run_id": x.run_id, "status": x.status, "mode": x.mode,
                       "workspace_id": x.workspace_id, "workspace_name": x.workspace_name,
                       "workspaces_seen": x.workspaces_seen, "documents_seen": x.documents_seen,
                       "documents_new": x.documents_new, "documents_changed": x.documents_changed,
                       "error_code": x.error_code, "error_detail": x.error_detail,
                       "created_at": x.created_at.isoformat(),
                       "finished_at": x.finished_at.isoformat() if x.finished_at else None}
                      for x in db.scalars(stmt).all()]}


@app.post("/api/v1/sync-runs", status_code=202, dependencies=[Depends(require_admin)])
async def start_sync(mode: str = "incremental", db: Session = Depends(db_session)):
    run = await sync_from_dingtalk(db, get_settings(), mode)
    return {"run_id": run.run_id, "status": run.status, "error_code": run.error_code, "documents_new": run.documents_new, "documents_changed": run.documents_changed}


@app.post("/api/v1/watch/run", dependencies=[Depends(require_admin)])
async def run_watch_now(db: Session = Depends(db_session)):
    """On-demand watch cycle (same code path as the worker tick), for testing
    and for ops after adding a workspace to KG_WATCH_WORKSPACES."""
    settings = get_settings()
    if not settings.watch_workspaces:
        raise HTTPException(400, "未配置 KG_WATCH_WORKSPACES。")
    return await run_watch_cycle_async(db, settings)


# Read-only DingTalk proxy endpoints for interactive directory browsing; each request supplies the real operator UnionID.
@app.get("/api/v1/dingtalk/knowledge-bases")
async def dingtalk_workspaces(operator_id: str, next_token: str = "", max_results: int = 30):
    try: return await DingtalkClient(get_settings()).list_workspaces(operator_id, next_token, max_results)
    except IntegrationError as exc: raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


@app.get("/api/v1/dingtalk/knowledge-bases/{workspace_id}")
async def dingtalk_workspace(workspace_id: str, operator_id: str):
    try: return await DingtalkClient(get_settings()).workspace_detail(workspace_id, operator_id)
    except IntegrationError as exc: raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


@app.get("/api/v1/dingtalk/knowledge-bases/{workspace_id}/nodes")
async def dingtalk_nodes(workspace_id: str, operator_id: str, parent_node_id: str = "", next_token: str = "", max_results: int = 30):
    try: return await DingtalkClient(get_settings()).list_nodes(workspace_id, operator_id, parent_node_id, next_token, max_results)
    except IntegrationError as exc: raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


NO_STORE_SUFFIXES = (".html", ".js", ".css")


def _static_response(path: Path) -> FileResponse:
    # HTML/JS/CSS must revalidate every load — a stale cached app.js against a
    # newer API broke the dashboard once (2026-08-12). 304s keep it cheap.
    headers = {"Cache-Control": "no-cache"} if path.suffix in NO_STORE_SUFFIXES else None
    return FileResponse(path, headers=headers)


@app.get("/")
def index():
    return _static_response(ROOT / "static" / "index.html")


@app.get("/{path:path}")
def static_files(path: str):
    candidate = ROOT / "static" / path
    if candidate.is_file() and candidate.resolve().is_relative_to((ROOT / "static").resolve()): return _static_response(candidate)
    return _static_response(ROOT / "static" / "index.html")
