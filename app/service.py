from __future__ import annotations
import asyncio
import hashlib
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from .config import Settings
from .db import BridgeWalk, Document, FileAuditEvent, ModelConfig, ReviewInstance, ReviewJob, ScoringRuleConfig, SyncRun, WatchPlan, Workspace, WorkspaceRole, utcnow
from .fileclass import classify, review_classes
from .integrations import ADVISORY_GENRES, BiCenterClient, DingtalkClient, IntegrationError, model_score_content
from .notify import enqueue_review_notification
from .scoring import RULE_VERSION, effective_config, score_document, verdict_for


# 正文修改的合并评审窗（2026-08-14 拍板）：30 分钟无后续修改评一次；
# 持续编辑以首次置脏起 6 小时封顶，防止"永远还在改"永不评审。
MODIFY_MERGE_WINDOW_SECONDS = 1800
MODIFY_MERGE_MAX_SECONDS = 6 * 3600


def iso(value):
    return value.isoformat() if value else None


def _to_naive_utc(moment: datetime | None) -> datetime | None:
    """DB 读回的时间可能带/不带 tzinfo（驱动差异）；比较前统一为 naive UTC。"""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def robot_keys(settings: Settings) -> set[str]:
    """Machine accounts in either id form (numeric userId / UnionID)."""
    return {token.strip() for token in settings.robot_user_ids.split(",") if token.strip()}


def _at_or_after(value: str, cutoff: str) -> bool:
    """时间到达判定：双方按 ISO 解析（Z 归一为 +00:00，无时区按 UTC），
    解析失败退回按日字符串前缀比较。空值恒 False。"""
    if not value or not cutoff:
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        edge = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if edge.tzinfo is None:
            edge = edge.replace(tzinfo=timezone.utc)
        return moment >= edge
    except ValueError:
        return value[:10] >= cutoff[:10]


def is_robot_uploader(settings: Settings, *identifiers: str) -> bool:
    """数字员工识别：KG_ROBOT_USER_IDS 的 id/名字精确匹配 + 名称前缀兜底
    （默认"数字员工"）。bi_center 会把机器人解析成正式员工身份并替换
    uploader_key，只比对原始 id 拦不住其文档进评审（2026-08-13 生产实测）。"""
    robots = robot_keys(settings)
    prefixes = tuple(p.strip() for p in settings.robot_name_prefixes.split(",") if p.strip())
    for ident in identifiers:
        value = (ident or "").strip()
        if not value:
            continue
        if value in robots or (prefixes and value.startswith(prefixes)):
            return True
    return False


def review_dict(review: ReviewInstance, rerun_count: int = 0) -> dict:
    return {"review_instance_id": review.review_instance_id, "node_id": review.node_id, "ai_score": round(review.ai_score, 1), "verdict": review.verdict, "review_scope": review.review_scope, "content_note": review.content_note, "rule_version": review.rule_version, "rule_config_ref": review.rule_config_ref, "model_config_version": review.model_config_version, "trigger": review.trigger, "dimensions": review.dimensions, "findings": review.findings, "created_at": iso(review.created_at), "rerun_count": rerun_count}


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


def run_review(db: Session, settings: Settings, node_id: str, trigger: str = "manual") -> ReviewInstance | None:
    """返回 None 表示"正文与上次评审逐字节一致，本次跳过"（重命名后保存、
    格式化重存等假修改不重复出分/推送）；手动重评永不跳过。"""
    from .content import fetch_document_content

    doc = db.get(Document, node_id)
    if not doc:
        raise KeyError("document_not_found")
    # The only body holder is this local variable. It is never assigned to an ORM field or logged.
    content = ""
    scope = "metadata_only"
    content_note = ""
    if not doc.is_folder:
        try:
            content, content_note = asyncio.run(fetch_document_content(settings, doc))
        except IntegrationError as exc:
            content, content_note = "", f"fetch_failed:{exc.code}"[:64]
        except RuntimeError:
            content, content_note = "", "fetch_failed:runtime"
        if not content and settings.dingtalk_doc_content_url_template:
            try:
                content = asyncio.run(DingtalkClient(settings).fetch_ephemeral_content(node_id))
            except (IntegrationError, RuntimeError):
                content = ""
        scope = "full_content" if content else "metadata_only"
        if content:
            content_note = ""  # 正文拿到了，原因字段只服务于 metadata_only 的可观测性
    if content and trigger != "manual_rerun":
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint == (doc.content_fingerprint or ""):
            latest = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == node_id)
                               .order_by(ReviewInstance.created_at.desc()).limit(1))
            if latest is not None and latest.content_fingerprint == fingerprint:
                content = ""
                return None
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
        review_scope=scope, content_note=content_note, rule_version=RULE_VERSION, rule_config_ref=rule_config_ref(rule_row),
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
    # 第二道闸：入队侧漏网（老任务、批量导入）也不评审数字员工文档；
    # 详情页手动重评（manual_rerun）视为明确的人为意图，放行。
    doc = db.get(Document, job.node_id)
    if doc is not None and job.trigger != "manual_rerun" and is_robot_uploader(settings, doc.uploader_key, doc.uploader_name):
        job.status, job.error_code, job.finished_at = "skipped", "robot_uploader", utcnow()
        db.commit()
        return True
    job.status = "running"
    db.commit()
    try:
        review = run_review(db, settings, job.node_id, job.trigger)
        if review is None:
            job.status, job.error_code, job.finished_at = "skipped", "content_unchanged", utcnow()
        else:
            job.status, job.result_review_instance_id, job.finished_at = "succeeded", review.review_instance_id, utcnow()
    except KeyError:
        job.status, job.error_code, job.finished_at = "failed", "document_not_found", utcnow()
    except Exception:
        job.status, job.error_code, job.finished_at = "failed", "review_execution_failed", utcnow()
    db.commit()
    return True


HARVEST_BATCH = 100


def harvest_due_reviews(db: Session, settings: Settings) -> int:
    """修改合并窗收割：到期（30 分钟无新修改）或触顶（持续编辑满 6 小时）的
    脏文档入队一次合并评审（trigger=modify_merged）。评审侧还有指纹去重兜
    底——正文无实质变化不会重复出分。

    到期筛选在 SQL 层完成并按最早到期排序（codex 第八轮 P0：无条件取前
    100 条会被未到期行占满、饿死真正到期的文档）。窗口字段只在"本次变更
    已有评审兜着"（成功建任务，或有尚未启动的 pending 任务）时清零；撞上
    running 任务则原样保留——正文可能已被抓走，任务结束后下轮再收割补评
    （codex 第八轮 P0：清了标记又不建任务=修改永久丢失）。"""
    now = _to_naive_utc(utcnow())
    cap_edge = now - timedelta(seconds=MODIFY_MERGE_MAX_SECONDS)
    harvested = 0
    rows = db.scalars(select(Document)
                      .where(Document.review_due_at.is_not(None),
                             or_(Document.review_due_at <= now, Document.dirty_since <= cap_edge))
                      .order_by(Document.review_due_at.asc())
                      .limit(HARVEST_BATCH)).all()
    for doc in rows:
        if doc.is_deleted or doc.is_folder:
            doc.review_due_at = None
            doc.dirty_since = None
            continue
        active = db.scalar(select(ReviewJob).where(ReviewJob.node_id == doc.node_id,
                                                   ReviewJob.status.in_(("pending", "running"))).limit(1))
        if active is not None and active.status == "running":
            continue  # 保留到期标记，任务完成后下一轮收割
        if active is None:
            db.add(ReviewJob(job_id=str(uuid.uuid4()), node_id=doc.node_id,
                             trigger="modify_merged", requested_by="system"))
            harvested += 1
        doc.review_due_at = None
        doc.dirty_since = None
    if rows:
        db.commit()
    return harvested


async def _upsert_document(db: Session, settings: Settings, run: SyncRun, workspace_id: str, item: dict,
                           enqueue: bool, trigger_new: str, trigger_change: str,
                           parent_node_id: str = "") -> None:
    """Persist one listed node. Walks are mirror-only (2026-08-14 流程定稿)：
    评审只由审计事件链触发；全量核对发现的新增/变化只更新镜像与目录，
    绝不补评审（审计漏捕另行观测）。``enqueue`` 参数保留签名兼容，恒不
    入队。

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
    if parent_node_id or is_new:
        # 遍历路径天然知道父节点：写入并清掉"目录待定"（审计直建的文档
        # 在此被每月核对补准归属）。
        doc.parent_node_id = parent_node_id or doc.parent_node_id or ""
        doc.directory_pending = False
    if doc.is_deleted:  # seen again — a recycle-bin restore, not a new document
        doc.is_deleted = False
    doc.watch_misses = 0
    if is_new or changed:
        # 个别节点没有创建人 id（2026-08-14 生产实测三个库因此整轮回滚）：
        # None 必须钳成空串，后续 .isdigit()/机器人判定才不炸。
        doc.uploader_key = item.get("creator_id") or doc.uploader_key or ""
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
    # 2026-08-14 流程定稿：全量/增量遍历一律不触发评审——评审唯一入口是
    # 审计事件链（桥接确认后直建/更新文档并入队）。月度核对发现的上线后
    # 新增视为"审计漏捕"，只观测不补评（status_brief 有对应口径）。


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
                                           trigger_new="sync", trigger_change="sync_change",
                                           parent_node_id=parent_node_id)
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
    workspace ids using the operator's workspace list; the single token `*`
    watches every workspace the operator can see (org-wide rollout). Cached
    for an hour so a 5-minute tick does not spend five list calls every
    round."""
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
        if token == "*":
            matches = spaces
        else:
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
    run = SyncRun(run_id=str(uuid.uuid4()), status="running", mode=mode, workspace_id=workspace_id,
                  workspace_name=(space or {}).get("name", ""))
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
        ws.is_active = True  # 走到这说明库可见：曾被排除的自动复活
        ws.unreachable_misses = 0
        run.workspace_name = ws.name
        run.workspaces_seen = 1
        # 显式补种标记：只有"完整走完"的 seed 轮才置位。用"镜像非空"推断会把
        # 中途被重启打断的库误判为已补种，下一轮把剩余存量全当新增灌进评审。
        seeding = not bool(ws.watch_seeded)
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
                                           trigger_new="watch", trigger_change="watch_change",
                                           parent_node_id=parent_node_id)
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
        if seeding:
            ws.watch_seeded = True
        run.status, run.finished_at = "succeeded", utcnow()
    except IntegrationError as exc:
        db.rollback()
        run.status, run.error_code, run.finished_at = "failed", f"{exc.code}:{exc.status_code}"[:64], utcnow()
        run.error_detail = str(exc)[:512]
        if exc.code == "workspace_not_visible":
            # 库疑似已删除或失权（P-06 德国DG路由器场景）：计一次缺席，连续
            # 两次才自动退出活跃集合——单次列表不完整不足以停掉正常库
            # （codex 第八轮 P1）；重新可见时上面自动复活并清零计数。
            ws_row = db.get(Workspace, workspace_id)
            if ws_row is not None:
                ws_row.unreachable_misses = (ws_row.unreachable_misses or 0) + 1
                if ws_row.unreachable_misses >= 2:
                    ws_row.is_active = False
    except Exception as exc:
        db.rollback()
        run.status, run.error_code, run.finished_at = "failed", "watch_execution_failed", utcnow()
        # 只留异常类型：原始 message 可能携带文档名/SQL 参数，不得入错误留痕
        run.error_detail = type(exc).__name__
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
    """Synchronous full cycle — kept for the manual /api/v1/watch/run endpoint
    and scripts; the worker loop uses run_watch_slice instead."""
    return asyncio.run(run_watch_cycle_async(db, settings))


_watch_rotation: dict = {"queue": [], "cycle_ids": []}


async def run_watch_slice_async(db: Session, settings: Settings, batch: int = 2) -> dict:
    """Walk at most ``batch`` workspaces of the current rotation, then return.
    The worker drains review jobs / notifications / audit pull between slices,
    so a 140-workspace cycle cannot starve them for hours (2026-08-13
    finding). An exhausted rotation reports cycle_completed and refills on the
    next call. cycle_ids 记录本轮周期的成员：整轮走完时它就是"这次全量真实
    看到的库"，缺席者由结账逻辑自动标记不可见。"""
    targets = await resolve_watch_targets(settings)
    by_id = {t["workspace_id"]: t for t in targets["resolved"]}
    if not _watch_rotation["queue"]:
        _watch_rotation["queue"] = list(by_id.keys())
        _watch_rotation["cycle_ids"] = list(by_id.keys())
    walked = []
    while _watch_rotation["queue"] and len(walked) < max(1, batch):
        ws_id = _watch_rotation["queue"].pop(0)
        target = by_id.get(ws_id)
        if not target:  # renamed/revoked since the rotation was built
            continue
        run = await watch_workspace(db, settings, ws_id, target.get("space"))
        walked.append({"workspace_id": ws_id, "name": target["name"], "run_id": run.run_id,
                       "mode": run.mode, "status": run.status, "documents_seen": run.documents_seen,
                       "documents_new": run.documents_new, "documents_changed": run.documents_changed,
                       "error_code": run.error_code})
    completed = not _watch_rotation["queue"]
    return {"walked": walked, "remaining": len(_watch_rotation["queue"]), "total": len(by_id),
            "unresolved": targets["unresolved"], "cycle_completed": completed,
            "cycle_workspace_ids": (list(_watch_rotation["cycle_ids"]) or list(by_id.keys())) if completed else []}


def run_watch_slice(db: Session, settings: Settings, batch: int = 2) -> dict:
    return asyncio.run(run_watch_slice_async(db, settings, batch))


CN_TZ = timezone(timedelta(hours=8))  # 业务时区（中国无夏令时，固定偏移即可）


def _scan_days(settings: Settings) -> list[int]:
    days = sorted({int(token) for token in settings.scan_days.split(",")
                   if token.strip().isdigit() and 1 <= int(token) <= 28})
    return days or [10, 24]


def current_scan_due(settings: Settings, today: date | None = None) -> str:
    """最近一个已到达的计划扫描日（YYYY-MM-DD，Asia/Shanghai 口径）。"""
    today = today or datetime.now(CN_TZ).date()
    candidates: list[date] = []
    for delta in (0, -1):
        year, month = today.year, today.month + delta
        if month == 0:
            year, month = year - 1, 12
        for day in _scan_days(settings):
            candidate = date(year, month, day)
            if candidate <= today:
                candidates.append(candidate)
    return max(candidates).isoformat() if candidates else today.isoformat()


def _watch_plan(db: Session) -> WatchPlan:
    plan = db.get(WatchPlan, 1)
    if plan is None:
        plan = WatchPlan(id=1)
        db.add(plan)
        db.commit()
    return plan


def _seeding_pending(db: Session) -> int:
    """待补种数只数活跃库：已删除/失权的库（is_active=False）永远补不了种，
    不能让它把 worker 永久拖回连续轮巡（P-06 德国DG路由器教训）。"""
    return db.scalar(select(func.count()).select_from(Workspace)
                     .where(Workspace.watch_seeded.is_(False), Workspace.is_active.is_(True))) or 0


def watch_scan_decision(db: Session, settings: Settings) -> str:
    """"scan" = 需要推进全量巡走；"idle" = 本期计划已完成，只等下个计划日。
    首轮补种未完成时始终 scan（2026-08-14 决策：补种完成后停止连续轮巡，
    全量扫描固定每月 10/24 日；期间的变化发现交给审计增量拉取 + 桥接）。"""
    if _seeding_pending(db):
        return "scan"
    return "idle" if _watch_plan(db).completed_for == current_scan_due(settings) else "scan"


def mark_scan_cycle_complete(db: Session, settings: Settings,
                             seen_workspace_ids: set[str] | None = None) -> None:
    """整轮走完时结账：补种全清后才消费计划日（首轮本身就是一次全量）。
    ``seen_workspace_ids`` 是本轮解析到的全部库 id：注册表里整轮缺席的库
    计一次缺席，连续两轮缺席才标记不可见（codex 第八轮 P1：单次列表不完整
    不能误停正常库；中途新注册的库也因此天然豁免）。传空/None 跳过判决，
    防解析瞬时失败误伤全量。"""
    if seen_workspace_ids:
        for ws in db.scalars(select(Workspace).where(Workspace.is_active.is_(True))).all():
            if ws.workspace_id in seen_workspace_ids:
                if ws.unreachable_misses:
                    ws.unreachable_misses = 0
            else:
                ws.unreachable_misses = (ws.unreachable_misses or 0) + 1
                if ws.unreachable_misses >= 2:
                    ws.is_active = False
    if _seeding_pending(db):
        db.commit()
        return
    plan = _watch_plan(db)
    plan.completed_for = current_scan_due(settings)
    plan.completed_at = utcnow()
    db.commit()


def sweep_stale_runs(db: Session) -> int:
    """Mark runs a dead process left in "running" as interrupted. Called once
    at worker boot — with a single worker, anything still "running" then is a
    leftover, not live work (7 such rows accumulated by 2026-08-13)。同时清空
    巡走残留队列：直建流程下走整库只剩试点级兜底，跨重启的旧行（如已删库
    P-06）只会反复 404 烧预算，一次性出清。"""
    stale = db.scalars(select(SyncRun).where(SyncRun.status == "running")).all()
    for run in stale:
        run.status, run.error_code, run.finished_at = "failed", "interrupted_by_restart", utcnow()
    for row in db.scalars(select(BridgeWalk)).all():
        db.delete(row)
    db.commit()
    return len(stale)


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
