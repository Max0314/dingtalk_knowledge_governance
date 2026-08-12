from __future__ import annotations
import asyncio
import time
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .config import Settings
from .db import Document, ModelConfig, ReviewInstance, ReviewJob, ScoringRuleConfig, SyncRun, Workspace, WorkspaceRole, utcnow
from .fileclass import classify, review_classes
from .integrations import ADVISORY_GENRES, BiCenterClient, DingtalkClient, IntegrationError, model_score_content
from .notify import enqueue_review_notification
from .scoring import RULE_VERSION, effective_config, score_document, verdict_for


def iso(value):
    return value.isoformat() if value else None


def robot_keys(settings: Settings) -> set[str]:
    """Machine accounts in either id form (numeric userId / UnionID)."""
    return {token.strip() for token in settings.robot_user_ids.split(",") if token.strip()}


def review_dict(review: ReviewInstance, rerun_count: int = 0) -> dict:
    return {"review_instance_id": review.review_instance_id, "node_id": review.node_id, "ai_score": round(review.ai_score, 1), "verdict": review.verdict, "review_scope": review.review_scope, "rule_version": review.rule_version, "rule_config_ref": review.rule_config_ref, "model_config_version": review.model_config_version, "trigger": review.trigger, "dimensions": review.dimensions, "findings": review.findings, "created_at": iso(review.created_at), "rerun_count": rerun_count}


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


def resolve_rule_config(db: Session, department_name: str = "") -> ScoringRuleConfig | None:
    """Uploader's 一级部门 override first, then the global row, else None
    (builtin V1.1 defaults). "未映射" never matches a department row."""
    row = None
    if department_name and department_name != "未映射":
        row = db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "department",
                                                        ScoringRuleConfig.department_name == department_name))
    return row or db.scalar(select(ScoringRuleConfig).where(ScoringRuleConfig.scope == "global"))


def rule_config_ref(row: ScoringRuleConfig | None) -> str:
    if not row:
        return "builtin"
    return f"department:{row.department_name}@v{row.version}" if row.scope == "department" else f"global@v{row.version}"


def run_review(db: Session, settings: Settings, node_id: str, trigger: str = "manual") -> ReviewInstance:
    from .content import fetch_document_content

    doc = db.get(Document, node_id)
    if not doc:
        raise KeyError("document_not_found")
    # The only body holder is this local variable. It is never assigned to an ORM field or logged.
    content = ""
    scope = "metadata_only"
    if not doc.is_folder:
        try:
            content, _source = asyncio.run(fetch_document_content(settings, doc))
        except (IntegrationError, RuntimeError):
            content = ""
        if not content and settings.dingtalk_doc_content_url_template:
            try:
                content = asyncio.run(DingtalkClient(settings).fetch_ephemeral_content(node_id))
            except (IntegrationError, RuntimeError):
                content = ""
        scope = "full_content" if content else "metadata_only"
    rule_row = resolve_rule_config(db, doc.department_name)
    rule_cfg = effective_config(rule_row.config if rule_row else None)
    result = score_document(doc.name, content or f"文档信息\n版本：\n适用范围：\n", doc.file_class or "document", rule_cfg)
    model = active_model(db)
    if content and model and settings.model_allow_content_transfer:
        model_result = asyncio.run(model_score_content({"base_url": model.base_url, "model_name": model.model_name,
                                                        "api_key": model.api_key, "api_key_env_name": model.api_key_env_name,
                                                        "temperature": model.temperature, "thinking_mode": model.thinking_mode,
                                                        "timeout_seconds": model.timeout_seconds}, content, doc.name,
                                                       doc.file_class or "document"))
        if model_result:
            # Dual-track scoring: the rule-compliance score stays reproducible,
            # the model judges content quality within its detected genre, and
            # the stored score is their weighted composite. Genres without a
            # document shape (test cases, reports, minutes) demote the
            # document-shaped rule dimensions to advisory — flagged, not fined.
            genre = model_result.get("genre", "")
            if genre in ADVISORY_GENRES:
                for key in ("metadata", "abstract", "structure", "rag"):
                    dim = result["dimensions"].get(key)
                    if dim and dim.get("deduction"):
                        dim["advisory"] = True
            rule_score = 100 - sum(dim["deduction"] for dim in result["dimensions"].values()
                                   if not dim.get("advisory"))
            model_score = model_result["score"]
            configured_weight = rule_cfg.get("rule_weight")
            weight = min(max(configured_weight if configured_weight is not None else settings.score_rule_weight, 0.0), 1.0)
            composite = round(weight * rule_score + (1 - weight) * model_score)
            result["findings"].extend(model_result["findings"])
            result["dimensions"]["model"] = {
                "label": "模型内容评审", "deduction": 0, "cap": 0,
                "genre": genre, "model_score": model_score, "rule_score": rule_score,
                "composite": composite, "rule_weight": weight,
                "model_dimensions": model_result.get("dimensions", {}),
                "findings": model_result["findings"],
            }
            result["ai_score"] = composite
            result["verdict"] = verdict_for(composite, rule_cfg)
    instance = ReviewInstance(
        review_instance_id=str(uuid.uuid4()), node_id=node_id, ai_score=result["ai_score"], verdict=result["verdict"],
        review_scope=scope, rule_version=RULE_VERSION, rule_config_ref=rule_config_ref(rule_row),
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


async def _upsert_document(db: Session, settings: Settings, run: SyncRun, workspace_id: str, item: dict,
                           enqueue: bool, trigger_new: str, trigger_change: str) -> None:
    """Persist one listed node and, when appropriate, queue a review.

    ``item`` is a ``normalize_node`` dict — the source timestamps live under
    ``created_at``/``updated_at`` (mapping them onto ``source_*`` columns here
    fixes the earlier field-name mismatch that zeroed both columns).
    """
    run.documents_seen += 1
    doc = db.get(Document, item["node_id"])
    is_new = doc is None
    changed = doc is not None and doc.source_updated_at != (item.get("updated_at") or "")
    if not doc:
        doc = Document(node_id=item["node_id"], workspace_id=workspace_id, name=item["name"])
        db.add(doc); run.documents_new += 1
    elif changed:
        run.documents_changed += 1
    for field in ("name", "category", "extension", "url"):
        setattr(doc, field, item.get(field) or "")
    doc.size = item.get("size") or 0
    doc.word_count = item.get("word_count") or 0
    doc.source_created_at = item.get("created_at") or ""
    doc.source_updated_at = item.get("updated_at") or ""
    doc.is_folder = item["has_children"]
    doc.file_class = classify(doc.extension, doc.is_folder)
    if doc.is_deleted:  # seen again — a recycle-bin restore, not a new document
        doc.is_deleted = False
    doc.watch_misses = 0
    if is_new or changed:
        doc.uploader_key = item.get("creator_id", "") or doc.uploader_key
        # bi_center is the single source of organization truth. Never infer a
        # department locally. The old wiki namespace reports creators as
        # UnionIDs, the new one as numeric userIds — send the matching key.
        identity_input = {"userId": doc.uploader_key} if doc.uploader_key.isdigit() else {"unionId": doc.uploader_key}
        resolved = await BiCenterClient(settings).resolve_batch([identity_input], datetime.now(timezone.utc).strftime("%Y-%m")) if doc.uploader_key else []
        identity = resolved[0] if resolved else {}
        if identity.get("matched") and identity.get("includeInOfficialStats"):
            doc.uploader_key = identity.get("employeeKey", doc.uploader_key)
            doc.uploader_name = identity.get("employeeName", "")
            doc.department_name = identity.get("departmentName", "")
            doc.biz_group_name = identity.get("bizGroupName", "")
            doc.org_matched = True
        else:
            doc.uploader_name, doc.department_name, doc.biz_group_name, doc.org_matched = "未映射", "未映射", "未映射", False
    robots = robot_keys(settings)
    uploaded_by_robot = doc.uploader_key in robots or (item.get("creator_id", "") in robots)
    if (enqueue and (is_new or changed) and not doc.is_folder and not uploaded_by_robot
            and doc.file_class in review_classes(settings.review_classes)):
        db.flush()
        pending = db.scalar(select(ReviewJob).where(ReviewJob.node_id == doc.node_id, ReviewJob.status.in_(("pending", "running"))))
        if not pending:
            db.add(ReviewJob(job_id=str(uuid.uuid4()), node_id=doc.node_id,
                             trigger=trigger_new if is_new else trigger_change, requested_by="system"))


async def sync_from_dingtalk(db: Session, settings: Settings, mode: str = "incremental") -> SyncRun:
    run = SyncRun(run_id=str(uuid.uuid4()), status="running", mode=mode)
    db.add(run); db.commit()
    client = DingtalkClient(settings)
    try:
        async def walk(workspace_id: str, parent_node_id: str) -> None:
            next_token = ""
            while True:
                page = await client.list_nodes(workspace_id, settings.dingtalk_sync_operator_id, parent_node_id, next_token)
                for item in page["items"]:
                    await _upsert_document(db, settings, run, workspace_id, item, enqueue=True,
                                           trigger_new="sync", trigger_change="sync_change")
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


# ---------------------------------------------------------------------------
# Targeted workspace watcher (pillar replacing the falsified Stream channel):
# a cheap complete walk of a handful of configured workspaces detects new,
# changed and deleted files without any DingTalk-side configuration.
# ---------------------------------------------------------------------------

_watch_cache: dict = {"at": 0.0, "key": "", "resolved": [], "unresolved": []}
WATCH_RESOLUTION_TTL_SECONDS = 3600


async def resolve_watch_targets(settings: Settings, force: bool = False) -> dict:
    """Map KG_WATCH_WORKSPACES tokens (id, exact name or name fragment) to
    workspace ids using the operator's workspace list. Cached for an hour so a
    5-minute tick does not spend five list calls every round."""
    tokens = [token.strip() for token in settings.watch_workspaces.split(",") if token.strip()]
    if not tokens:
        return {"resolved": [], "unresolved": []}
    key = "|".join(tokens)
    if not force and _watch_cache["key"] == key and time.time() - _watch_cache["at"] < WATCH_RESOLUTION_TTL_SECONDS and _watch_cache["resolved"]:
        return {"resolved": _watch_cache["resolved"], "unresolved": _watch_cache["unresolved"]}
    client = DingtalkClient(settings)
    spaces: list[dict] = []
    next_token = ""
    while True:
        page = await client.list_workspaces(settings.dingtalk_sync_operator_id, next_token)
        spaces.extend(page["items"])
        next_token = page.get("next_token", "")
        if not next_token:
            break
    resolved: list[dict] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()
    for token in tokens:
        matches = ([space for space in spaces if space["workspace_id"] == token]
                   or [space for space in spaces if space.get("name", "") == token]
                   or [space for space in spaces if token in space.get("name", "")])
        if not matches:
            unresolved.append(token)
            continue
        for space in matches:
            if space["workspace_id"] in seen_ids:
                continue
            seen_ids.add(space["workspace_id"])
            # Keep the whole normalized listing item: the detail endpoint fails
            # for some (personal) spaces, so the walk must reuse rootNodeId and
            # metadata from the listing — exactly what the baseline scanner does.
            resolved.append({"token": token, "workspace_id": space["workspace_id"], "name": space.get("name", ""),
                             "space": space})
    _watch_cache.update(at=time.time(), key=key, resolved=resolved, unresolved=unresolved)
    return {"resolved": resolved, "unresolved": unresolved}


async def watch_workspace(db: Session, settings: Settings, workspace_id: str, space: dict | None = None,
                          mode: str = "watch") -> SyncRun:
    """One complete walk of one workspace. The first walk (empty mirror) seeds
    without queueing reviews — 482 pre-existing files must not flood the queue
    on day one; every later walk queues reviews for new/changed files and
    counts absences toward soft deletion.

    ``space`` is the normalized workspace listing item; when given, the walk
    trusts its rootNodeId instead of calling the detail endpoint, which fails
    for some personal spaces."""
    run = SyncRun(run_id=str(uuid.uuid4()), status="running", mode=mode)
    db.add(run); db.commit()
    client = DingtalkClient(settings)
    operator = settings.dingtalk_sync_operator_id
    try:
        raw = space if space and space.get("root_node_id") else None
        if raw is None:
            # The detail endpoint 400/404s on personal/team spaces; fall back
            # to scanning the operator's listing, which always carries the root.
            try:
                raw = await client.workspace_detail(workspace_id, operator)
            except IntegrationError:
                raw = None
            if not raw or not raw.get("root_node_id"):
                next_token = ""
                while True:
                    page = await client.list_workspaces(operator, next_token)
                    raw = next((item for item in page["items"] if item["workspace_id"] == workspace_id), None)
                    next_token = page.get("next_token", "")
                    if raw or not next_token:
                        break
            if not raw:
                raise IntegrationError("workspace_not_visible", "工作区不在操作者可见列表中。", 404)
        ws = db.get(Workspace, workspace_id)
        if not ws:
            ws = Workspace(workspace_id=workspace_id, name=raw.get("name", "") or workspace_id)
            db.add(ws)
        for field in ("name", "description", "url"):
            if raw.get(field):
                setattr(ws, field, raw[field])
        ws.source_created_at = raw.get("created_at", "") or ws.source_created_at
        ws.source_updated_at = raw.get("updated_at", "") or ws.source_updated_at
        ws.creator_key = raw.get("creator_id", "") or ws.creator_key
        ws.synced_at = utcnow()
        run.workspaces_seen = 1
        seeding = (db.scalar(select(func.count()).select_from(Document).where(Document.workspace_id == workspace_id)) or 0) == 0
        if seeding:
            run.mode = f"{mode}_seed"
        seen: set[str] = set()

        async def walk(parent_node_id: str) -> None:
            next_token = ""
            while True:
                page = await client.list_nodes(workspace_id, operator, parent_node_id, next_token, max_results=100)
                for item in page["items"]:
                    # The listing demonstrably re-emits nodes (a personal-space
                    # walk yielded ~2x rows); a second sight in the same cycle
                    # must be skipped or the pending INSERT collides on the PK.
                    if item["node_id"] in seen:
                        continue
                    seen.add(item["node_id"])
                    await _upsert_document(db, settings, run, workspace_id, item, enqueue=not seeding,
                                           trigger_new="watch", trigger_change="watch_change")
                    if item["has_children"]:
                        await walk(item["node_id"])
                next_token = page.get("next_token", "")
                if not next_token:
                    return

        await walk(raw.get("root_node_id", "") or "")
        # Only a complete, successful walk may accuse a document of deletion.
        for doc in db.scalars(select(Document).where(Document.workspace_id == workspace_id, Document.is_deleted.is_(False))).all():
            if doc.node_id in seen:
                continue
            doc.watch_misses += 1
            if doc.watch_misses >= max(1, settings.watch_delete_misses):
                doc.is_deleted = True
        run.status, run.finished_at = "succeeded", utcnow()
    except IntegrationError as exc:
        db.rollback()
        run.status, run.error_code, run.finished_at = "failed", f"{exc.code}:{exc.status_code}"[:64], utcnow()
    except Exception:
        db.rollback()
        run.status, run.error_code, run.finished_at = "failed", "watch_execution_failed", utcnow()
    try:
        db.commit()
    except Exception:
        db.rollback()
        run.status, run.error_code, run.finished_at = "failed", "watch_commit_failed", utcnow()
        db.commit()
    return run


async def run_watch_cycle_async(db: Session, settings: Settings) -> dict:
    targets = await resolve_watch_targets(settings)
    runs = []
    for target in targets["resolved"]:
        run = await watch_workspace(db, settings, target["workspace_id"], target.get("space"))
        runs.append({"workspace_id": target["workspace_id"], "name": target["name"], "run_id": run.run_id,
                     "mode": run.mode, "status": run.status, "documents_seen": run.documents_seen,
                     "documents_new": run.documents_new, "documents_changed": run.documents_changed,
                     "error_code": run.error_code})
    return {"resolved": targets["resolved"], "unresolved": targets["unresolved"], "runs": runs}


def run_watch_cycle(db: Session, settings: Settings) -> dict:
    """Synchronous entry for the worker loop."""
    return asyncio.run(run_watch_cycle_async(db, settings))


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
