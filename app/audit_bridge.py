"""Audit-event -> knowledge-base-node bridge.

The audit trail reliably says *that* a knowledge-base write happened, but —
verified live 2026-08-11 — wiki uploads all carry one shared org-wide
storage-space id, so the event does NOT say in *which* library. 2026-08-14
流程定稿后，桥接是评审的唯一入口，每条 wiki 写事件按动作白名单分流：

  1. 分类（codex P0-1）：上传/新建/导入类 -> 立即评审；正文修改 -> 合并
     窗延时评审（30 分钟无后续修改评一次，持续编辑 6 小时封顶）；重命名/
     移动 -> 只更新元数据；删除/恢复 -> 仅本地匹配（bizId/历史确认映射）
     做软删/恢复，恢复不评审；协作/分享/权限等纯协同动作 -> 直接终态忽略，
     不消耗任何定位额度。
  2. locator：按文件名走存储搜索 + wiki 批量节点查询拿精确 node id，并与
     节点载荷时间/大小互证（"系统不认识" ≠ 新节点）。
  3. 确认后直接建档/更新该节点（一个事件只动一个节点，零整库遍历）。
     完成语义：评审类/修改类事件以"下载键在文档上"为 done；元数据/删除/
     恢复/忽略各有专属终态，绝不伪装成 done。

space_map remains as an observability tally. Cost model: no wiki writes ->
no locator calls; the legacy sweep walk only fires at pilot scale
(governed <= bridge_sweep_max_governed).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid as uuid_module

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import (BridgeWalk, Document, FileAuditEvent, HistoricalFileNode, HistoricalSnapshot, Notification,
                 ReviewJob, SpaceMap, Workspace, utcnow)
from .fileclass import classify, review_classes
from .integrations import BiCenterClient, DingtalkClient, IntegrationError
from .service import MODIFY_MERGE_WINDOW_SECONDS, watch_workspace

logger = logging.getLogger("kg.bridge")

BATCH = 500
# 每轮远程定位的事件上限与时间预算。名称搜索与节点查询都受并发上限约束，
# 使高频上传不会积压，同时不会独占 worker 或压垮钉钉接口。
WIKI_LOCATE_BUDGET = 20
WIKI_LOCATE_CONCURRENCY = 5
LOCATE_TIME_BUDGET_SECONDS = 30
# 每轮桥接巡走的库数上限；没走到的库留在持久化队列里下一轮续走。
WALK_BUDGET = 5
# confirmed 待完成事件的全局收尾额度（纯 DB 操作，不外呼，可以宽松）。
CONFIRM_FINISH_BUDGET = 50
# 未能确认匹配的事件转入死信的时限：终态带 dead_letter_* 原因可观测，
# 不伪装成功（发现与评审由 watcher 轮巡 + KG_REVIEW_SINCE 兜底）。
GIVE_UP_AFTER_MS = 48 * 3600 * 1000

# 操作类型白名单（codex P0-1 + 2026-08-14 拍板）。分两档：
#
# 评审触发类（review/modify）**整名精确匹配**（codex 第九轮 P0：裸子串会把
# "修改文档标题""更新知识库描述"这类非正文操作放进评审）。名单以生产流水
# 实际观察到的动作名为主（知识库上传文件/知识库修改文件/复制或转发文件/
# 文档导入/创建文档/创建副本），补少量无歧义的"动词+文件/文档"完整名；
# 名单外一律 unknown 终态计数，人工确认后再扩。
#
# 非评审类（ignore/restore/delete/metadata）保持子串匹配——分错档也不会
# 触发评审，宽召回换取删除/协同动作的覆盖。匹配顺序即语义优先级：
# "从回收站恢复"先于"删除"，"移除成员"先于"移除"。
IGNORE_ACTIONS = ("协作", "成员", "分享", "公开", "外链", "权限", "评论", "链接", "收藏", "浏览", "预览", "下载")
RESTORE_ACTIONS = ("恢复", "还原")
DELETE_ACTIONS = ("删除", "撤回", "移除", "回收站")
METADATA_ACTIONS = ("重命名", "移动")
REVIEW_ACTIONS_EXACT = frozenset({
    "知识库上传文件", "上传文件", "新建文件", "新建文档", "创建文件", "创建文档",
    "创建副本", "复制或转发文件", "文档导入", "覆盖上传", "覆盖文件",
})
MODIFY_ACTIONS_EXACT = frozenset({
    "知识库修改文件", "修改文件", "修改文档", "修改文件正文",
    "编辑文件", "编辑文档", "更新文件", "更新文档",
})
# 覆盖动作复用旧节点，created_at 不会贴近事件——互证时不算"创建"，应使用
# updated_at。"覆盖文件"是生产审计已出现的真实动作名。
CREATION_ACTIONS_EXACT = REVIEW_ACTIONS_EXACT - {"覆盖上传", "覆盖文件"}

# In-memory debounce: workspace_id -> monotonic seconds of the last bridge
# walk. Worker restarts forget it; one extra walk is harmless. Failed walks
# are evicted so the next event retries immediately.
_last_walk: dict[str, float] = {}


def _is_wiki_write(event: FileAuditEvent) -> bool:
    return "知识库" in (event.action_view or "") or (event.module_view or "") == "团队空间"


def _name_candidates(event: FileAuditEvent) -> list[str]:
    names = [event.resource]
    if event.extension and not event.resource.endswith("." + event.extension):
        names.append(f"{event.resource}.{event.extension}")
    return [name for name in names if name]


def _extension_mismatch(event: FileAuditEvent, node: dict) -> bool:
    """Audit extension is advisory; this only feeds non-sensitive counters."""
    return bool(event.extension and node.get("extension")
                and event.extension.lower() != str(node["extension"]).lower())


def _latest_snapshot_id(db: Session) -> str:
    return db.scalar(select(HistoricalSnapshot.snapshot_id)
                     .order_by(HistoricalSnapshot.collected_at.desc()).limit(1)) or ""


def _unique_node_match(db: Session, event: FileAuditEvent, snapshot_id: str) -> str:
    """node_id when the resource name matches exactly one known node."""
    names = _name_candidates(event)
    if not names:
        return ""
    nodes = {doc.node_id for doc in db.scalars(
        select(Document).where(or_(*[Document.name == name for name in names])).limit(5)).all()}
    if snapshot_id:
        nodes |= {row[0] for row in db.execute(
            select(HistoricalFileNode.node_id)
            .where(HistoricalFileNode.snapshot_id == snapshot_id,
                   HistoricalFileNode.name.in_(names)).distinct().limit(5)).all()}
    return nodes.pop() if len(nodes) == 1 else ""


def _tally_space(db: Session, event: FileAuditEvent) -> None:
    space_id = event.target_space_id or ""
    if not space_id:
        return
    entry = db.get(SpaceMap, space_id)
    if not entry:
        entry = SpaceMap(space_id=space_id)
        db.add(entry)
        db.flush()
    entry.event_count += 1
    entry.last_event_gmt = max(entry.last_event_gmt or 0, event.gmt_create or 0)


def _governed_workspaces(db: Session) -> list[str]:
    return [row[0] for row in db.execute(select(Document.workspace_id).distinct()).all() if row[0]]


def _attach_numeric_id(db: Session, event: FileAuditEvent, doc: Document | None = None) -> bool:
    """The event's bizId IS the file's numeric storage dentry id (verified by
    cross-download); hand it to uploaded files so reviews can fetch the body.

    Native .adoc nodes use the official document export API and must never be
    given a synthetic storage key. Attaching a key is metadata enrichment only:
    the event's explicit review/modify action decides whether a review is due.
    Historical no-body records are deliberately not backfilled (2026-08-17)."""
    if not event.matched_node_id or not (event.biz_id or "").isdigit():
        return False
    doc = doc or db.get(Document, event.matched_node_id)
    if not doc or doc.storage_dentry_id or (doc.extension or "").lower() == "adoc":
        return False
    doc.storage_dentry_id = event.biz_id
    return True


def _body_fetch_ready(doc: Document) -> bool:
    """Whether this node has the locator needed by its body adapter."""
    return (doc.extension or "").lower() == "adoc" or bool(doc.storage_dentry_id)


def _action_kind(event: FileAuditEvent) -> str:
    """审计动作分类：review（立即评审）/ modify（合并窗评审）/ metadata
    （只更新镜像）/ delete、restore（软删/恢复，绝不评审）/ ignore（纯协同
    动作，直接终态）/ unknown（不在任何白名单：绝不评审，终态可观测）。
    评审触发类整名精确匹配，非评审类子串匹配（见名单注释）。"""
    view = (event.action_view or "").strip()
    for keywords, kind in ((IGNORE_ACTIONS, "ignore"), (RESTORE_ACTIONS, "restore"),
                           (DELETE_ACTIONS, "delete"), (METADATA_ACTIONS, "metadata")):
        if any(keyword in view for keyword in keywords):
            return kind
    if view in REVIEW_ACTIONS_EXACT:
        return "review"
    if view in MODIFY_ACTIONS_EXACT:
        return "modify"
    return "unknown"


def _route_review_event(db: Session, settings: Settings, doc: Document,
                        event: FileAuditEvent) -> None:
    """Apply the event's explicit review semantics once body fetching is ready.

    A storage key arriving by itself is never a review reason. This keeps old
    no-body debt quiet while ensuring a real upload/overwrite is immediate and
    an online edit still uses the merge window.
    """
    from .service import is_review_excluded_workspace, is_robot_uploader

    kind = _action_kind(event)
    if kind not in ("review", "modify") or not _body_fetch_ready(doc):
        return
    eligible = (not doc.is_folder
                and not is_robot_uploader(settings, doc.uploader_key, doc.uploader_name)
                # 个人知识库（I-）不进自动评审（2026-08-18 拍板）
                and not is_review_excluded_workspace(db, settings, doc.workspace_id)
                and doc.file_class in review_classes(settings.review_classes)
                and _should_auto_review(db, settings, doc, event))
    if not eligible:
        return
    if kind == "review":
        # 上传/新建/覆盖立即评审。pending 任务执行时会读取最新正文；running
        # 任务可能已读完正文，因此留下立即到期标记，由收割器补一次。
        active = db.scalar(select(ReviewJob).where(
            ReviewJob.node_id == doc.node_id,
            ReviewJob.status.in_(("pending", "running"))).limit(1))
        if active is None or active.status == "pending":
            if active is None:
                db.add(ReviewJob(job_id=str(uuid_module.uuid4()), node_id=doc.node_id,
                                 trigger="audit", requested_by="system"))
            doc.review_due_at = None
            doc.dirty_since = None
        else:
            stamp = utcnow()
            if doc.review_due_at is None:
                doc.dirty_since = stamp
            doc.review_due_at = stamp
        return

    # 正文修改进合并窗：30 分钟无后续修改评一次，持续编辑 6 小时封顶。
    stamp = utcnow()
    if doc.review_due_at is None:
        doc.dirty_since = stamp
    doc.review_due_at = stamp + timedelta(seconds=MODIFY_MERGE_WINDOW_SECONDS)


def _upsert_audit_document(db: Session, settings: Settings, event: FileAuditEvent,
                           node: dict, path: str) -> Document:
    """审计事件确认后的直接建档/更新：一个事件只动一个节点，绝不整库遍历
    （2026-08-14 流程定稿，取代早期"门铃+整库确认"）。batchQuery 不返回
    父节点——先记存储搜索给的 path、置 directory_pending，父节点关系由
    每月 10/24 全量核对补准。"""
    workspace_id = node.get("workspace_id") or ""
    if workspace_id and db.get(Workspace, workspace_id) is None:
        # 未补种的新库连注册行都没有：建占位（名称由月度核对刷新）。标记
        # watch_seeded=True 以免把 worker 拉回连续轮巡——其存量本就交给
        # 月度核对。
        db.add(Workspace(workspace_id=workspace_id, name=workspace_id, watch_seeded=True))
        db.flush()
    doc = db.get(Document, node["node_id"])
    is_new = doc is None
    if doc is None:
        doc = Document(node_id=node["node_id"], workspace_id=workspace_id, name=node.get("name") or "")
        db.add(doc)
    for field in ("name", "category", "url"):
        if node.get(field):
            setattr(doc, field, node[field])
    # batchQuery normally carries extension, but the audit trail is the
    # authoritative fallback for the confirmed event. This matters most for
    # native .adoc nodes: without the fallback they would be classified as
    # "other", assigned the wrong body-adapter semantics and silently miss
    # review. Preserve a known mirror extension before trusting the fallback.
    extension = node.get("extension") or doc.extension or event.extension
    if extension:
        doc.extension = extension
    doc.size = node.get("size") or doc.size or 0
    doc.source_created_at = node.get("created_at") or doc.source_created_at or ""
    doc.source_updated_at = node.get("updated_at") or doc.source_updated_at or ""
    if node.get("has_children") is not None:
        doc.is_folder = bool(node.get("has_children"))
    doc.file_class = classify(doc.extension, doc.is_folder)
    if path:
        doc.path = path
    if is_new and not doc.parent_node_id:
        doc.directory_pending = True
    if doc.is_deleted:
        doc.is_deleted = False
        doc.deleted_at = None
    doc.watch_misses = 0
    # 归属保护（codex feb567a P1）：上传人只在建档时落定（节点载荷的
    # creator 优先，审计操作人兜底）；已有文档的后续事件只记 last_modifier，
    # 绝不改写上传人——通知与统计都按上传人归属。
    filled_creator = False
    if is_new:
        doc.uploader_key = node.get("creator_id") or event.operator_user_id or ""
        filled_creator = True
    elif not doc.uploader_key and node.get("creator_id"):
        doc.uploader_key = node.get("creator_id") or ""
        filled_creator = True
    doc.last_modifier_key = event.operator_user_id or doc.last_modifier_key or ""
    if filled_creator:
        identity: dict = {}
        if doc.uploader_key:
            identity_input = ({"userId": doc.uploader_key} if doc.uploader_key.isdigit()
                              else {"unionId": doc.uploader_key})
            try:
                resolved = asyncio.run(BiCenterClient(settings).resolve_batch(
                    [identity_input], datetime.now(timezone.utc).strftime("%Y-%m")))
                identity = resolved[0] if resolved else {}
            except (IntegrationError, RuntimeError):
                identity = {}
        if identity.get("matched") and identity.get("includeInOfficialStats"):
            doc.uploader_key = identity.get("employeeKey", doc.uploader_key)
            doc.uploader_name = identity.get("employeeName", "")
            doc.department_name = identity.get("departmentName", "")
            doc.biz_group_name = identity.get("bizGroupName", "")
            doc.org_matched = True
        else:
            doc.uploader_name, doc.department_name, doc.biz_group_name, doc.org_matched = "未映射", "未映射", "未映射", False
    _attach_numeric_id(db, event, doc)
    db.flush()
    _route_review_event(db, settings, doc, event)
    return doc


def _enqueue_walk(db: Session, workspace_id: str, queued: set[str]) -> None:
    """queued 是本轮事务内的去重集合：autoflush 关闭时 db.get 看不到同事务
    刚 add 的行，同一库两条事件会撞主键炸掉整轮（codex 第五轮 P0）。"""
    if not workspace_id or workspace_id in queued:
        return
    queued.add(workspace_id)
    if db.get(BridgeWalk, workspace_id) is None:
        db.add(BridgeWalk(workspace_id=workspace_id))


def _finish(db: Session, event: FileAuditEvent, resolution: str) -> None:
    event.processed = True
    event.resolution = resolution
    _tally_space(db, event)


def _resolve_node_locally(db: Session, event: FileAuditEvent) -> str:
    """删除/恢复事件的本地匹配：数字 bizId 直查下载键，仅此一条路。

    生产只读实证（codex 第八轮，2026-08-17）：274 条删除/撤回/恢复事件全为
    数字 bizId，但与文档下载键匹配数为 0——删除流水的 bizId 与上传的下载键
    不在同一命名空间，即时软删基本不会命中，真实删除权威=每月 10/24 全量
    核对的 watch_misses。本函数保留为尽力而为；匹配不上保持 pending 到 48h
    死信可观测。绝不按文件名猜（同名多节点删错档案不可接受）。"稳定标识
    重构"待原始审计流水核实后另行处理（biz_id 唯一键改动风险见 memory）。

    注意：不查"同 bizId 的历史已确认事件"——file_audit_events.biz_id 是
    唯一键（入库去重），同库不可能存在第二条同 bizId 行，那是死路。"""
    if (event.biz_id or "").isdigit():
        node_id = db.scalar(select(Document.node_id)
                            .where(Document.storage_dentry_id == event.biz_id).limit(1))
        if node_id:
            return node_id
    return ""


def _handle_delete_restore(db: Session, settings: Settings, event: FileAuditEvent,
                           kind: str, summary: dict) -> None:
    """软删除/恢复（2026-08-14 拍板）：删除只翻 is_deleted 位并取消在途任务
    与未发通知，全部评审历史保留可查；恢复翻回位即可，绝不触发评审。"""
    node_id = event.matched_node_id or _resolve_node_locally(db, event)
    if not node_id:
        return  # pending：bizId 映射可能晚到，重试到 48h 死信
    doc = db.get(Document, node_id)
    if doc is None:
        return
    event.matched_node_id = node_id
    event.match_status = "confirmed"
    if kind == "delete":
        doc.is_deleted = True
        doc.deleted_at = utcnow()
        doc.review_due_at = None
        doc.dirty_since = None
        for job in db.scalars(select(ReviewJob).where(
                ReviewJob.node_id == node_id, ReviewJob.status.in_(("pending", "running")))).all():
            job.status, job.error_code, job.finished_at = "skipped", "document_deleted", utcnow()
        for note in db.scalars(select(Notification).where(
                Notification.node_id == node_id, Notification.status == "pending")).all():
            note.status, note.error_code = "skipped", "skipped_document_deleted"
        _finish(db, event, "deleted")
        summary["deleted"] = summary.get("deleted", 0) + 1
    else:
        doc.is_deleted = False
        doc.deleted_at = None
        doc.watch_misses = 0
        _finish(db, event, "restored")  # 恢复不评审（用户拍板）：正文没变，历史实例仍有效
        summary["restored"] = summary.get("restored", 0) + 1


def _try_finish_confirmed(db: Session, event: FileAuditEvent, settings: Settings, summary: dict,
                          queued: set[str], cutoff_ms: int | None = None) -> bool:
    """成功终态按动作类型收口：评审/修改类须"文档入镜像 + 正文适配器
    已具备定位条件"才 done。上传文件需要数字下载键；原生 .adoc 只需要
    node id。元数据/删除/恢复/忽略各归专属终态。"""
    if _finish_pre_cutover_event(db, event, summary, cutoff_ms):
        return True
    if event.match_status != "confirmed" or not event.matched_node_id:
        return False
    kind = _action_kind(event)
    if kind in ("delete", "restore"):
        _handle_delete_restore(db, settings, event, kind, summary)
        return bool(event.processed)
    if kind == "ignore":
        _finish(db, event, "ignored_action")
        summary["ignored"] = summary.get("ignored", 0) + 1
        return True
    if kind == "unknown":
        _finish(db, event, "ignored_unknown_action")
        summary["unknown_actions"] = summary.get("unknown_actions", 0) + 1
        return True
    _attach_numeric_id(db, event)
    doc = db.get(Document, event.matched_node_id)
    if doc is None:
        return False
    if kind == "metadata":
        _finish(db, event, "metadata_applied")
        return True
    if _body_fetch_ready(doc):
        _route_review_event(db, settings, doc, event)
        _finish(db, event, "done")
        return True
    if not (event.biz_id or "").isdigit():
        _finish(db, event, "dead_letter_no_numeric_biz_id")
        summary["dead_letter"] = summary.get("dead_letter", 0) + 1
        return True
    return False


def _near_event(iso_value: str, gmt_ms: int, tolerance_seconds: int = 900) -> bool:
    """Whether a Wiki node timestamp corroborates an audit event.

    The production Wiki ``batchQuery`` response has been observed to encode
    Beijing wall-clock time with a trailing ``Z``: for example, an audit event
    at ``13:20 UTC`` is returned by Wiki as ``21:20Z``.  Treating that marker
    as UTC rejects the same node by eight hours and prevents the review from
    ever reaching the body-fetch stage.  Keep the literal UTC interpretation
    too, but accept the Beijing-wall-clock interpretation for that malformed
    ``Z`` form only.  Explicit offsets remain authoritative.

    This correction belongs only to the event/node corroboration gate.  It
    deliberately does not rewrite the existing mirror timestamps, which would
    make the next full scan report every stored document as changed.
    """
    if not iso_value or not gmt_ms:
        return False
    raw = str(iso_value).strip()
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        candidates = [moment]
        if raw.endswith("Z"):
            # DingTalk Wiki's malformed Z timestamp is a China Standard Time
            # wall-clock value, not an actual UTC offset.
            candidates.append(moment.replace(tzinfo=timezone(timedelta(hours=8))))
        return any(abs(candidate.timestamp() * 1000 - gmt_ms) <= tolerance_seconds * 1000
                   for candidate in candidates)
    except ValueError:
        return False


def _is_creation_event(event: FileAuditEvent) -> bool:
    # 生产审计用“复制或转发文件”“文档导入”等名称表示新节点；这些事件和
    # 上传一样，绝不能拿旧节点的 updated_at 做互证。与分类同源的精确名单。
    return (event.action_view or "").strip() in CREATION_ACTIONS_EXACT


def _event_matches_node(db: Session, event: FileAuditEvent, node: dict) -> bool:
    """搜索命中唯一 ≠ 就是本事件的节点（同名新文件未进索引时，搜索只会返回
    旧节点）。只有节点创建/修改时间（或已确认的数字下载键）可以互证；仍
    证实不了一律拒绝——"系统不认识"绝不是确认依据。

    上传/新建事件只认 created_at 互证（codex 第七轮 P0）：上传产生新节点，
    其创建时间必然贴近事件；旧同名节点哪怕刚被人修改过（updated_at 落在
    窗口内）也不是这次上传的节点。修改类事件才允许 updated_at 互证。"""
    allow_updated = not _is_creation_event(event)
    # resourceExtension is advisory. Production has reported adoc for fresh
    # .xlsx/.docx uploads, so it must never reject the stronger proof: exact
    # filename + unique node + event-time corroboration. A nonzero size is
    # still an independent safety check; zero means the audit trail omitted
    # the size. Node type is persisted from the wiki node, not this hint.
    if event.size and node.get("size") and int(event.size) != int(node["size"]):
        return False
    # A creation event belongs to its creator.  The audit feed and the Wiki
    # node API both provide the same numeric userId in production.  Make that
    # a second independent proof when both sides carry it: identical titles
    # created at nearly the same time are still not enough to attach one
    # person's event to somebody else's online document.  A missing creator
    # remains non-dispositive because older Wiki nodes occasionally omit it.
    operator_id = getattr(event, "operator_user_id", "") or ""
    if (_is_creation_event(event) and operator_id and node.get("creator_id")
            and operator_id != str(node["creator_id"])):
        return False
    if _near_event(node.get("created_at") or "", event.gmt_create):
        return True
    if allow_updated and _near_event(node.get("updated_at") or "", event.gmt_create):
        return True
    doc = db.get(Document, node.get("node_id") or "")
    if doc is not None:
        if doc.storage_dentry_id and doc.storage_dentry_id == (event.biz_id or ""):
            return True
        if event.size and doc.size and int(event.size) != int(doc.size):
            return False
        if _near_event(doc.source_created_at, event.gmt_create):
            return True
        return allow_updated and _near_event(doc.source_updated_at, event.gmt_create)
    return False


def _as_utc_ms(moment) -> float:
    """DB 存的就是 UTC；naive 只是驱动丢了时区标记，绝不能按服务器本地时区
    （Asia/Shanghai）解释——否则 48h 窗口实际缩水成 40h（codex 第六轮 P1）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() * 1000


def _expired(event: FileAuditEvent, now_ms: int) -> bool:
    """重试窗口基准：retry_started_at（死信重开时设置，received_at 作为原始
    入库审计字段保持不动）→ received_at → 事件时间。"""
    basis = event.retry_started_at or event.received_at
    if basis is not None:
        return (now_ms - _as_utc_ms(basis)) > GIVE_UP_AFTER_MS
    return bool(event.gmt_create) and (now_ms - event.gmt_create) > GIVE_UP_AFTER_MS


def _cutoff_ms(cutoff: str) -> int | None:
    try:
        edge = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if edge.tzinfo is None:
            edge = edge.replace(tzinfo=timezone.utc)
        return int(edge.timestamp() * 1000)
    except ValueError:
        return None


def _finish_pre_cutover_event(db: Session, event: FileAuditEvent, summary: dict,
                              cutoff_ms: int | None) -> bool:
    """Retain, but do not replay, events that predate a repair cutover.

    This is intentionally stricter than the ordinary ``review_since`` stock
    gate: Plan A must not issue locator calls, attach download keys, enqueue
    jobs, or notify for the known pre-repair backlog.
    """
    if cutoff_ms is None or not event.gmt_create or event.gmt_create >= cutoff_ms:
        return False
    _finish(db, event, "pre_cutover_not_reviewed")
    summary["pre_cutover_not_reviewed"] = summary.get("pre_cutover_not_reviewed", 0) + 1
    return True


def _should_auto_review(db: Session, settings: Settings, doc: Document, event: FileAuditEvent) -> bool:
    """存量豁免门禁（codex 第六轮 P0）：审计拉取的游标重叠会重放截止前的旧
    事件——挂键无妨，自动评审必须满足 KG_REVIEW_SINCE（文档或事件时间在
    上线时刻之后）。历史无正文/metadata_only 记录不构成补评理由；只有该
    文档之后发生新的、白名单内的正文事件才会自然进入评审。"""
    cutoff = settings.review_since
    if not cutoff:
        return True
    from .service import _at_or_after
    if _at_or_after(doc.source_created_at or "", cutoff) or _at_or_after(doc.source_updated_at or "", cutoff):
        return True
    edge_ms = _cutoff_ms(cutoff)
    if edge_ms is not None and event.gmt_create and event.gmt_create >= edge_ms:
        return True
    return False


def _provisional_match(db: Session, event: FileAuditEvent, snapshot_id: str, summary: dict) -> None:
    """名称唯一联结只给 provisional 候选：同名新上传绝不能据此挂到旧节点
    （codex 第四轮 P0）。仅 locator 的精确 node id 才能 confirmed。"""
    if event.matched_node_id:
        return
    node_id = _unique_node_match(db, event, snapshot_id)
    if node_id:
        event.matched_node_id = node_id
        event.match_status = "provisional"
        summary["matched"] += 1


def process_audit_events(db: Session, settings: Settings) -> dict:
    """One bridge cycle.「完成才消费」生命周期：wiki 事件唯有 locator 确认
    节点、文档入镜像、正文适配器具备定位条件才算 done；到期未确认转 dead_letter_*
    可观测死信。定位按"最久未尝试"轮转取额（公平），巡走走持久化队列
    （预算外的库下轮续走），两者都有硬预算，绝不独占 worker。"""
    events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.processed.is_(False))
                        .order_by(FileAuditEvent.gmt_create).limit(BATCH)).all()
    summary = {"events": len(events), "wiki_events": 0, "matched": 0, "confirmed": 0, "walks": []}
    cutoff_ms = _cutoff_ms(settings.audit_review_since)
    snapshot_id = _latest_snapshot_id(db)
    queued: set[str] = set()  # 本轮事务内的巡走去重
    wiki_events: list[FileAuditEvent] = []
    for event in events:
        if _finish_pre_cutover_event(db, event, summary, cutoff_ms):
            continue
        if not _is_wiki_write(event):
            event.processed = True
            continue
        summary["wiki_events"] += 1
        wiki_events.append(event)
        kind = _action_kind(event)
        if kind == "ignore":
            # 协作/分享/权限等纯协同动作：终态忽略，零定位消费（codex P0-1）
            _finish(db, event, "ignored_action")
            summary["ignored"] = summary.get("ignored", 0) + 1
            continue
        if kind == "unknown":
            # 白名单外的未知动作：绝不评审（codex 第八轮 P0），带专属终态
            # 进诊断——status_brief 计数，观察到新动作类型再扩名单。
            _finish(db, event, "ignored_unknown_action")
            summary["unknown_actions"] = summary.get("unknown_actions", 0) + 1
            continue
        if kind in ("delete", "restore"):
            _handle_delete_restore(db, settings, event, kind, summary)
            continue  # 已删节点搜索拿不到，绝不进远程定位；未匹配保持 pending
        _provisional_match(db, event, snapshot_id, summary)  # 仅诊断参考，不再触发整库门铃
        _try_finish_confirmed(db, event, settings, summary, queued, cutoff_ms)
    db.commit()
    pending_wiki = [event for event in wiki_events if not event.processed]

    # confirmed-pending 全局收尾（codex 第六轮 P0）：等文档入镜像的已确认
    # 事件不能只靠 BATCH 窗口推进——纯 DB 操作按全局取额完成。取额按
    # 最久未尝试轮转（codex 第七轮 P0）：50 个"文档迟迟不来"的老事件
    # 不得堵死后来者，每次尝试盖 last_attempt_at 章自然转到队尾。
    confirmed_pending = db.scalars(
        select(FileAuditEvent)
        .where(FileAuditEvent.processed.is_(False), FileAuditEvent.match_status == "confirmed")
        .order_by(FileAuditEvent.last_attempt_at.is_(None).desc(),
                  FileAuditEvent.last_attempt_at.asc(),
                  FileAuditEvent.gmt_create.asc())
        .limit(CONFIRM_FINISH_BUDGET)).all()
    finish_stamp = utcnow()
    for event in confirmed_pending:
        event.last_attempt_at = finish_stamp
        _try_finish_confirmed(db, event, settings, summary, queued, cutoff_ms)

    # Locator: a wiki-search by file name gives the doorbell an address, and
    # its exact, corroborated node id is the ONLY authoritative match.
    # 公平轮转在数据库层全局取额（最久未尝试优先）——不受 BATCH 截断影响，
    # 第 501 条事件同样按轮次获得定位机会（codex 第五轮 P0）。
    governed = set(_governed_workspaces(db))
    located_ungoverned: set[str] = set()
    unlocated = 0
    now = utcnow()
    now_ms = int(time.time() * 1000)
    candidate_conditions = [FileAuditEvent.processed.is_(False), FileAuditEvent.match_status != "confirmed",
                            or_(FileAuditEvent.action_view.like("%知识库%"),
                                FileAuditEvent.module_view == "团队空间")]
    if cutoff_ms is not None:
        # Plan A: old backlog is retained terminally by the bounded main pass;
        # never let it consume remote locator capacity before that happens.
        candidate_conditions.append(FileAuditEvent.gmt_create >= cutoff_ms)
    # Native online documents cannot use the normal shared-storage lookup and
    # historically accumulated behind high-volume attachment uploads. Give
    # review/modify adoc events the first slots in the bounded locator; within
    # each tier the established fair retry ordering remains unchanged.
    native_review_priority = case(
        (and_(FileAuditEvent.extension == "adoc",
              FileAuditEvent.action_view.in_(tuple(REVIEW_ACTIONS_EXACT | MODIFY_ACTIONS_EXACT))), 0),
        else_=1,
    )
    candidates = db.scalars(
        select(FileAuditEvent).where(*candidate_conditions)
        # Fresh, unattempted events are latency-sensitive; retry candidates
        # then rotate fairly by their oldest last attempt.
        .order_by(native_review_priority.asc(),
                  FileAuditEvent.last_attempt_at.is_(None).desc(),
                  FileAuditEvent.gmt_create.desc(), FileAuditEvent.last_attempt_at.asc())
        .limit(WIKI_LOCATE_BUDGET)).all()
    if settings.bridge_locator_enabled and candidates:
        client = DingtalkClient(settings)
        operator = settings.dingtalk_sync_operator_id
        # Uploaded files belong to the configured shared storage space. Native
        # DingTalk online documents (adoc) do not: their nodes are absent from
        # that scoped storage index.  Search them globally, then retain the
        # existing exact-name + event-time (+ creator for creation) proof
        # before touching the mirror or review queue.  This is metadata-only;
        # the document body is still fetched only by the review worker.
        def _can_locate(event: FileAuditEvent) -> bool:
            if _action_kind(event) not in ("review", "modify", "metadata"):
                return False
            return (event.extension or "").lower() == "adoc" or bool(settings.wiki_storage_space_id)

        locatable = [event for event in candidates if _can_locate(event)]
        for event in candidates:
            event.last_attempt_at = now

        async def _locate_one(event: FileAuditEvent):
            # Storage search returns dentryUuids (== wiki nodeIds) plus the
            # directory path; the wiki batch query then names the workspace
            # each hit lives in. 两者都要带回：path 是目录归属的线索。
            names = _name_candidates(event)
            native_doc = (event.extension or "").lower() == "adoc"
            space_ids = None if native_doc else [settings.wiki_storage_space_id]
            dentries = await client.search_dentries(names[0], operator, space_ids)
            exact_ids = [d["dentry_uuid"] for d in dentries if d.get("name") in names and d.get("dentry_uuid")]
            nodes = await client.batch_query_wiki_nodes(exact_ids, operator) if exact_ids else []
            return event, names, dentries, nodes

        async def _locate_all():
            semaphore = asyncio.Semaphore(WIKI_LOCATE_CONCURRENCY)

            async def _bounded(event: FileAuditEvent):
                async with semaphore:
                    try:
                        return await _locate_one(event)
                    except (IntegrationError, RuntimeError, TimeoutError):
                        return event, None, None, None

            return await asyncio.gather(*[_bounded(event) for event in locatable])

        try:
            located = asyncio.run(asyncio.wait_for(_locate_all(), timeout=LOCATE_TIME_BUDGET_SECONDS))
        except TimeoutError:
            # All selected events keep their retry stamp and receive another
            # fair turn. A timeout cannot block the worker past its budget.
            located = []
            unlocated += len(locatable)
        summary["locator_attempted"] = summary.get("locator_attempted", 0) + len(locatable)
        for event, names, dentries, nodes in located:
            if names is None:
                unlocated += 1  # network failure: keep pending for retry
                continue
            hits = [node for node in nodes if node.get("name") in names]
            workspaces = {node.get("workspace_id") for node in hits if node.get("workspace_id")}
            if not workspaces:
                unlocated += 1  # 尚未进搜索索引：保持 pending，下一轮重试
                continue
            node_ids = {node["node_id"] for node in hits if node.get("node_id")}
            if len(node_ids) == 1:
                confirmed_id = next(iter(node_ids))
                hit = next(node for node in hits if node.get("node_id") == confirmed_id)
                # 搜索唯一还不够：须与节点载荷/镜像互证（同名新文件未入索引
                # 时，唯一命中的很可能是旧节点）。互证失败保持 pending。
                if _event_matches_node(db, event, hit):
                    if _extension_mismatch(event, hit):
                        summary["extension_mismatch_confirmed"] = summary.get("extension_mismatch_confirmed", 0) + 1
                    if event.matched_node_id != confirmed_id:
                        summary["matched"] += 1
                    event.matched_node_id = confirmed_id
                    event.match_status = "confirmed"
                    summary["confirmed"] += 1
                    # 直接建档/更新——不再把整个知识库放进巡走队列
                    # （2026-08-14 流程定稿：一个事件只动一个节点）。
                    hit_path = next((d.get("path") or "" for d in dentries
                                     if d.get("dentry_uuid") == confirmed_id), "")
                    doc = _upsert_audit_document(db, settings, event, hit, hit_path)
                    summary["direct_upserts"] = summary.get("direct_upserts", 0) + 1
                    # 完成语义按类型收口：上传文件须有数字下载键，原生
                    # .adoc 由 node id 导出正文；元数据类只更新镜像。
                    kind = _action_kind(event)
                    if kind == "metadata":
                        _finish(db, event, "metadata_applied")
                    elif _body_fetch_ready(doc):
                        _finish(db, event, "done")
                    elif not (event.biz_id or "").isdigit():
                        _finish(db, event, "dead_letter_no_numeric_biz_id")
                        summary["dead_letter"] = summary.get("dead_letter", 0) + 1
                else:
                    summary["uncorroborated"] = summary.get("uncorroborated", 0) + 1
            if not event.processed:
                _try_finish_confirmed(db, event, settings, summary, queued, cutoff_ms)
        # Candidates whose body type lacks a locator (for example an uploaded
        # file while the shared storage space is not configured) remain
        # observable/pending exactly as before.
        unlocated += len(candidates) - len(locatable)
    elif candidates:
        unlocated = len(candidates)
    if unlocated:
        if len(governed) <= settings.bridge_sweep_max_governed:
            for workspace_id in governed:
                _enqueue_walk(db, workspace_id, queued)  # 试点规模的兜底扫，代价可控
        else:
            # org 级规模：未定位事件不触发全库兜底；发现与评审由 watcher
            # 轮巡 + KG_REVIEW_SINCE 兜底。
            summary["sweep_skipped_governed"] = len(governed)
    summary["unlocated"] = unlocated
    summary["located_ungoverned"] = sorted(located_ungoverned)[:5]

    # 死信裁决：到期仍未完成的事件带原因归档，绝不伪装成功。窗口基准是
    # retry_started_at（重开）→ received_at；池子含批外的 confirmed-pending。
    expiry_pool = {event.id: event for event in pending_wiki}
    for event in confirmed_pending:
        expiry_pool.setdefault(event.id, event)
    for event in expiry_pool.values():
        if event.processed:
            continue
        if _expired(event, now_ms):
            reason = "dead_letter_no_doc" if event.match_status == "confirmed" else "dead_letter_unmatched"
            _finish(db, event, reason)
            summary["dead_letter"] = summary.get("dead_letter", 0) + 1
    summary["pending_retry"] = sum(1 for event in expiry_pool.values() if not event.processed)
    db.commit()
    _drain_walk_queue(db, settings, summary)
    db.commit()
    return summary


def _drain_walk_queue(db: Session, settings: Settings, summary: dict) -> None:
    """持久化巡走队列：成功才出队；失败行记 last_attempt_at/failures 后
    自然轮转到队尾——五个持续失败的库不能永久占据预算饿死其余
    （codex 第六轮 P0）。"""
    debounce = max(60, settings.bridge_debounce_seconds)
    rows = db.scalars(select(BridgeWalk)
                      .order_by(BridgeWalk.last_attempt_at.is_(None).desc(),
                                BridgeWalk.last_attempt_at.asc(),
                                BridgeWalk.requested_at.asc())).all()
    for row in rows:
        if len(summary["walks"]) >= WALK_BUDGET:
            summary["walks_deferred"] = summary.get("walks_deferred", 0) + 1
            continue
        if time.time() - _last_walk.get(row.workspace_id, 0) < debounce:
            continue  # 去抖窗口内：行留队，窗口过后自然续走
        _last_walk[row.workspace_id] = time.time()
        row.last_attempt_at = utcnow()
        run = asyncio.run(watch_workspace(db, settings, row.workspace_id, mode="bridge"))
        if run.status == "succeeded":
            db.delete(row)
        else:
            row.failures += 1
            _last_walk.pop(row.workspace_id, None)  # 内存去抖立即让路，轮转由 last_attempt_at 保证
        summary["walks"].append({"workspace_id": row.workspace_id, "run_id": run.run_id, "mode": run.mode,
                                 "status": run.status, "new": run.documents_new,
                                 "changed": run.documents_changed, "error_code": run.error_code})


def bridge_status(db: Session) -> dict:
    spaces = db.scalars(select(SpaceMap).order_by(SpaceMap.event_count.desc()).limit(10)).all()
    pending = db.scalar(select(FileAuditEvent.id).where(FileAuditEvent.processed.is_(False)).limit(1))
    matched = db.scalar(select(FileAuditEvent.id).where(FileAuditEvent.matched_node_id != "").limit(1))
    return {
        "governed_workspaces": _governed_workspaces(db),
        "space_tallies": [{"space_id": entry.space_id, "events": entry.event_count,
                           "workspace_id": entry.workspace_id, "source": entry.source} for entry in spaces],
        "has_unprocessed_events": bool(pending),
        "has_matched_events": bool(matched),
    }
