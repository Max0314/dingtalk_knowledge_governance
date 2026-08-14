"""Audit-event -> knowledge-base-node bridge.

The audit trail reliably says *that* a knowledge-base write happened, but —
verified live 2026-08-11 — wiki uploads all carry one shared org-wide
storage-space id, so the event does NOT say in *which* library. The bridge
therefore treats wiki write events as an unaddressed doorbell:

  1. consume unprocessed wiki-write events (action mentions 知识库 or
     module 团队空间);
  2. ring the governed set: every workspace the mirror governs gets a
     debounced targeted walk (the proven watcher code, mode="bridge") —
     node_id-exact diffs enqueue the reviews. In the pilot that is one
     workspace; org rollout will add a wiki-search resolver to route events
     by file name before falling back to the sweep;
  3. backfill matched_node_id where the event's resource name joins a
     mirrored or snapshot node uniquely — retried after the walks so files
     the walk just mirrored match too.

space_map remains as an observability tally (which space ids appear, how
often) and for future per-space modules; wiki routing no longer trusts it.

Cost model: no wiki writes -> no walks. Each ring costs one walk per
governed workspace, amortized by the debounce window.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid as uuid_module

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from .config import Settings
from .db import (BridgeWalk, Document, FileAuditEvent, HistoricalFileNode, HistoricalSnapshot, ReviewInstance,
                 ReviewJob, SpaceMap, utcnow)
from .fileclass import review_classes
from .integrations import DingtalkClient, IntegrationError
from .service import watch_workspace

logger = logging.getLogger("kg.bridge")

BATCH = 500
# 每轮远程定位的事件上限与时间预算：单事件是"搜索+批量节点查询"两个 20s
# 超时的串行外呼，必须双重限额，绝不独占 worker。
WIKI_LOCATE_BUDGET = 5
LOCATE_TIME_BUDGET_SECONDS = 30
# 每轮桥接巡走的库数上限；没走到的库留在持久化队列里下一轮续走。
WALK_BUDGET = 5
# confirmed 待完成事件的全局收尾额度（纯 DB 操作，不外呼，可以宽松）。
CONFIRM_FINISH_BUDGET = 50
# 未能确认匹配的事件转入死信的时限：终态带 dead_letter_* 原因可观测，
# 不伪装成功（发现与评审由 watcher 轮巡 + KG_REVIEW_SINCE 兜底）。
GIVE_UP_AFTER_MS = 48 * 3600 * 1000

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


def _attach_numeric_id(db: Session, event: FileAuditEvent, settings: Settings) -> None:
    """The event's bizId IS the file's numeric storage dentry id (verified by
    cross-download); hand it to the mirrored document so reviews can fetch
    the body through the numeric-only download API. A document whose review
    ran before its key arrived gets an automatic content-scope re-review."""
    if not event.matched_node_id or not (event.biz_id or "").isdigit():
        return
    doc = db.get(Document, event.matched_node_id)
    if not doc or doc.storage_dentry_id:
        return
    doc.storage_dentry_id = event.biz_id
    if doc.is_folder or doc.file_class not in review_classes(settings.review_classes):
        return
    if not _should_auto_review(db, settings, doc, event):
        return  # 截止前存量：键已挂上，但不违背「存量忽略」补评审
    pending = db.scalar(select(ReviewJob).where(ReviewJob.node_id == doc.node_id,
                                                ReviewJob.status.in_(("pending", "running"))))
    if not pending:
        db.add(ReviewJob(job_id=str(uuid_module.uuid4()), node_id=doc.node_id,
                         trigger="content_key", requested_by="system"))


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


def _try_finish_confirmed(db: Session, event: FileAuditEvent, settings: Settings, summary: dict,
                          queued: set[str]) -> bool:
    """成功终态的完整定义（codex 第四轮 P0）：locator 确认的节点 + 文档已入
    镜像 + 正文下载键在文档上。键挂不上（bizId 非数字）转死信而非伪装成功；
    文档未入镜像保持 pending 重试。"""
    if event.match_status != "confirmed" or not event.matched_node_id:
        return False
    _attach_numeric_id(db, event, settings)
    doc = db.get(Document, event.matched_node_id)
    if doc is None:
        return False
    _enqueue_walk(db, doc.workspace_id, queued)  # 事件说这个库有写操作：欠一次快巡走
    if doc.storage_dentry_id:
        _finish(db, event, "done")
        return True
    if not (event.biz_id or "").isdigit():
        _finish(db, event, "dead_letter_no_numeric_biz_id")
        summary["dead_letter"] = summary.get("dead_letter", 0) + 1
        return True
    return False


def _near_event(iso_value: str, gmt_ms: int, tolerance_seconds: int = 900) -> bool:
    if not iso_value or not gmt_ms:
        return False
    try:
        moment = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return abs(moment.timestamp() * 1000 - gmt_ms) <= tolerance_seconds * 1000
    except ValueError:
        return False


def _is_creation_event(event: FileAuditEvent) -> bool:
    view = event.action_view or ""
    # 生产审计还会用“复制或转发文件”“文档导入”等名称表示新节点；
    # 这些事件和上传一样，绝不能拿旧节点的 updated_at 做互证。
    return any(keyword in view for keyword in ("上传", "新建", "创建", "复制", "转发", "导入", "副本"))


def _event_matches_node(db: Session, event: FileAuditEvent, node: dict) -> bool:
    """搜索命中唯一 ≠ 就是本事件的节点（同名新文件未进索引时，搜索只会返回
    旧节点）。互证优先用 locator 节点载荷自带的时间/扩展名，载荷没有才退回
    镜像文档；仍证实不了一律拒绝——"系统不认识"绝不是确认依据。

    上传/新建事件只认 created_at 互证（codex 第七轮 P0）：上传产生新节点，
    其创建时间必然贴近事件；旧同名节点哪怕刚被人修改过（updated_at 落在
    窗口内）也不是这次上传的节点。修改类事件才允许 updated_at 互证。"""
    allow_updated = not _is_creation_event(event)
    if event.extension and node.get("extension") and event.extension.lower() != str(node["extension"]).lower():
        return False
    if event.size and node.get("size") and int(event.size) != int(node["size"]):
        return False
    if _near_event(node.get("created_at") or "", event.gmt_create):
        return True
    if allow_updated and _near_event(node.get("updated_at") or "", event.gmt_create):
        return True
    doc = db.get(Document, node.get("node_id") or "")
    if doc is not None:
        if doc.storage_dentry_id and doc.storage_dentry_id == (event.biz_id or ""):
            return True
        if event.extension and doc.extension and event.extension.lower() != doc.extension.lower():
            return False
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


def _should_auto_review(db: Session, settings: Settings, doc: Document, event: FileAuditEvent) -> bool:
    """存量豁免门禁（codex 第六轮 P0）：审计拉取的游标重叠会重放截止前的旧
    事件——挂键无妨，自动评审必须满足 KG_REVIEW_SINCE（文档或事件时间在
    上线时刻之后），或该文档已有 metadata_only 评审等待正文补评。"""
    cutoff = settings.review_since
    if not cutoff:
        return True
    from .service import _at_or_after
    if _at_or_after(doc.source_created_at or "", cutoff) or _at_or_after(doc.source_updated_at or "", cutoff):
        return True
    edge_ms = _cutoff_ms(cutoff)
    if edge_ms is not None and event.gmt_create and event.gmt_create >= edge_ms:
        return True
    latest = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == doc.node_id)
                       .order_by(ReviewInstance.created_at.desc()).limit(1))
    return latest is not None and latest.review_scope == "metadata_only"


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
    节点、文档入镜像、下载键在文档上才算 done；到期未确认转 dead_letter_*
    可观测死信。定位按"最久未尝试"轮转取额（公平），巡走走持久化队列
    （预算外的库下轮续走），两者都有硬预算，绝不独占 worker。"""
    events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.processed.is_(False))
                        .order_by(FileAuditEvent.gmt_create).limit(BATCH)).all()
    summary = {"events": len(events), "wiki_events": 0, "matched": 0, "confirmed": 0, "walks": []}
    snapshot_id = _latest_snapshot_id(db)
    queued: set[str] = set()  # 本轮事务内的巡走去重
    wiki_events: list[FileAuditEvent] = []
    for event in events:
        if not _is_wiki_write(event):
            event.processed = True
            continue
        summary["wiki_events"] += 1
        wiki_events.append(event)
        _provisional_match(db, event, snapshot_id, summary)
        if event.match_status == "provisional" and event.matched_node_id:
            doc = db.get(Document, event.matched_node_id)
            if doc is not None:
                _enqueue_walk(db, doc.workspace_id, queued)  # 门铃提示：该库可能有写操作
        _try_finish_confirmed(db, event, settings, summary, queued)
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
        _try_finish_confirmed(db, event, settings, summary, queued)

    # Locator: a wiki-search by file name gives the doorbell an address, and
    # its exact, corroborated node id is the ONLY authoritative match.
    # 公平轮转在数据库层全局取额（最久未尝试优先）——不受 BATCH 截断影响，
    # 第 501 条事件同样按轮次获得定位机会（codex 第五轮 P0）。
    governed = set(_governed_workspaces(db))
    located_ungoverned: set[str] = set()
    unlocated = 0
    now = utcnow()
    now_ms = int(time.time() * 1000)
    candidates = db.scalars(
        select(FileAuditEvent)
        .where(FileAuditEvent.processed.is_(False), FileAuditEvent.match_status != "confirmed",
               or_(FileAuditEvent.action_view.like("%知识库%"), FileAuditEvent.module_view == "团队空间"))
        .order_by(FileAuditEvent.last_attempt_at.is_(None).desc(),
                  FileAuditEvent.last_attempt_at.asc(), FileAuditEvent.gmt_create.asc())
        .limit(WIKI_LOCATE_BUDGET)).all()
    if settings.bridge_locator_enabled and settings.wiki_storage_space_id and candidates:
        client = DingtalkClient(settings)
        operator = settings.dingtalk_sync_operator_id
        started = time.monotonic()

        async def _locate(names: list[str]):
            # Storage search returns dentryUuids (== wiki nodeIds); the wiki
            # batch query then names the workspace each hit lives in.
            dentries = await client.search_dentries(names[0], operator, [settings.wiki_storage_space_id])
            exact_ids = [d["dentry_uuid"] for d in dentries if d.get("name") in names and d.get("dentry_uuid")]
            return await client.batch_query_wiki_nodes(exact_ids, operator) if exact_ids else []

        for event in candidates:
            remaining = LOCATE_TIME_BUDGET_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                break
            event.last_attempt_at = now
            names = _name_candidates(event)
            try:
                # 硬时间预算：整组请求包在剩余时间内，40s 的一对外呼不能把
                # 30s 预算撑到 70s（codex 第五轮 P1）。
                nodes = asyncio.run(asyncio.wait_for(_locate(names), timeout=remaining))
            except (IntegrationError, RuntimeError, TimeoutError):
                unlocated += 1  # 网络失败/超时：事件保持 pending，下一轮重试
                continue
            hits = [node for node in nodes if node.get("name") in names]
            workspaces = {node.get("workspace_id") for node in hits if node.get("workspace_id")}
            if not workspaces:
                unlocated += 1  # 尚未进搜索索引：保持 pending，下一轮重试
                continue
            for workspace_id in workspaces:
                if workspace_id in governed or settings.bridge_scope == "mapped":
                    _enqueue_walk(db, workspace_id, queued)
                else:
                    located_ungoverned.add(workspace_id)
            node_ids = {node["node_id"] for node in hits if node.get("node_id")}
            if len(node_ids) == 1:
                confirmed_id = next(iter(node_ids))
                hit = next(node for node in hits if node.get("node_id") == confirmed_id)
                # 搜索唯一还不够：须与节点载荷/镜像互证（同名新文件未入索引
                # 时，唯一命中的很可能是旧节点）。互证失败保持 pending。
                if _event_matches_node(db, event, hit):
                    if event.matched_node_id != confirmed_id:
                        summary["matched"] += 1
                    event.matched_node_id = confirmed_id
                    event.match_status = "confirmed"
                    summary["confirmed"] += 1
                else:
                    summary["uncorroborated"] = summary.get("uncorroborated", 0) + 1
            _try_finish_confirmed(db, event, settings, summary, queued)
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
