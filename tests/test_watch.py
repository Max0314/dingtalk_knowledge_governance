import os

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

import pytest
from sqlalchemy import select

from app import service
from app.config import get_settings
from app.db import Document, FileAuditEvent, ReviewInstance, ReviewJob, SessionLocal, Workspace, init_db

WS = "watch-ws"


class FakeClient:
    """Deterministic stand-in for DingtalkClient: one workspace, mutable node set."""

    nodes: dict[str, dict] = {}

    def __init__(self, settings):
        pass

    async def list_workspaces(self, operator_id, next_token="", max_results=30):
        return {"items": [{"workspace_id": WS, "name": "测试个人库", "root_node_id": "root"}], "next_token": ""}

    async def workspace_detail(self, workspace_id, operator_id):
        return {"workspace_id": workspace_id, "name": "测试个人库", "root_node_id": "root",
                "description": "", "url": "", "creator_id": "tester", "created_at": "", "updated_at": ""}

    async def list_nodes(self, workspace_id, operator_id, parent_node_id="", next_token="", max_results=100):
        items = []
        for node in type(self).nodes.values():
            if node["parent"] != (parent_node_id or "root"):
                continue
            items.append({"node_id": node["node_id"], "name": node["name"], "category": "file",
                          "extension": node.get("extension", "docx"), "url": "", "size": 10, "word_count": 0,
                          "has_children": node.get("has_children", False),
                          "creator_id": node.get("creator", "tester"),  # None = 无创建人节点
                          "created_at": node.get("created_at", "2026-08-07T09:00:00"),
                          "updated_at": node.get("updated_at", "2026-08-07T09:00:00")})
        # The real listing re-emits nodes (observed ~2x on a personal space);
        # duplicating here locks in the walker's same-cycle dedup.
        return {"items": items * 2, "next_token": "", "parent_node_id": parent_node_id or "root"}


def node(node_id, parent="root", **extra):
    return {"node_id": node_id, "parent": parent, "name": extra.pop("name", f"文件{node_id}.docx"), **extra}


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setattr(service, "DingtalkClient", FakeClient)
    service._watch_cache.update(at=0.0, key="", resolved=[], unresolved=[])
    init_db()
    with SessionLocal() as db:
        node_ids = [row[0] for row in db.execute(select(Document.node_id).where(Document.workspace_id == WS))]
        if node_ids:
            db.query(ReviewJob).filter(ReviewJob.node_id.in_(node_ids)).delete(synchronize_session=False)
            db.query(ReviewInstance).filter(ReviewInstance.node_id.in_(node_ids)).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id.in_(node_ids)).delete(synchronize_session=False)
        ws = db.get(Workspace, WS)
        if ws:
            ws.watch_seeded = False  # 补种标记持久于共享测试库，必须随夹具复位
        db.commit()
    return get_settings().model_copy(update={"watch_workspaces": "个人库", "watch_delete_misses": 2,
                                             "dingtalk_sync_operator_id": "op-test"})


def cycle(settings):
    with SessionLocal() as db:
        return service.run_watch_cycle(db, settings)


def jobs_for(db, node_id):
    return db.scalars(select(ReviewJob).where(ReviewJob.node_id == node_id)).all()


def test_watch_full_lifecycle(settings):
    FakeClient.nodes = {"A": node("watch-A"), "D": node("watch-D", name="目录D", has_children=True),
                        "B": node("watch-B", parent="watch-D")}

    seeded = cycle(settings)
    assert seeded["resolved"] and seeded["resolved"][0]["workspace_id"] == WS
    assert seeded["runs"][0]["mode"] == "watch_seed"
    assert seeded["runs"][0]["documents_seen"] == 3 and seeded["runs"][0]["documents_new"] == 3
    with SessionLocal() as db:
        assert db.get(Document, "watch-A").source_updated_at == "2026-08-07T09:00:00"
        for node_id in ("watch-A", "watch-B", "watch-D"):
            assert jobs_for(db, node_id) == []  # seeding must not flood the queue

    # Uploaded attachments still wait for the audit numeric storage key; the
    # watcher cannot fetch their body from nodeId alone.
    FakeClient.nodes["C"] = node("watch-C")
    added = cycle(settings)
    assert added["runs"][0]["mode"] == "watch" and added["runs"][0]["documents_new"] == 1
    with SessionLocal() as db:
        assert jobs_for(db, "watch-C") == []
        assert db.get(Document, "watch-C").parent_node_id == "root"
        assert db.get(Document, "watch-B").parent_node_id == "watch-D"  # 遍历补准目录

    # Source update remains mirror-only here; edits use the audit merge window.
    FakeClient.nodes["B"]["updated_at"] = "2026-08-07T10:30:00"
    changed = cycle(settings)
    assert changed["runs"][0]["documents_changed"] == 1
    with SessionLocal() as db:
        assert jobs_for(db, "watch-B") == []
        assert db.get(Document, "watch-B").source_updated_at == "2026-08-07T10:30:00"

    # Disappearance: soft delete only after two consecutive complete misses.
    removed = FakeClient.nodes.pop("A")
    cycle(settings)
    with SessionLocal() as db:
        doc = db.get(Document, "watch-A")
        assert doc.watch_misses == 1 and doc.is_deleted is False
    cycle(settings)
    with SessionLocal() as db:
        assert db.get(Document, "watch-A").is_deleted is True

    # Recycle-bin restore: seen again -> undeleted, no duplicate review job.
    FakeClient.nodes["A"] = removed
    cycle(settings)
    with SessionLocal() as db:
        doc = db.get(Document, "watch-A")
        assert doc.is_deleted is False and doc.watch_misses == 0
        assert jobs_for(db, "watch-A") == []


def test_watch_unresolved_target_reported(settings):
    result = cycle(settings.model_copy(update={"watch_workspaces": "不存在的库"}))
    assert result["resolved"] == [] and result["unresolved"] == ["不存在的库"] and result["runs"] == []


def test_seeded_watch_enqueues_new_native_adoc_by_node_id(settings):
    """A post-seed native document is body-ready by authoritative nodeId and
    must not depend on the audit trail's stale creation title."""
    live = settings.model_copy(update={"audit_review_since": "2026-08-20T00:00:00+08:00"})
    FakeClient.nodes = {"A": node("watch-A")}
    cycle(live)  # stock seed: no review
    FakeClient.nodes["N"] = node("watch-native-new", name="重命名后的在线文档.adoc",
                                 extension="adoc", created_at="2026-08-21T09:46Z",
                                 updated_at="2026-08-21T09:46Z")
    added = cycle(live)
    assert added["runs"][0]["documents_new"] == 1
    with SessionLocal() as db:
        doc = db.get(Document, "watch-native-new")
        assert doc.file_class == "native_doc" and doc.storage_dentry_id == ""
        assert [job.trigger for job in jobs_for(db, "watch-native-new")] == ["watch"]


def test_seeded_watch_keeps_pre_cutover_native_node_mirror_only(settings):
    """Plan A remains intact if an old node becomes visible after seed."""
    live = settings.model_copy(update={"audit_review_since": "2026-08-20T00:00:00+08:00"})
    FakeClient.nodes = {"A": node("watch-A")}
    cycle(live)
    FakeClient.nodes["OLD"] = node("watch-native-old", name="历史在线文档.adoc",
                                   extension="adoc", created_at="2026-08-01T09:00Z",
                                   updated_at="2026-08-01T09:00Z")
    cycle(live)
    with SessionLocal() as db:
        assert db.get(Document, "watch-native-old") is not None
        assert jobs_for(db, "watch-native-old") == []


def test_walk_falls_back_to_listing_when_detail_fails(settings, monkeypatch):
    import asyncio
    from app.integrations import IntegrationError

    class DetailFailClient(FakeClient):
        async def workspace_detail(self, workspace_id, operator_id):
            raise IntegrationError("dingtalk_request_failed", "detail 400s on this space", 400)

    monkeypatch.setattr(service, "DingtalkClient", DetailFailClient)
    FakeClient.nodes = {"A": node("watch-A")}
    with SessionLocal() as db:
        run = asyncio.run(service.watch_workspace(db, settings, WS, mode="bridge"))  # no space passed
        assert run.status == "succeeded" and run.documents_seen == 1


def test_robot_uploads_never_enter_review_queue(settings):
    robot_settings = settings.model_copy(update={"robot_user_ids": "tester"})  # FakeClient creator is "tester"
    FakeClient.nodes = {"R": node("watch-robot-doc")}
    cycle(robot_settings)  # seed
    FakeClient.nodes["R2"] = node("watch-robot-doc2")
    cycle(robot_settings)
    with SessionLocal() as db:
        assert jobs_for(db, "watch-robot-doc2") == []  # robot upload: mirrored but never reviewed


def test_interrupted_seed_stays_seed_and_absorbs_stock(settings):
    """半途而废的补种：镜像非空但 watch_seeded 未置位 → 下一轮仍是 seed，
    存量不得灌入评审；完整走完才置位（codex 2026-08-13 阻断项）。"""
    FakeClient.nodes = {"A": node("watch-A"), "B": node("watch-B")}
    with SessionLocal() as db:
        db.merge(Document(node_id="watch-A", workspace_id=WS, name="文件watch-A.docx", extension="docx"))
        db.commit()  # 模拟上一轮补种被重启打断：镜像已有 A，标记未置位
    result = cycle(settings)
    assert result["runs"][0]["mode"] == "watch_seed"
    with SessionLocal() as db:
        assert jobs_for(db, "watch-A") == [] and jobs_for(db, "watch-B") == []
        assert db.get(Workspace, WS).watch_seeded
    FakeClient.nodes["C"] = node("watch-C")
    second = cycle(settings)
    assert second["runs"][0]["mode"] == "watch"
    with SessionLocal() as db:
        assert jobs_for(db, "watch-C") == []  # attachment still needs audit storage key


def test_seed_walk_never_enqueues_reviews_even_past_cutoff(settings):
    """The first complete walk always absorbs stock, including recent rows."""
    live = settings.model_copy(update={"review_since": "2026-08-10"})
    FakeClient.nodes = {"S": node("watch-stock", created_at="2026-08-07T09:00:00"),
                        "N": node("watch-fresh", created_at="2026-08-12T09:00:00")}
    result = cycle(live)
    assert result["runs"][0]["mode"] == "watch_seed"
    with SessionLocal() as db:
        assert jobs_for(db, "watch-stock") == []
        assert jobs_for(db, "watch-fresh") == []


def test_seed_walk_stays_mirror_only_across_cutoff_boundaries(settings):
    """Cutoff never turns a seed run into a stock backfill."""
    live = settings.model_copy(update={"review_since": "2026-08-12T08:00:00Z"})
    FakeClient.nodes = {
        "E": node("watch-early", created_at="2026-08-12T07:59:00"),
        "A": node("watch-after", created_at="2026-08-12T08:30:00"),
        "M": node("watch-modified", created_at="2026-08-07T09:00:00",
                  updated_at="2026-08-13T09:00:00"),
    }
    result = cycle(live)
    assert result["runs"][0]["mode"] == "watch_seed"
    with SessionLocal() as db:
        for node_id in ("watch-early", "watch-after", "watch-modified"):
            assert jobs_for(db, node_id) == []


def test_walk_survives_nodes_without_creator(settings):
    """2026-08-14 生产实测：个别节点没有创建人 id，None.isdigit() 曾令
    三个库整轮回滚、镜像永远为 0。"""
    FakeClient.nodes = {"N": node("watch-nocreator", creator=None)}
    result = cycle(settings)
    assert result["runs"][0]["status"] == "succeeded" and result["runs"][0]["documents_seen"] == 1
    with SessionLocal() as db:
        doc = db.get(Document, "watch-nocreator")
        assert doc is not None and doc.uploader_key == "" and doc.uploader_name == "未映射"


def test_scan_calendar_and_decision(settings):
    """全量扫描改为每月计划日（默认 10/24）：补种未清恒 scan；结账后 idle；
    新计划日到达自动回到 scan。日期计算含跨月/跨年。"""
    from datetime import date

    from app.db import Workspace

    assert service.current_scan_due(settings, date(2026, 8, 14)) == "2026-08-10"
    assert service.current_scan_due(settings, date(2026, 8, 24)) == "2026-08-24"
    assert service.current_scan_due(settings, date(2026, 8, 5)) == "2026-07-24"
    assert service.current_scan_due(settings, date(2026, 1, 3)) == "2025-12-24"

    with SessionLocal() as db:
        pending = db.scalars(select(Workspace.workspace_id).where(Workspace.watch_seeded.is_(False))).all()
        db.query(Workspace).filter(Workspace.workspace_id.in_(pending)).update(
            {"watch_seeded": True}, synchronize_session=False) if pending else None
        db.commit()
        plan = service._watch_plan(db)
        old_completed = plan.completed_for
        plan.completed_for = ""
        db.commit()
        assert service.watch_scan_decision(db, settings) == "scan"   # 本期计划未完成
        service.mark_scan_cycle_complete(db, settings)
        assert service.watch_scan_decision(db, settings) == "idle"   # 结账后空闲
        ws0 = db.scalars(select(Workspace)).first()
        ws0.watch_seeded = False
        db.commit()
        assert service.watch_scan_decision(db, settings) == "scan"   # 补种未清恒 scan
        ws0.watch_seeded = True
        if pending:
            db.query(Workspace).filter(Workspace.workspace_id.in_(pending)).update(
                {"watch_seeded": False}, synchronize_session=False)
        plan.completed_for = old_completed
        db.commit()


def test_inactive_workspace_excluded_from_seeding(settings):
    """已删除/失权的库不计入补种缺口——不能把 worker 永久拖回连续轮巡
    （P-06 德国DG路由器教训，2026-08-14 拍板自动排除）。"""
    with SessionLocal() as db:
        before = service._seeding_pending(db)
        db.merge(Workspace(workspace_id="inactive-x", name="已删除的库",
                           watch_seeded=False, is_active=False))
        db.commit()
        assert service._seeding_pending(db) == before
        db.query(Workspace).filter(Workspace.workspace_id == "inactive-x").delete(synchronize_session=False)
        db.commit()


def test_cycle_completion_two_strikes_before_inactive(settings):
    """整轮缺席计一次缺席，连续两轮才标记不可见（codex 第八轮 P1：单次
    列表不完整不能误停正常库）；重新出现清零计数；空集不判决。"""
    with SessionLocal() as db:
        db.merge(Workspace(workspace_id="gone-404", name="已被删除的库", watch_seeded=True,
                           is_active=True, unreachable_misses=0))
        db.commit()
        seen = {row[0] for row in db.execute(select(Workspace.workspace_id)
                                             .where(Workspace.is_active.is_(True)))} - {"gone-404"}
        service.mark_scan_cycle_complete(db, settings, seen)
        ws = db.get(Workspace, "gone-404")
        assert ws.is_active is True and ws.unreachable_misses == 1   # 第一击：仅计数
        service.mark_scan_cycle_complete(db, settings, seen)
        assert db.get(Workspace, "gone-404").is_active is False      # 第二击：排除
        assert db.get(Workspace, WS).is_active is True               # 本轮看到的库不受影响
        ws = db.get(Workspace, "gone-404")
        ws.is_active, ws.unreachable_misses = True, 1
        db.commit()
        service.mark_scan_cycle_complete(db, settings, seen | {"gone-404"})
        assert db.get(Workspace, "gone-404").unreachable_misses == 0  # 重新出现清零
        service.mark_scan_cycle_complete(db, settings, set())
        assert db.get(Workspace, "gone-404").is_active is True        # 空集不判决
        db.query(Workspace).filter(Workspace.workspace_id == "gone-404").delete(synchronize_session=False)
        db.commit()


def test_workspace_not_visible_two_strikes_then_revives(settings, monkeypatch):
    """404/失权的库：连续两次探测失败才自动排除（单次列表抖动不误停）；
    恢复可见后一次成功巡走自动复活并清零计数。"""
    import asyncio

    from app.integrations import IntegrationError

    class VanishedClient(FakeClient):
        async def workspace_detail(self, workspace_id, operator_id):
            raise IntegrationError("dingtalk_request_failed", "404 on detail", 404)

        async def list_workspaces(self, operator_id, next_token="", max_results=30):
            return {"items": [], "next_token": ""}  # 操作者列表里也没有

    monkeypatch.setattr(service, "DingtalkClient", VanishedClient)
    with SessionLocal() as db:
        db.merge(Workspace(workspace_id="vanished-1", name="德国DG路由器", watch_seeded=True,
                           is_active=True, unreachable_misses=0))
        db.commit()
        run = asyncio.run(service.watch_workspace(db, settings, "vanished-1"))
        assert run.status == "failed" and run.error_code.startswith("workspace_not_visible")
        ws = db.get(Workspace, "vanished-1")
        assert ws.is_active is True and ws.unreachable_misses == 1   # 第一击：仅计数
        run_again = asyncio.run(service.watch_workspace(db, settings, "vanished-1"))
        assert run_again.status == "failed"
        assert db.get(Workspace, "vanished-1").is_active is False    # 第二击：排除
    monkeypatch.setattr(service, "DingtalkClient", FakeClient)
    FakeClient.nodes = {}
    with SessionLocal() as db:
        run2 = asyncio.run(service.watch_workspace(db, settings, "vanished-1"))
        assert run2.status == "succeeded"
        ws = db.get(Workspace, "vanished-1")
        assert ws.is_active is True and ws.unreachable_misses == 0   # 重新可见自动复活
        db.query(Workspace).filter(Workspace.workspace_id == "vanished-1").delete(synchronize_session=False)
        db.commit()


def test_watch_skips_unreviewable_file_classes(settings):
    FakeClient.nodes = {"D1": node("watch-doc1")}
    cycle(settings)  # seed with one document

    FakeClient.nodes["P"] = node("watch-pic", name="截图.png", extension="png")
    FakeClient.nodes["L"] = node("watch-log", name="dump.log", extension="log")
    FakeClient.nodes["D2"] = node("watch-doc2")
    cycle(settings)
    with SessionLocal() as db:
        assert db.get(Document, "watch-pic").file_class == "image"
        assert db.get(Document, "watch-log").file_class == "engineering"
        # Attachments and unreviewable classes do not use native nodeId export.
        assert jobs_for(db, "watch-pic") == [] and jobs_for(db, "watch-doc2") == []
