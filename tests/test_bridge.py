import os

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import audit_bridge
from app.config import get_settings
from app.db import Document, FileAuditEvent, SessionLocal, SpaceMap, Workspace, init_db
from app.fileclass import classify, review_classes

WS = "bridge-ws"
WS_EMPTY = "bridge-empty-ws"


@pytest.fixture()
def env(monkeypatch):
    init_db()
    walks: list[dict] = []

    async def fake_walk(db, settings, workspace_id, space=None, mode="watch"):
        walks.append({"workspace_id": workspace_id, "mode": mode})
        return SimpleNamespace(run_id="fake-run", mode=mode, status="succeeded",
                               documents_new=0, documents_changed=0, error_code="")

    monkeypatch.setattr(audit_bridge, "watch_workspace", fake_walk)
    audit_bridge._last_walk.clear()
    with SessionLocal() as db:
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("tb-%")).delete(synchronize_session=False)
        db.query(SpaceMap).filter(SpaceMap.space_id.like("990%")).delete(synchronize_session=False)
        for ws_id, name in ((WS, "桥接测试库"), (WS_EMPTY, "空白库")):
            if not db.get(Workspace, ws_id):
                db.add(Workspace(workspace_id=ws_id, name=name))
        if not db.get(Document, "bridge-A"):
            db.add(Document(node_id="bridge-A", workspace_id=WS, name="桥接测试文档.docx",
                            extension="docx", file_class="document"))
        db.commit()
    settings = get_settings().model_copy(update={"bridge_enabled": True, "bridge_scope": "watched",
                                                 "bridge_debounce_seconds": 900})
    return settings, walks


def add_event(biz_id, resource, space_id, action_view="知识库上传文件", module_view="团队空间",
              extension="docx", gmt=1786400000000):
    with SessionLocal() as db:
        db.add(FileAuditEvent(biz_id=biz_id, gmt_create=gmt, action_view=action_view,
                              module_view=module_view, resource=resource, extension=extension,
                              target_space_id=space_id))
        db.commit()


def run_bridge(settings):
    with SessionLocal() as db:
        return audit_bridge.process_audit_events(db, settings)


def test_learn_map_walk_and_backfill(env):
    settings, walks = env
    add_event("tb-1", "桥接测试文档", "99001")
    summary = run_bridge(settings)
    assert summary["wiki_events"] == 1 and summary["learned"] == 1 and len(summary["walks"]) == 1
    assert walks == [{"workspace_id": WS, "mode": "bridge"}]
    with SessionLocal() as db:
        entry = db.get(SpaceMap, "99001")
        assert entry.workspace_id == WS and entry.source == "learned" and entry.event_count == 1
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-1"))
        assert event.processed is True and event.matched_node_id == "bridge-A"

    # Same space again inside the debounce window: mapped, but no second walk.
    add_event("tb-2", "桥接测试文档.docx", "99001")
    summary2 = run_bridge(settings)
    assert summary2["mapped"] == 1 and summary2["walks"] == []
    assert len(walks) == 1


def test_non_wiki_and_ambiguous_events(env):
    settings, walks = env
    add_event("tb-3", "随便.docx", "99002", action_view="上传文件", module_view="单聊")
    with SessionLocal() as db:  # same name in a second workspace -> ambiguous join
        if not db.get(Document, "bridge-B"):
            db.add(Document(node_id="bridge-B", workspace_id=WS_EMPTY, name="桥接测试文档.docx",
                            extension="docx", file_class="document"))
            db.commit()
    add_event("tb-4", "桥接测试文档.docx", "99003")
    summary = run_bridge(settings)
    assert summary["wiki_events"] == 1 and summary["learned"] == 0 and summary["walks"] == []
    with SessionLocal() as db:
        assert db.get(SpaceMap, "99003").workspace_id == ""
        assert db.scalar(select(FileAuditEvent.processed).where(FileAuditEvent.biz_id == "tb-3")) is True
        db.query(Document).filter(Document.node_id == "bridge-B").delete(synchronize_session=False)
        db.commit()


def test_snapshot_bootstrap_learns_unwalked_workspace(env):
    settings, walks = env
    from app.db import HistoricalFileNode, HistoricalSnapshot
    with SessionLocal() as db:
        if not db.get(HistoricalSnapshot, "bridge-snap"):
            db.add(HistoricalSnapshot(snapshot_id="bridge-snap"))
            db.add(HistoricalFileNode(snapshot_id="bridge-snap", workspace_id="never-walked-ws",
                                      node_id="snap-node-1", name="仅快照可见.docx", extension="docx"))
            db.commit()
    add_event("tb-7", "仅快照可见.docx", "99005")
    summary = run_bridge(settings)
    assert summary["learned"] == 1 and summary["walks"] == []  # learned, but watched-scope gates the walk
    with SessionLocal() as db:
        assert db.get(SpaceMap, "99005").workspace_id == "never-walked-ws"


def test_scope_gates_ungoverned_workspaces(env):
    settings, walks = env
    with SessionLocal() as db:
        db.add(SpaceMap(space_id="99004", workspace_id=WS_EMPTY, workspace_name="空白库", source="manual"))
        db.commit()
    add_event("tb-5", "无镜像文档.docx", "99004")
    assert run_bridge(settings)["walks"] == []  # watched scope: no mirror rows -> no walk
    audit_bridge._last_walk.clear()
    add_event("tb-6", "无镜像文档.docx", "99004")
    open_scope = run_bridge(settings.model_copy(update={"bridge_scope": "mapped"}))
    assert [walk["workspace_id"] for walk in open_scope["walks"]] == [WS_EMPTY]


def test_fileclass_and_notify_guardrails():
    assert classify("docx") == "document" and classify("adoc") == "native_doc"
    assert classify("log") == "engineering" and classify("png") == "image" and classify("", True) == "folder"
    assert "engineering" not in review_classes("") and "document" in review_classes("")
    assert review_classes("document, sheet") == {"document", "sheet"}

    from app.db import Notification, ReviewInstance
    from app.notify import enqueue_review_notification
    init_db()
    with SessionLocal() as db:
        doc = Document(node_id="bridge-notify", workspace_id=WS, name="x.docx", uploader_key="u1")
        instance = ReviewInstance(review_instance_id="ri-guard", node_id="bridge-notify", ai_score=50,
                                  verdict="return", review_scope="metadata_only")
        settings = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": "other-ws"})
        row = enqueue_review_notification(db, settings, doc, instance)
        assert row.status == "skipped" and row.error_code == "workspace_not_allowlisted"
        settings_open = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": ""})
        row2 = enqueue_review_notification(db, settings_open, doc, instance)
        db.add(doc); db.add(instance); db.flush()  # column defaults apply at flush
        assert row2.status == "pending" and row2.target_union_id == "u1"
        db.rollback()
