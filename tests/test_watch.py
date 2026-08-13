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
                          "has_children": node.get("has_children", False), "creator_id": "tester",
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

    # New file after the seed -> review job with trigger "watch".
    FakeClient.nodes["C"] = node("watch-C")
    added = cycle(settings)
    assert added["runs"][0]["mode"] == "watch" and added["runs"][0]["documents_new"] == 1
    with SessionLocal() as db:
        triggers = [job.trigger for job in jobs_for(db, "watch-C")]
        assert triggers == ["watch"]

    # Source update -> review job with trigger "watch_change".
    FakeClient.nodes["B"]["updated_at"] = "2026-08-07T10:30:00"
    changed = cycle(settings)
    assert changed["runs"][0]["documents_changed"] == 1
    with SessionLocal() as db:
        assert [job.trigger for job in jobs_for(db, "watch-B")] == ["watch_change"]
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
        assert [job.trigger for job in jobs_for(db, "watch-C")] == ["watch"]


def test_seed_reviews_files_created_after_go_live(settings):
    """KG_REVIEW_SINCE：补种期间创建时间在上线日之后的文件也要评审——
    上线后传进未补种库的文档不能被静默吸收（codex 阻断项 2）。"""
    live = settings.model_copy(update={"review_since": "2026-08-10"})
    FakeClient.nodes = {"S": node("watch-stock", created_at="2026-08-07T09:00:00"),
                        "N": node("watch-fresh", created_at="2026-08-12T09:00:00")}
    result = cycle(live)
    assert result["runs"][0]["mode"] == "watch_seed"
    with SessionLocal() as db:
        assert jobs_for(db, "watch-stock") == []
        assert [job.trigger for job in jobs_for(db, "watch-fresh")] == ["watch"]


def test_review_since_precise_moment_updated_at_and_key_recovery(settings):
    """codex 阻断项1/2：上线时刻精确到分钟——当天更早上传的仍是存量；
    存量在上线后被修改也要评审；桥接先消费的下载键在入队时找回。"""
    live = settings.model_copy(update={"review_since": "2026-08-12T08:00:00Z"})
    with SessionLocal() as db:
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id == "987654321").delete(synchronize_session=False)
        db.add(FileAuditEvent(biz_id="987654321", gmt_create=1755000000000,
                              matched_node_id="watch-after", match_status="confirmed", processed=True))
        db.commit()
    FakeClient.nodes = {
        "E": node("watch-early", created_at="2026-08-12T07:59:00"),
        "A": node("watch-after", created_at="2026-08-12T08:30:00"),
        "M": node("watch-modified", created_at="2026-08-07T09:00:00",
                  updated_at="2026-08-13T09:00:00"),
    }
    result = cycle(live)
    assert result["runs"][0]["mode"] == "watch_seed"
    with SessionLocal() as db:
        assert jobs_for(db, "watch-early") == []                                   # 上线前 1 分钟 → 存量
        assert [j.trigger for j in jobs_for(db, "watch-after")] == ["watch"]       # 上线后创建 → 评审
        assert [j.trigger for j in jobs_for(db, "watch-modified")] == ["watch"]    # 存量上线后被改 → 评审
        assert db.get(Document, "watch-after").storage_dentry_id == "987654321"    # 下载键找回
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id == "987654321").delete(synchronize_session=False)
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
        assert jobs_for(db, "watch-pic") == [] and jobs_for(db, "watch-log") == []
        assert [job.trigger for job in jobs_for(db, "watch-doc2")] == ["watch"]
