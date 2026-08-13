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


@pytest.fixture()
def env(monkeypatch):
    init_db()
    walks: list[dict] = []
    fail_next: list[bool] = []

    async def fake_walk(db, settings, workspace_id, space=None, mode="watch"):
        walks.append({"workspace_id": workspace_id, "mode": mode})
        status = "failed" if (fail_next and fail_next.pop(0)) else "succeeded"
        return SimpleNamespace(run_id="fake-run", mode=mode, status=status,
                               documents_new=0, documents_changed=0, error_code="")

    monkeypatch.setattr(audit_bridge, "watch_workspace", fake_walk)
    audit_bridge._last_walk.clear()
    with SessionLocal() as db:
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("tb-%")).delete(synchronize_session=False)
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("999%")).delete(synchronize_session=False)
        db.query(SpaceMap).filter(SpaceMap.space_id.like("990%")).delete(synchronize_session=False)
        if not db.get(Workspace, WS):
            db.add(Workspace(workspace_id=WS, name="桥接测试库"))
        if not db.get(Document, "bridge-A"):
            db.add(Document(node_id="bridge-A", workspace_id=WS, name="桥接测试文档.docx",
                            extension="docx", file_class="document"))
        db.commit()
    settings = get_settings().model_copy(update={"bridge_enabled": True, "bridge_debounce_seconds": 900,
                                                 "bridge_locator_enabled": False})
    return settings, walks, fail_next


def add_event(biz_id, resource, space_id="2932890480", action_view="知识库上传文件",
              module_view="团队空间", extension="docx", gmt=1786400000000):
    with SessionLocal() as db:
        db.add(FileAuditEvent(biz_id=biz_id, gmt_create=gmt, action_view=action_view,
                              module_view=module_view, resource=resource, extension=extension,
                              target_space_id=space_id))
        db.commit()


def run_bridge(settings):
    with SessionLocal() as db:
        return audit_bridge.process_audit_events(db, settings)


def test_wiki_event_sweeps_governed_set_with_debounce(env):
    settings, walks, _ = env
    add_event("tb-1", "桥接测试文档", "99001")
    summary = run_bridge(settings)
    assert summary["wiki_events"] == 1 and summary["matched"] == 1
    walked = {walk["workspace_id"] for walk in walks}
    assert WS in walked and all(walk["mode"] == "bridge" for walk in walks)
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-1"))
        assert event.processed is True and event.matched_node_id == "bridge-A"
        assert db.get(SpaceMap, "99001").event_count == 1  # tally survives as observability

    # Second ring inside the debounce window: consumed, but no new walks.
    count_before = len(walks)
    add_event("tb-2", "桥接测试文档.docx", "99001")
    assert run_bridge(settings)["wiki_events"] == 1
    assert len(walks) == count_before


def test_non_wiki_event_never_walks(env):
    settings, walks, _ = env
    add_event("tb-3", "随便.docx", "99002", action_view="上传文件", module_view="单聊")
    summary = run_bridge(settings)
    assert summary["wiki_events"] == 0 and summary["walks"] == [] and walks == []


def test_failed_walk_evicts_debounce_for_retry(env):
    settings, walks, fail_next = env
    fail_next.extend([True] * 10)  # every governed workspace fails this round
    add_event("tb-4", "任意文件.docx")
    first = run_bridge(settings)
    assert first["walks"] and all(walk["status"] == "failed" for walk in first["walks"])
    count_after_failure = len(walks)
    fail_next.clear()
    add_event("tb-5", "任意文件2.docx")
    second = run_bridge(settings)  # debounce evicted -> walks again immediately
    assert len(walks) > count_after_failure
    assert any(walk["status"] == "succeeded" for walk in second["walks"])


def test_snapshot_join_backfills_match(env):
    settings, walks, _ = env
    from app.db import HistoricalFileNode, HistoricalSnapshot
    with SessionLocal() as db:
        if not db.get(HistoricalSnapshot, "bridge-snap"):
            db.add(HistoricalSnapshot(snapshot_id="bridge-snap"))
            db.add(HistoricalFileNode(snapshot_id="bridge-snap", workspace_id="never-walked-ws",
                                      node_id="snap-node-1", name="仅快照可见.docx", extension="docx"))
            db.commit()
    add_event("tb-6", "仅快照可见.docx", "99005")
    assert run_bridge(settings)["matched"] == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-6"))
        assert event.matched_node_id == "snap-node-1"


def locator_settings(settings):
    return settings.model_copy(update={"bridge_locator_enabled": True, "dingtalk_sync_operator_id": "op",
                                       "wiki_storage_space_id": "2932890480"})


def test_locator_routes_precisely(env, monkeypatch):
    settings, walks, _ = env

    class FakeSearchClient:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "bridge-A", "name": "桥接测试文档.docx", "path": "/桥接测试库/桥接测试文档.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            assert node_ids == ["bridge-A"]
            return [{"name": "桥接测试文档.docx", "workspace_id": WS, "node_id": "bridge-A"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", FakeSearchClient)
    add_event("99900000001", "桥接测试文档.docx", "99009")  # digit bizId == numeric dentry id
    summary = run_bridge(locator_settings(settings))
    assert summary["unlocated"] == 0
    assert [walk["workspace_id"] for walk in summary["walks"]] == [WS]  # only the located workspace
    with SessionLocal() as db:
        doc = db.get(Document, "bridge-A")
        assert doc.storage_dentry_id == "99900000001"  # numeric download key attached from the event
        doc.storage_dentry_id = ""
        db.commit()


def test_locator_miss_falls_back_to_sweep(env, monkeypatch):
    settings, walks, _ = env

    class EmptySearchClient:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return []  # brand-new file not indexed yet

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return []

    monkeypatch.setattr(audit_bridge, "DingtalkClient", EmptySearchClient)
    add_event("tb-10", "全新未索引文件.docx", "99010")
    summary = run_bridge(locator_settings(settings))
    assert summary["unlocated"] == 1
    assert WS in {walk["workspace_id"] for walk in summary["walks"]}  # governed sweep fired


def test_notify_override_redirects_to_operator(monkeypatch):
    from app.db import Notification
    from app import notify as notify_module

    sent = []

    class FakeNotifyClient:
        def __init__(self, _settings):
            pass

        async def resolve_user_id(self, union_id):
            return "resolved-" + union_id

        async def send_robot_markdown(self, user_ids, title, text):
            sent.append((user_ids, title, text))

    monkeypatch.setattr(notify_module, "DingtalkClient", FakeNotifyClient)
    init_db()
    with SessionLocal() as db:
        db.add(Notification(node_id="n-ovr", target_union_id="u-999", title="评审退回", body="正文", status="pending"))
        db.commit()
        settings = get_settings().model_copy(update={"notify_enabled": True, "notify_digest_window_seconds": 0,
                                                     "notify_override_user_id": "01115324500438248944"})
        notify_module.process_pending_notifications(db, settings)
    ours = [item for item in sent if "u-999" in item[2]]
    assert ours and ours[0][0] == ["01115324500438248944"]


def test_notification_digest_batches_bursts(monkeypatch):
    from datetime import timedelta

    from app.db import Notification, utcnow
    from app import notify as notify_module

    sent = []

    class FakeNotifyClient:
        def __init__(self, _settings):
            pass

        async def resolve_user_id(self, union_id):
            return union_id

        async def send_robot_markdown(self, user_ids, title, text):
            sent.append((user_ids, title, text))

    monkeypatch.setattr(notify_module, "DingtalkClient", FakeNotifyClient)
    init_db()
    settings = get_settings().model_copy(update={"notify_enabled": True, "notify_override_user_id": "",
                                                 "notify_digest_window_seconds": 300,
                                                 "notify_digest_max_delay_seconds": 1800})
    with SessionLocal() as db:
        db.query(Notification).filter(Notification.target_union_id == "70000001").delete(synchronize_session=False)
        # Fresh burst rows -> the pump must hold them.
        for index in range(3):
            db.add(Notification(node_id=f"burst-{index}", target_union_id="70000001",
                                title=f"文档评审退回：批量文档{index}.docx", body="正文", status="pending"))
        db.commit()
        assert notify_module.process_pending_notifications(db, settings) == 0
        assert sent == []
        # Quiet window elapsed -> ONE digest message, all rows marked sent.
        for row in db.query(Notification).filter(Notification.target_union_id == "70000001").all():
            row.created_at = utcnow() - timedelta(seconds=400)
        db.commit()
        assert notify_module.process_pending_notifications(db, settings) == 3
        digest = [item for item in sent if "批量文档" in item[2]]
        assert len(digest) == 1 and "3 份" in digest[0][1]
        assert "AI应用研发部-陈鹏列" in digest[0][2]
        statuses = {row.status for row in db.query(Notification)
                    .filter(Notification.target_union_id == "70000001").all()}
        assert statuses == {"sent"}


def test_notification_digest_max_delay_forces_flush(monkeypatch):
    from datetime import timedelta

    from app.db import Notification, utcnow
    from app import notify as notify_module

    sent = []

    class FakeNotifyClient:
        def __init__(self, _settings):
            pass

        async def resolve_user_id(self, union_id):
            return union_id

        async def send_robot_markdown(self, user_ids, title, text):
            sent.append((user_ids, title, text))

    monkeypatch.setattr(notify_module, "DingtalkClient", FakeNotifyClient)
    init_db()
    settings = get_settings().model_copy(update={"notify_enabled": True, "notify_override_user_id": "",
                                                 "notify_digest_window_seconds": 300,
                                                 "notify_digest_max_delay_seconds": 1800})
    with SessionLocal() as db:
        db.query(Notification).filter(Notification.target_union_id == "70000002").delete(synchronize_session=False)
        old = Notification(node_id="force-0", target_union_id="70000002",
                           title="文档评审退回：最早的文档.docx", body="正文", status="pending")
        db.add(old); db.commit()
        old.created_at = utcnow() - timedelta(seconds=1900)  # past max delay
        fresh = Notification(node_id="force-1", target_union_id="70000002",
                             title="文档评审退回：刚上传的文档.docx", body="正文", status="pending")
        db.add(fresh); db.commit()  # burst still ongoing, but max delay wins
        assert notify_module.process_pending_notifications(db, settings) == 2
        assert any("2 份" in item[1] for item in sent)


def test_notify_pass_gate_copy_and_pilot_footer():
    from types import SimpleNamespace

    from app import notify as notify_module

    doc = SimpleNamespace(node_id="pass-1", workspace_id="ws-x", uploader_key="u-1", name="样例.docx")
    ok = SimpleNamespace(review_instance_id="ri-ok", ai_score=88.0, verdict="pass",
                         review_scope="full_content", rule_version="V1.1", findings=[])
    low = SimpleNamespace(review_instance_id="ri-low", ai_score=54.0, verdict="return",
                          review_scope="metadata_only", rule_version="V1.1",
                          findings=[{"message": "标题未标注版本号"}])
    t_ok, b_ok = notify_module.build_message(doc, ok, "https://kg.example.com/prefix")
    t_low, b_low = notify_module.build_message(doc, low, "https://kg.example.com/prefix")
    assert "通过" in t_ok and "质量达标" in b_ok
    # 试点口径：低分只做说明，不出现"退回"字样；带分析页链接
    assert "低分说明" in t_low and "退回" not in b_low and "质量提示" in b_low
    assert "https://kg.example.com/prefix/#/doc/pass-1" in b_low and "](http" not in b_ok
    for body in (b_ok, b_low):
        assert "AI应用研发部-陈鹏列" in body

    init_db()
    with SessionLocal() as db:
        base = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": ""})
        assert notify_module.enqueue_review_notification(db, base, doc, ok) is None  # 默认不推合格
        row = notify_module.enqueue_review_notification(db, base.model_copy(update={"notify_on_pass": True}), doc, ok)
        assert row is not None and "通过" in row.title
        db.rollback()


def test_notify_department_allowlist():
    from types import SimpleNamespace

    from app import notify as notify_module

    low = SimpleNamespace(review_instance_id="ri-dept", ai_score=50.0, verdict="return",
                          review_scope="metadata_only", rule_version="V1.1", findings=[])
    doc_in = SimpleNamespace(node_id="dept-1", workspace_id="ws-x", uploader_key="u-1",
                             name="a.docx", department_name="AI应用研发部")
    doc_out = SimpleNamespace(node_id="dept-2", workspace_id="ws-x", uploader_key="u-2",
                              name="b.docx", department_name="质量部")
    doc_unmapped = SimpleNamespace(node_id="dept-3", workspace_id="ws-x", uploader_key="u-3",
                                   name="c.docx", department_name="未映射")
    init_db()
    with SessionLocal() as db:
        settings = get_settings().model_copy(update={
            "notify_enabled": True, "notify_workspaces": "",
            "notify_departments": "数字化转型部, AI应用研发部"})
        row_in = notify_module.enqueue_review_notification(db, settings, doc_in, low)
        assert row_in is not None and row_in.status == "pending"
        for doc in (doc_out, doc_unmapped):
            row = notify_module.enqueue_review_notification(db, settings, doc, low)
            assert row is not None and row.status == "skipped" and row.error_code == "department_not_allowlisted"
        db.rollback()


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
