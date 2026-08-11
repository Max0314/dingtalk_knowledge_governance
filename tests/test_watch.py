import os

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

import pytest
from sqlalchemy import select

from app import service
from app.config import get_settings
from app.db import Document, ReviewInstance, ReviewJob, SessionLocal, init_db

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
