from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .config import Settings
from .db import Document, ModelConfig, ReviewInstance, ReviewJob, SyncRun, Workspace, WorkspaceRole, utcnow
from .integrations import BiCenterClient, DingtalkClient, IntegrationError, model_score_content
from .notify import enqueue_review_notification
from .scoring import RULE_VERSION, score_document


def iso(value):
    return value.isoformat() if value else None


def review_dict(review: ReviewInstance, rerun_count: int = 0) -> dict:
    return {"review_instance_id": review.review_instance_id, "node_id": review.node_id, "ai_score": round(review.ai_score, 1), "verdict": review.verdict, "review_scope": review.review_scope, "rule_version": review.rule_version, "model_config_version": review.model_config_version, "trigger": review.trigger, "dimensions": review.dimensions, "findings": review.findings, "created_at": iso(review.created_at), "rerun_count": rerun_count}


def document_dict(doc: Document, review: ReviewInstance | None = None, rerun_count: int = 0) -> dict:
    data = {"node_id": doc.node_id, "workspace_id": doc.workspace_id, "name": doc.name, "category": doc.category, "extension": doc.extension, "url": doc.url, "size": doc.size, "word_count": doc.word_count, "is_folder": doc.is_folder, "source_created_at": doc.source_created_at, "source_updated_at": doc.source_updated_at, "uploader_key": doc.uploader_key, "uploader_name": doc.uploader_name, "department_name": doc.department_name, "biz_group_name": doc.biz_group_name, "org_matched": doc.org_matched, "discovered_at": iso(doc.discovered_at), "rerun_count": rerun_count}
    if review:
        data["latest_review"] = review_dict(review, rerun_count)
    return data


def workspace_dict(ws: Workspace, db: Session) -> dict:
    total = db.scalar(select(func.count()).select_from(Document).where(Document.workspace_id == ws.workspace_id, Document.is_folder.is_(False), Document.is_deleted.is_(False))) or 0
    roles = db.scalars(select(WorkspaceRole).where(WorkspaceRole.workspace_id == ws.workspace_id)).all()
    return {"workspace_id": ws.workspace_id, "name": ws.name, "description": ws.description, "url": ws.url, "source_created_at": ws.source_created_at, "source_updated_at": ws.source_updated_at, "creator_key": ws.creator_key, "owner_department_id": ws.owner_department_id, "owner_department_name": ws.owner_department_name, "owner_biz_group_name": ws.owner_biz_group_name, "document_count": total, "administrators": [r.display_name or r.employee_key for r in roles if r.role == "administrator"], "reviewers": [r.display_name or r.employee_key for r in roles if r.role == "reviewer"], "synced_at": iso(ws.synced_at)}


def active_model(db: Session) -> ModelConfig | None:
    return db.scalar(select(ModelConfig).where(ModelConfig.enabled.is_(True)).order_by(ModelConfig.updated_at.desc()))


def run_review(db: Session, settings: Settings, node_id: str, trigger: str = "manual") -> ReviewInstance:
    doc = db.get(Document, node_id)
    if not doc:
        raise KeyError("document_not_found")
    # The only body holder is this local variable. It is never assigned to an ORM field or logged.
    content = ""
    scope = "metadata_only"
    if settings.dingtalk_doc_content_url_template and not doc.is_folder:
        try:
            content = asyncio.run(DingtalkClient(settings).fetch_ephemeral_content(node_id))
            scope = "full_content" if content else "metadata_only"
        except (IntegrationError, RuntimeError):
            content = ""
    result = score_document(doc.name, content or f"文档信息\n版本：\n适用范围：\n")
    model = active_model(db)
    if content and model and settings.model_allow_content_transfer:
        model_result = asyncio.run(model_score_content({"base_url": model.base_url, "model_name": model.model_name,
                                                        "api_key": model.api_key, "api_key_env_name": model.api_key_env_name,
                                                        "temperature": model.temperature, "thinking_mode": model.thinking_mode,
                                                        "timeout_seconds": model.timeout_seconds}, content, doc.name))
        if model_result:
            result["ai_score"] = model_result["score"]
            result["findings"].extend(model_result["findings"])
            result["dimensions"]["model"] = {"label": "模型补充评审", "deduction": 0, "cap": 0, "findings": model_result["findings"]}
    instance = ReviewInstance(
        review_instance_id=str(uuid.uuid4()), node_id=node_id, ai_score=result["ai_score"], verdict=result["verdict"],
        review_scope=scope, rule_version=RULE_VERSION,
        model_config_version=(model.version if model and content and settings.model_allow_content_transfer else "rule-engine"), trigger=trigger,
        content_fingerprint=result["fingerprint"], dimensions=result["dimensions"], findings=result["findings"],
    )
    if result["fingerprint"]:
        doc.content_fingerprint = result["fingerprint"]
    db.add(instance)
    db.flush()
    enqueue_review_notification(db, settings, doc, instance)
    db.commit()
    db.refresh(instance)
    # Explicitly release body reference after persistence of derived data only.
    content = ""
    return instance


def process_next_job(db: Session, settings: Settings) -> bool:
    job = db.scalar(select(ReviewJob).where(ReviewJob.status == "pending").order_by(ReviewJob.created_at).limit(1))
    if not job:
        return False
    job.status = "running"
    db.commit()
    try:
        review = run_review(db, settings, job.node_id, job.trigger)
        job.status, job.result_review_instance_id, job.finished_at = "succeeded", review.review_instance_id, utcnow()
    except KeyError:
        job.status, job.error_code, job.finished_at = "failed", "document_not_found", utcnow()
    except Exception:
        job.status, job.error_code, job.finished_at = "failed", "review_execution_failed", utcnow()
    db.commit()
    return True


async def sync_from_dingtalk(db: Session, settings: Settings, mode: str = "incremental") -> SyncRun:
    run = SyncRun(run_id=str(uuid.uuid4()), status="running", mode=mode)
    db.add(run); db.commit()
    client = DingtalkClient(settings)
    try:
        async def persist_node(workspace_id: str, item: dict) -> None:
            run.documents_seen += 1
            doc = db.get(Document, item["node_id"])
            is_new = doc is None
            changed = doc is not None and doc.source_updated_at != item.get("updated_at", "")
            if not doc:
                doc = Document(node_id=item["node_id"], workspace_id=workspace_id, name=item["name"])
                db.add(doc); run.documents_new += 1
            elif changed:
                run.documents_changed += 1
            for field in ("name", "category", "extension", "url", "size", "word_count", "source_created_at", "source_updated_at"):
                setattr(doc, field, item.get(field, "") if item.get(field) is not None else "")
            doc.is_folder, doc.uploader_key, doc.discovered_at = item["has_children"], item.get("creator_id", ""), utcnow()
            # bi_center is the single source of organization truth. Never infer a department locally.
            resolved = await BiCenterClient(settings).resolve_batch([{"unionId": doc.uploader_key}], datetime.now(timezone.utc).strftime("%Y-%m"))
            identity = resolved[0] if resolved else {}
            if identity.get("matched") and identity.get("includeInOfficialStats"):
                doc.uploader_key = identity.get("employeeKey", doc.uploader_key)
                doc.uploader_name = identity.get("employeeName", "")
                doc.department_name = identity.get("departmentName", "")
                doc.biz_group_name = identity.get("bizGroupName", "")
                doc.org_matched = True
            else:
                doc.uploader_name, doc.department_name, doc.biz_group_name, doc.org_matched = "未映射", "未映射", "未映射", False
            if (is_new or changed) and not doc.is_folder:
                db.flush()
                pending = db.scalar(select(ReviewJob).where(ReviewJob.node_id == doc.node_id, ReviewJob.status.in_(("pending", "running"))))
                if not pending:
                    db.add(ReviewJob(job_id=str(uuid.uuid4()), node_id=doc.node_id,
                                     trigger="sync" if is_new else "sync_change", requested_by="system"))

        async def walk(workspace_id: str, parent_node_id: str) -> None:
            next_token = ""
            while True:
                page = await client.list_nodes(workspace_id, settings.dingtalk_sync_operator_id, parent_node_id, next_token)
                for item in page["items"]:
                    await persist_node(workspace_id, item)
                    if item["has_children"]:
                        await walk(workspace_id, item["node_id"])
                next_token = page.get("next_token", "")
                if not next_token:
                    return

        workspace_next_token = ""
        while True:
            page = await client.list_workspaces(settings.dingtalk_sync_operator_id, workspace_next_token)
            run.workspaces_seen += len(page["items"])
            for raw in page["items"]:
                ws = db.get(Workspace, raw["workspace_id"])
                if not ws:
                    ws = Workspace(workspace_id=raw["workspace_id"], name=raw["name"])
                    db.add(ws)
                for field in ("name", "description", "url"):
                    setattr(ws, field, raw.get(field, ""))
                ws.source_created_at, ws.source_updated_at, ws.creator_key, ws.synced_at = raw.get("created_at", ""), raw.get("updated_at", ""), raw.get("creator_id", ""), utcnow()
                await walk(ws.workspace_id, raw.get("root_node_id", ""))
            workspace_next_token = page.get("next_token", "")
            if not workspace_next_token:
                break
        run.status, run.finished_at = "succeeded", utcnow()
    except IntegrationError as exc:
        run.status, run.error_code, run.finished_at = "failed", exc.code, utcnow()
    except Exception:
        run.status, run.error_code, run.finished_at = "failed", "sync_execution_failed", utcnow()
    db.commit()
    return run


def seed_demo(db: Session) -> None:
    if db.get(Workspace, "demo-workspace"):
        return
    ws = Workspace(workspace_id="demo-workspace", name="研发知识库", description="仅用于本地验收的示例元数据", owner_department_name="研发中心", owner_biz_group_name="平台研发组", creator_key="demo-creator")
    db.add(ws)
    db.add_all([WorkspaceRole(workspace_id=ws.workspace_id, employee_key="demo-admin", role="administrator", display_name="知识库管理员"), WorkspaceRole(workspace_id=ws.workspace_id, employee_key="demo-reviewer", role="reviewer", display_name="知识库审核员")])
    docs = [
        Document(node_id="demo-001", workspace_id=ws.workspace_id, name="接口发布规范_V1.1.md", extension="md", source_created_at="2026-08-01", source_updated_at="2026-08-01", uploader_name="张三", uploader_key="demo-zhang", department_name="研发中心", biz_group_name="平台研发组", org_matched=True),
        Document(node_id="demo-002", workspace_id=ws.workspace_id, name="数据治理说明_V1.0.md", extension="md", source_created_at="2026-07-18", source_updated_at="2026-07-25", uploader_name="李四", uploader_key="demo-li", department_name="研发中心", biz_group_name="数据平台组", org_matched=True),
        Document(node_id="demo-003", workspace_id=ws.workspace_id, name="服务巡检清单_V1.2.md", extension="md", source_created_at="2026-08-02", source_updated_at="2026-08-02", uploader_name="王五", uploader_key="demo-wang", department_name="研发中心", biz_group_name="平台研发组", org_matched=True),
    ]
    db.add_all(docs); db.commit()
    for doc in docs:
        run_review(db, __import__("app.config", fromlist=["get_settings"]).get_settings(), doc.node_id, "demo_seed")
