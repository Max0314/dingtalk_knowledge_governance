from __future__ import annotations
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from . import metrics, orgmap
from .config import get_settings
from .db import Document, HistoricalFileNode, ModelConfig, Notification, ReviewDecision, ReviewInstance, ReviewJob, SessionLocal, SyncRun, Workspace, WorkspaceRole, init_db
from .integrations import BiCenterClient, DingtalkClient, IntegrationError, model_connection_check
from .service import document_dict, review_dict, seed_demo, sync_from_dingtalk, workspace_dict

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


class GovernancePatch(BaseModel):
    owner_department_id: str = ""
    owner_department_name: str = ""
    owner_biz_group_name: str = ""
    administrators: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    trigger: str = "manual"


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


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dingtalk_knowledge_governance", "document_body_persistence": "disabled"}


@app.get("/api/v1/dashboard/overview")
def dashboard(db: Session = Depends(db_session)):
    workspaces = db.scalar(select(func.count()).select_from(Workspace)) or 0
    reviews = db.scalars(select(ReviewInstance).order_by(ReviewInstance.created_at.desc())).all()
    average = round(sum(x.ai_score for x in reviews) / len(reviews), 1) if reviews else None
    increments = metrics.monthly_increments(db)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    month_row = next((row for row in increments["rows"] if row["month"] == current_month), None)
    latest = []
    for doc in db.scalars(select(Document).where(Document.is_folder.is_(False)).order_by(Document.discovered_at.desc()).limit(8)).all():
        review = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == doc.node_id).order_by(ReviewInstance.created_at.desc()))
        count = db.scalar(select(func.count()).select_from(ReviewInstance).where(ReviewInstance.node_id == doc.node_id)) or 0
        latest.append(document_dict(doc, review, max(0, count - 1)))
    coverage_summary = metrics.coverage(db)["summary"] if workspaces else {"visible_workspaces": 0, "scanned": 0, "empty": 0, "excluded": 0}
    return {
        "metrics": {
            "workspace_count": workspaces,
            "total_files": increments["total_files"],
            "month_increment": month_row["total"] if month_row else 0,
            "average_ai_score": average,
        },
        "coverage_summary": coverage_summary,
        "org_context": increments["baseline"]["definition"].get("org_context", {}),
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


class NotifyTestRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    title: str = "知识库治理推送测试"
    text: str = "### 知识库治理推送测试\n如果你看到这条消息，机器人发送链路已打通。"


@app.post("/api/v1/notifications/test")
async def notification_test(payload: NotifyTestRequest):
    try:
        result = await DingtalkClient(get_settings()).send_robot_markdown([payload.user_id], payload.title, payload.text)
        return {"status": "sent", "result": result}
    except IntegrationError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


@app.get("/api/v1/reviews")
def reviews_list(verdict: str = Query(default="", pattern="^$|^(pass|manual_review|return)$"),
                 query: str = "", offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(db_session)):
    stmt = select(ReviewInstance, Document).join(Document, Document.node_id == ReviewInstance.node_id)
    if verdict:
        stmt = stmt.where(ReviewInstance.verdict == verdict)
    if query:
        stmt = stmt.where(Document.name.contains(query))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(ReviewInstance.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit,
            "items": [{"review_instance_id": r.review_instance_id, "node_id": d.node_id, "document_name": d.name,
                       "workspace_id": d.workspace_id, "uploader_name": d.uploader_name,
                       "department_name": d.department_name, "ai_score": round(r.ai_score, 1),
                       "verdict": r.verdict, "review_scope": r.review_scope, "trigger": r.trigger,
                       "rule_version": r.rule_version, "created_at": r.created_at.isoformat() if r.created_at else None}
                      for r, d in rows]}


@app.get("/api/v1/stream-events")
def stream_events(limit: int = Query(default=20, ge=1, le=100), event_type: str = "", db: Session = Depends(db_session)):
    from .db import StreamEvent
    stmt = select(StreamEvent).order_by(StreamEvent.received_at.desc()).limit(limit)
    if event_type:
        stmt = stmt.where(StreamEvent.event_type == event_type)
    return {"stream_enabled": get_settings().stream_enabled,
            "items": [{"id": e.id, "event_type": e.event_type, "biz_id": e.biz_id,
                       "received_at": e.received_at.isoformat() if e.received_at else None,
                       "payload": e.payload[:2000]} for e in db.scalars(stmt).all()]}


@app.get("/api/v1/notifications")
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


@app.get("/api/v1/workspaces")
def workspaces(db: Session = Depends(db_session)):
    return {"items": [workspace_dict(ws, db) for ws in db.scalars(select(Workspace).order_by(Workspace.name)).all()]}


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


@app.get("/api/v1/model-configs")
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


@app.post("/api/v1/model-configs")
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


@app.put("/api/v1/model-configs/{config_id}")
def update_model(config_id: int, body: ModelRequest, db: Session = Depends(db_session)):
    item = db.get(ModelConfig, config_id)
    if not item: raise HTTPException(404, "模型配置不存在")
    _record_history(db, item, "update")  # keep the pre-change state
    if body.enabled:
        db.query(ModelConfig).filter(ModelConfig.id != config_id).update({ModelConfig.enabled: False})
    _apply_model_body(item, body)
    db.commit(); return {"id": item.id, "name": item.name, "version": item.version}


@app.get("/api/v1/model-configs/{config_id}/history")
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


@app.post("/api/v1/model-configs/{config_id}/rollback/{history_id}")
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


@app.post("/api/v1/model-configs/{config_id}/connection-check")
async def model_check(config_id: int, db: Session = Depends(db_session)):
    item = db.get(ModelConfig, config_id)
    if not item: raise HTTPException(404, "模型配置不存在")
    return await model_connection_check({"enabled": item.enabled, "base_url": item.base_url, "model_name": item.model_name,
                                         "api_key": item.api_key, "api_key_env_name": item.api_key_env_name,
                                         "timeout_seconds": item.timeout_seconds}, get_settings())


@app.get("/api/v1/diagnostics/connectivity")
async def connectivity(db: Session = Depends(db_session)):
    settings = get_settings(); ding = DingtalkClient(settings)
    if ding.configured() and settings.dingtalk_sync_operator_id:
        try: await ding.list_workspaces(settings.dingtalk_sync_operator_id, max_results=1); ding_result = {"status": "healthy", "message": "知识库读取权限与 operatorId 可用。"}
        except IntegrationError as exc: ding_result = {"status": "failed", "message": str(exc), "code": exc.code}
    else: ding_result = {"status": "not_configured", "message": "缺少钉钉应用凭据或 DINGTALK_SYNC_OPERATOR_ID。"}
    model = db.scalar(select(ModelConfig).where(ModelConfig.enabled.is_(True)))
    model_result = await model_connection_check({"enabled": bool(model), "base_url": model.base_url if model else "", "model_name": model.model_name if model else "", "api_key_env_name": model.api_key_env_name if model else "KG_MODEL_API_KEY", "timeout_seconds": model.timeout_seconds if model else 30}, settings)
    return {"items": [{"name": "钉钉知识库", **ding_result}, {"name": "bi_center 组织架构", **(await BiCenterClient(settings).check())}, {"name": "AI 评审模型", **model_result}], "body_storage": "正文仅在 worker 内存/tmpfs 临时处理，数据库不保存正文。"}


@app.get("/api/v1/diagnostics/sync-runs")
def sync_runs(db: Session = Depends(db_session)):
    return {"items": [{"run_id": x.run_id, "status": x.status, "mode": x.mode, "workspaces_seen": x.workspaces_seen, "documents_seen": x.documents_seen, "documents_new": x.documents_new, "documents_changed": x.documents_changed, "error_code": x.error_code, "created_at": x.created_at.isoformat(), "finished_at": x.finished_at.isoformat() if x.finished_at else None} for x in db.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(30)).all()]}


@app.post("/api/v1/sync-runs", status_code=202)
async def start_sync(mode: str = "incremental", db: Session = Depends(db_session)):
    run = await sync_from_dingtalk(db, get_settings(), mode)
    return {"run_id": run.run_id, "status": run.status, "error_code": run.error_code, "documents_new": run.documents_new, "documents_changed": run.documents_changed}


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


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/{path:path}")
def static_files(path: str):
    candidate = ROOT / "static" / path
    if candidate.is_file() and candidate.resolve().is_relative_to((ROOT / "static").resolve()): return FileResponse(candidate)
    return FileResponse(ROOT / "static" / "index.html")
