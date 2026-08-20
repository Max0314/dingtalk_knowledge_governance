import os

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import audit_bridge
from app.audit_pull import _filename_extension
from app.config import get_settings
from app.db import Document, FileAuditEvent, ReviewInstance, ReviewJob, SessionLocal, SpaceMap, Workspace, init_db
from app.fileclass import classify, review_classes

WS = "bridge-ws"
DEFAULT_GMT = 1786400000000  # add_event 的默认事件时间


def gmt_iso(gmt_ms=DEFAULT_GMT):
    from datetime import datetime, timezone as _tz
    return datetime.fromtimestamp(gmt_ms / 1000, tz=_tz.utc).isoformat()


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
        from app.db import BridgeWalk, Notification
        db.query(BridgeWalk).delete(synchronize_session=False)
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("tb-%")).delete(synchronize_session=False)
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("999%")).delete(synchronize_session=False)
        db.query(SpaceMap).filter(SpaceMap.space_id.like("990%")).delete(synchronize_session=False)
        # 上一次失败运行可能留下测试文档（各测试的收尾清理没跑到），开场兜底
        stale_ids = ["bridge-late-doc", "old-same-2", "old-node-same", "dup-a", "dup-b", "stock-doc-1",
                     "late-cp-doc", "unknown-old", "fresh-node-1", "rn-doc-1", "old-touched",
                     "del-doc-1", "same-name-del", "rest-doc-1", "mod-doc-1", "own-doc-1", "nd-doc-1",
                     "matrix-1", "matrix-2", "matrix-3", "cover-doc-1", "adoc-native-1", "bad-audit-ext"]
        stale_ids += [row[0] for row in db.execute(select(Document.node_id)
                                                   .where(Document.node_id.like("cp-doc-%")))]
        for node_id in stale_ids:
            leftover = db.get(Document, node_id)
            if leftover:
                # 其他测试沥干任务队列时可能执行过这些文档的评审——实例
                # 不清会让 ORM 删除时置空外键而炸 NOT NULL
                db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
                db.query(ReviewInstance).filter(ReviewInstance.node_id == node_id).delete(synchronize_session=False)
                db.query(Notification).filter(Notification.node_id == node_id).delete(synchronize_session=False)
                db.delete(leftover)
        if not db.get(Workspace, WS):
            db.add(Workspace(workspace_id=WS, name="桥接测试库"))
        # 镜像文档带与默认事件时间互证得上的时间戳（真实 watcher 建档必有）；
        # 共享测试库里可能残留旧行，字段必须每次刷新
        db.merge(Document(node_id="bridge-A", workspace_id=WS, name="桥接测试文档.docx",
                          extension="docx", file_class="document", storage_dentry_id="",
                          source_created_at=gmt_iso(), source_updated_at=gmt_iso()))
        db.commit()
    settings = get_settings().model_copy(update={"bridge_enabled": True, "bridge_debounce_seconds": 900,
                                                 "bridge_locator_enabled": False,
                                                 "bridge_sweep_max_governed": 999})  # 旧测试保留试点兜底扫语义
    return settings, walks, fail_next


def add_event(biz_id, resource, space_id="2932890480", action_view="知识库上传文件",
              module_view="团队空间", extension="docx", gmt=1786400000000, received=None, operator=""):
    with SessionLocal() as db:
        kwargs = dict(biz_id=biz_id, gmt_create=gmt, action_view=action_view,
                      module_view=module_view, resource=resource, extension=extension,
                      target_space_id=space_id)
        if received is not None:
            kwargs["received_at"] = received  # 重试窗口基准（默认=入库当下）
        if operator:
            kwargs["operator_user_id"] = operator
        db.add(FileAuditEvent(**kwargs))
        db.commit()


def old_received(days=3):
    from datetime import datetime, timedelta, timezone as tz
    return datetime.now(tz.utc) - timedelta(days=days)


def run_bridge(settings):
    with SessionLocal() as db:
        return audit_bridge.process_audit_events(db, settings)


def test_wiki_event_sweeps_governed_set_with_debounce(env):
    settings, walks, _ = env
    add_event("tb-1", "桥接测试文档", "99001", received=old_received())  # 过期→死信归档路径
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


def test_audit_extension_prefers_filename_when_trail_metadata_is_wrong():
    assert _filename_extension("工具领取明细.xlsx", "adoc") == "xlsx"
    assert _filename_extension("无后缀在线文档", "adoc") == "adoc"


def test_pre_cutover_event_is_retained_without_locator_or_review(env, monkeypatch):
    """方案 A：修复前积压只留审计终态，绝不触发定位、入队或通知。"""
    settings, walks, _ = env

    class LocatorMustNotRun:
        def __init__(self, _settings):
            raise AssertionError("pre-cutover event must not call the locator")

    monkeypatch.setattr(audit_bridge, "DingtalkClient", LocatorMustNotRun)
    add_event("99997000001", "修复前积压.docx", "99297", gmt=DEFAULT_GMT)
    org = locator_settings(settings).model_copy(update={"audit_review_since": gmt_iso(DEFAULT_GMT + 1)})
    summary = run_bridge(org)
    assert summary["pre_cutover_not_reviewed"] == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99997000001"))
        assert event.processed is True and event.resolution == "pre_cutover_not_reviewed"
        assert event.matched_node_id == ""
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "bridge-A")).all() == []


def test_exact_fresh_node_confirms_despite_bad_audit_extension(env, monkeypatch):
    """真实节点的精确文件名和创建时间比审计扩展名提示更可靠。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class BadExtensionSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            assert keyword == "误标格式的台账.xlsx"
            return [{"dentry_uuid": "bad-audit-ext", "name": "误标格式的台账.xlsx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "误标格式的台账.xlsx", "workspace_id": WS, "node_id": "bad-audit-ext",
                     "extension": "xlsx", "size": 31114, "created_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", BadExtensionSearch)
    add_event("99997000002", "误标格式的台账.xlsx", "99298", extension="adoc", gmt=now_ms)
    summary = run_bridge(locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0}))
    assert summary["confirmed"] == 1 and summary["extension_mismatch_confirmed"] == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99997000002"))
        doc = db.get(Document, "bad-audit-ext")
        assert event.processed is True and event.resolution == "done"
        assert doc is not None and doc.extension == "xlsx" and doc.storage_dentry_id == "99997000002"
        assert [job.trigger for job in db.scalars(select(ReviewJob).where(ReviewJob.node_id == doc.node_id)).all()] == ["audit"]
        db.query(ReviewJob).filter(ReviewJob.node_id == doc.node_id).delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_locator_routes_precisely(env, monkeypatch):
    settings, walks, _ = env

    class FakeSearchClient:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            assert space_ids == ["2932890480"]  # uploaded files stay in the configured storage scope
            return [{"dentry_uuid": "bridge-A", "name": "桥接测试文档.docx", "path": "/桥接测试库/桥接测试文档.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            assert node_ids == ["bridge-A"]
            return [{"name": "桥接测试文档.docx", "workspace_id": WS, "node_id": "bridge-A"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", FakeSearchClient)
    add_event("99900000001", "桥接测试文档.docx", "99009")  # digit bizId == numeric dentry id
    summary = run_bridge(locator_settings(settings))
    assert summary["unlocated"] == 0
    assert summary["walks"] == [] and summary.get("direct_upserts") == 1  # 直建取代整库巡走
    with SessionLocal() as db:
        doc = db.get(Document, "bridge-A")
        assert doc.storage_dentry_id == "99900000001"  # numeric download key attached from the event
        db.query(ReviewJob).filter(ReviewJob.node_id == "bridge-A").delete(synchronize_session=False)
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


class _EmptySearchClient:
    def __init__(self, _settings):
        pass

    async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
        return []

    async def batch_query_wiki_nodes(self, node_ids, operator_id):
        return []


def test_unlocated_event_stays_pending_until_locator_confirms(env, monkeypatch):
    """事件保持 pending 重试；watcher 建档 + 搜索索引跟上后由 locator
    确认，下载键挂上、正文重评入队。名称联结永不能替代确认。"""
    import time as time_module

    from app.db import ReviewJob

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99920000001", "重试到镜像出现", "99020", gmt=int(time_module.time() * 1000))
    s1 = run_bridge(org)
    assert s1["unlocated"] == 1 and s1["walks"] == [] and s1.get("pending_retry") == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99920000001"))
        assert event.processed is False and event.matched_node_id == "" and event.last_attempt_at is not None
        # watcher 把文档建进镜像（带真实时间戳）；名称联结只能 provisional
        now_iso = gmt_iso(int(time_module.time() * 1000))
        db.merge(Document(node_id="bridge-late-doc", workspace_id=WS, name="重试到镜像出现.docx",
                          extension="docx", file_class="document",
                          source_created_at=now_iso, source_updated_at=now_iso))
        db.commit()
    s_mid = run_bridge(org)
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99920000001"))
        assert event.processed is False and event.match_status == "provisional"
        assert db.get(Document, "bridge-late-doc").storage_dentry_id == ""  # provisional 不挂键

    class HitSearchClient:  # 搜索索引跟上了
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "bridge-late-doc", "name": "重试到镜像出现.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "重试到镜像出现.docx", "workspace_id": WS, "node_id": "bridge-late-doc"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", HitSearchClient)
    s2 = run_bridge(org)
    assert s2["confirmed"] == 1 and s2.get("pending_retry", 0) == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99920000001"))
        assert event.processed is True and event.match_status == "confirmed" and event.resolution == "done"
        doc = db.get(Document, "bridge-late-doc")
        assert doc.storage_dentry_id == "99920000001"
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "bridge-late-doc")).all()
        assert [job.trigger for job in jobs] == ["audit"]  # 直建路径的评审触发标记
        for job in jobs:
            db.delete(job)
        db.delete(doc)
        db.commit()


def test_stale_unmatched_event_becomes_observable_dead_letter(env, monkeypatch):
    """超时事件转入带原因的死信，绝不伪装成功（codex 第四轮 P0）。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99930000001", "永远找不到的文件", "99030",
              gmt=int(time_module.time() * 1000) - 3 * 24 * 3600 * 1000, received=old_received())
    summary = run_bridge(org)
    assert summary.get("dead_letter") == 1 and summary.get("pending_retry") == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99930000001"))
        assert event.processed is True and event.matched_node_id == ""
        assert event.resolution == "dead_letter_unmatched"


def test_same_name_new_upload_never_pins_old_doc(env, monkeypatch):
    """codex 第四轮 P0：同名新上传的事件绝不能把键挂到镜像里的旧节点，
    也不触发旧文档评审——名称联结只做 provisional。"""
    import time as time_module

    from app.db import ReviewJob

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    with SessionLocal() as db:
        db.merge(Document(node_id="old-node-same", workspace_id=WS, name="同名文件.docx",
                          extension="docx", file_class="document"))
        db.commit()
    add_event("99940000001", "同名文件", "99040", gmt=int(time_module.time() * 1000))
    run_bridge(org)
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99940000001"))
        old = db.get(Document, "old-node-same")
        assert old.storage_dentry_id == ""                         # 新文件的键没挂到旧文档
        assert event.processed is False and event.match_status == "provisional"
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "old-node-same")).all() == []
        db.delete(old)
        db.commit()


def test_locator_budget_rotates_fairly(env, monkeypatch):
    """codex 第四轮 P0：定位额度按"最久未尝试"轮转，9 条事件两轮内
    全部获得定位机会，不存在永久队首阻塞。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    now_ms = int(time_module.time() * 1000)
    for index in range(9):
        add_event(f"tb-rot{index}", f"轮转文件{index}", "99050", gmt=now_ms + index)
    run_bridge(org)
    run_bridge(org)
    with SessionLocal() as db:
        events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.biz_id.like("tb-rot%"))).all()
        assert len(events) == 9 and all(event.last_attempt_at is not None for event in events)


def test_search_hit_on_old_same_name_doc_is_not_confirmed(env, monkeypatch):
    """codex 第五轮 P0：同名新文件未入索引时，搜索只会返回旧节点——搜索
    唯一命中也不得 confirmed，必须与镜像时间/扩展名互证。"""
    import time as time_module

    settings, walks, _ = env
    with SessionLocal() as db:
        db.merge(Document(node_id="old-same-2", workspace_id=WS, name="互证同名.docx", extension="docx",
                          file_class="document", source_created_at="2026-08-01T09:00:00Z",
                          source_updated_at="2026-08-01T09:00:00Z"))
        db.commit()

    class OldOnlySearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "old-same-2", "name": "互证同名.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "互证同名.docx", "workspace_id": WS, "node_id": "old-same-2"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", OldOnlySearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99950000001", "互证同名", "99060", gmt=int(time_module.time() * 1000))
    summary = run_bridge(org)
    assert summary.get("uncorroborated") == 1 and summary["confirmed"] == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99950000001"))
        old = db.get(Document, "old-same-2")
        assert old.storage_dentry_id == ""            # 新文件的键没有挂到旧文档
        assert event.processed is False and event.match_status != "confirmed"
        db.delete(old)
        db.commit()


def test_same_workspace_double_enqueue_no_conflict(env):
    """codex 第五轮 P0 回归（机制级）：同一事务内对同一库排队两次不得撞
    bridge_walk_queue 主键——autoflush 关闭时 db.get 看不到刚 add 的行。"""
    from app.db import BridgeWalk

    settings, walks, _ = env
    with SessionLocal() as db:
        queued: set = set()
        audit_bridge._enqueue_walk(db, WS, queued)
        audit_bridge._enqueue_walk(db, WS, queued)  # 修复前：IntegrityError
        db.commit()
        rows = db.scalars(select(BridgeWalk).where(BridgeWalk.workspace_id == WS)).all()
        assert len(rows) == 1
        db.delete(rows[0])
        db.commit()


def test_locator_rotation_reaches_beyond_batch_window(env, monkeypatch):
    """codex 第五轮 P0：定位候选在数据库层全局按最久未尝试取额——BATCH
    窗口之外的事件同样按轮次获得机会。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "BATCH", 10)
    monkeypatch.setattr(audit_bridge, "WIKI_LOCATE_BUDGET", 2)
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    now_ms = int(time_module.time() * 1000)
    for index in range(12):
        add_event(f"tb-glob{index}", f"批外轮转{index}", "99080", gmt=now_ms + index)
    for _ in range(6):
        run_bridge(org)
    with SessionLocal() as db:
        events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.biz_id.like("tb-glob%"))).all()
        assert len(events) == 12
        assert all(event.last_attempt_at is not None for event in events)


def test_reopened_dead_letter_gets_fresh_retry_window(env, monkeypatch):
    """codex 第五轮 P1：死信重开重置 received_at，获得完整的新 48h 窗口，
    不会一轮之内立即再次死信。"""
    import time as time_module

    from app.db import utcnow as db_utcnow

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    old_ms = int(time_module.time() * 1000) - 5 * 24 * 3600 * 1000
    add_event("99970000001", "死信重开样本", "99090", gmt=old_ms, received=old_received(5))
    first = run_bridge(org)
    assert first.get("dead_letter") == 1
    with SessionLocal() as db:  # 与 reopen_dead_letters.py 相同的重开语义
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99970000001"))
        event.processed, event.resolution, event.last_attempt_at = False, "", None
        event.retry_started_at = db_utcnow()  # received_at 是入库审计字段，不动
        db.commit()
    second = run_bridge(org)
    assert second.get("dead_letter", 0) == 0 and second.get("pending_retry") == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99970000001"))
        assert event.processed is False and event.last_attempt_at is not None


def test_unknown_node_corroborates_by_payload_not_by_ignorance(env, monkeypatch):
    """codex 第六轮 P0：镜像/快照都不认识 ≠ 新建。互证依据是 locator 载荷
    自带的时间——旧载荷拒确认，时间吻合的载荷才确认。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class UnknownOldSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "unknown-old", "name": "未知旧节点.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "未知旧节点.docx", "workspace_id": WS, "node_id": "unknown-old",
                     "extension": "docx", "created_at": "2026-08-07T09:00:00Z",
                     "updated_at": "2026-08-07T09:00:00Z"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", UnknownOldSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99980000001", "未知旧节点", "99110", gmt=now_ms)
    first = run_bridge(org)
    assert first.get("uncorroborated") == 1 and first["confirmed"] == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99980000001"))
        assert event.processed is False and event.match_status != "confirmed"

    class UnknownFreshSearch(UnknownOldSearch):
        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "未知旧节点.docx", "workspace_id": WS, "node_id": "unknown-old",
                     "extension": "docx", "created_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", UnknownFreshSearch)
    second = run_bridge(org)
    assert second["confirmed"] == 1  # 载荷时间与事件吻合的全新节点才是本事件的节点
    with SessionLocal() as db:  # 新语义：确认即直建文档——必须清理，否则毒化下次运行
        doc = db.get(Document, "unknown-old")
        assert doc is not None and doc.storage_dentry_id == "99980000001"
        db.query(ReviewJob).filter(ReviewJob.node_id == "unknown-old").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_pre_cutoff_event_attaches_key_without_review(env, monkeypatch):
    """codex 第六轮 P0：审计游标重叠重放的截止前事件——键照挂，但绝不生成
    content_key 评审任务（存量忽略、不补评分）。"""
    from datetime import datetime as dt, timezone as tz

    settings, walks, _ = env
    old_iso = "2026-08-01T09:00:00+00:00"
    old_ms = int(dt.fromisoformat(old_iso).timestamp() * 1000)
    with SessionLocal() as db:
        db.merge(Document(node_id="stock-doc-1", workspace_id=WS, name="截止前存量.docx",
                          extension="docx", file_class="document", storage_dentry_id="",
                          source_created_at=old_iso, source_updated_at=old_iso))
        db.merge(ReviewInstance(review_instance_id="stock-meta-old", node_id="stock-doc-1",
                                ai_score=80, verdict="pass", review_scope="metadata_only",
                                rule_version="V1.1", trigger="legacy_precheck"))
        db.commit()

    class StockSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "stock-doc-1", "name": "截止前存量.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "截止前存量.docx", "workspace_id": WS, "node_id": "stock-doc-1",
                     "extension": "docx", "created_at": old_iso}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", StockSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0,
                                                        "review_since": "2026-08-10T00:00:00Z"})
    add_event("99990000001", "截止前存量", "99120", gmt=old_ms)
    summary = run_bridge(org)
    assert summary["confirmed"] == 1
    with SessionLocal() as db:
        doc = db.get(Document, "stock-doc-1")
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99990000001"))
        assert doc.storage_dentry_id == "99990000001"  # 键挂上（allowed）
        assert event.processed is True and event.resolution == "done"
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "stock-doc-1")).all() == []
        db.query(ReviewInstance).filter(ReviewInstance.node_id == "stock-doc-1").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_confirmed_pending_finishes_beyond_batch(env, monkeypatch):
    """codex 第六轮 P0：confirmed 待完成事件的收尾走全局取额，不被 BATCH
    窗口截断——批外的第 11、12 条同轮完成、键挂上。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "BATCH", 10)
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    now_ms = int(time_module.time() * 1000)
    now_iso = gmt_iso(now_ms)
    with SessionLocal() as db:
        for index in range(12):
            db.merge(Document(node_id=f"cp-doc-{index}", workspace_id=WS, name=f"批外完成{index}.docx",
                              extension="docx", file_class="document", storage_dentry_id="",
                              source_created_at=now_iso, source_updated_at=now_iso))
            db.add(FileAuditEvent(biz_id=f"99911100{index:02d}", gmt_create=now_ms + index,
                                  action_view="知识库上传文件", module_view="团队空间",
                                  resource=f"批外完成{index}", extension="docx", target_space_id="99130",
                                  matched_node_id=f"cp-doc-{index}", match_status="confirmed"))
        db.commit()
    run_bridge(org)
    with SessionLocal() as db:
        remaining = db.scalars(select(FileAuditEvent)
                               .where(FileAuditEvent.biz_id.like("999111%"),
                                      FileAuditEvent.processed.is_(False))).all()
        assert remaining == []
        for index in range(12):
            doc = db.get(Document, f"cp-doc-{index}")
            assert doc.storage_dentry_id == f"99911100{index:02d}"
            db.query(ReviewJob).filter(ReviewJob.node_id == doc.node_id).delete(synchronize_session=False)
            db.delete(doc)
        db.commit()


def test_failing_walk_rows_rotate_not_starve(env, monkeypatch):
    """codex 第六轮 P0：持续失败的前 5 库按 last_attempt_at 轮转让位，
    第 6 库第二轮就被走到；成功出队、失败计数。"""
    from types import SimpleNamespace as NS

    from app.db import BridgeWalk

    settings, walks, _ = env

    async def picky_walk(db, settings_, workspace_id, space=None, mode="watch"):
        ok = workspace_id == "rot-ok"
        return NS(run_id="r-" + workspace_id, mode=mode, status="succeeded" if ok else "failed",
                  documents_seen=0, documents_new=0, documents_changed=0,
                  error_code="" if ok else "boom")

    monkeypatch.setattr(audit_bridge, "watch_workspace", picky_walk)
    with SessionLocal() as db:
        for index in range(5):
            db.add(BridgeWalk(workspace_id=f"rot-f{index}"))
        db.add(BridgeWalk(workspace_id="rot-ok"))
        db.commit()
    first = run_bridge(settings)
    assert all(walk["workspace_id"].startswith("rot-f") for walk in first["walks"])  # 预算被失败库占满
    second = run_bridge(settings)
    assert "rot-ok" in [walk["workspace_id"] for walk in second["walks"]]
    with SessionLocal() as db:
        assert db.get(BridgeWalk, "rot-ok") is None  # 成功出队
        rows = db.scalars(select(BridgeWalk).where(BridgeWalk.workspace_id.like("rot-%"))).all()
        assert {row.workspace_id for row in rows} == {f"rot-f{index}" for index in range(5)}
        assert all(row.failures >= 1 for row in rows)
        db.query(BridgeWalk).filter(BridgeWalk.workspace_id.like("rot-%")).delete(synchronize_session=False)
        db.commit()


def test_expired_treats_naive_datetimes_as_utc():
    """codex 第六轮 P1：MySQL 回读的 naive datetime 是 UTC 值——按本地时区
    （Asia/Shanghai）解释会让 48h 窗口缩成 40h。41h 不得过期，49h 过期。"""
    import time as time_module
    from datetime import datetime as dt, timedelta, timezone as tz
    from types import SimpleNamespace as NS

    now_ms = int(time_module.time() * 1000)
    naive_utc = lambda hours: dt.now(tz.utc).replace(tzinfo=None) - timedelta(hours=hours)
    assert audit_bridge._expired(NS(retry_started_at=None, received_at=naive_utc(41), gmt_create=0), now_ms) is False
    assert audit_bridge._expired(NS(retry_started_at=None, received_at=naive_utc(49), gmt_create=0), now_ms) is True
    # retry_started_at 优先于 received_at：重开后的新窗口生效
    assert audit_bridge._expired(NS(retry_started_at=naive_utc(1), received_at=naive_utc(100), gmt_create=0), now_ms) is False


def test_upload_event_ignores_recent_update_on_old_node(env, monkeypatch):
    """codex 第七轮 P0：上传事件只认 created_at 互证——旧同名节点刚被人
    修改（updated_at 落在 ±15 分钟窗口）也不是这次上传的节点；修改类
    事件才允许 updated_at 互证。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class RecentlyTouchedOldSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "old-touched", "name": "刚被改过的旧文件.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "刚被改过的旧文件.docx", "workspace_id": WS, "node_id": "old-touched",
                     "extension": "docx", "created_at": "2026-08-01T09:00:00Z",
                     "updated_at": gmt_iso(now_ms)}]  # 旧节点，但刚被修改过

    monkeypatch.setattr(audit_bridge, "DingtalkClient", RecentlyTouchedOldSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99912000001", "刚被改过的旧文件", "99140", gmt=now_ms)  # 默认动作=上传
    first = run_bridge(org)
    assert first.get("uncorroborated") == 1 and first["confirmed"] == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99912000001"))
        assert event.processed is False and event.match_status != "confirmed"

    # 修改类事件对同样的载荷：updated_at 互证成立
    add_event("99912000002", "刚被改过的旧文件", "99140", gmt=now_ms + 1, action_view="知识库修改文件")
    second = run_bridge(org)
    assert second["confirmed"] == 1


def test_cover_file_uses_updated_at_and_queues_review(env, monkeypatch):
    """生产真实动作“覆盖文件”复用旧节点：必须以 updated_at 互证，确认后
    挂正文下载键并立即入队评审，不能被当成创建事件或 unknown。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)
    old_iso = "2026-08-01T09:00:00+00:00"
    with SessionLocal() as db:
        db.merge(Document(node_id="cover-doc-1", workspace_id=WS, name="待覆盖.docx",
                          extension="docx", file_class="document", storage_dentry_id="",
                          uploader_key="human-uploader", uploader_name="人工上传人",
                          source_created_at=old_iso, source_updated_at=old_iso))
        db.commit()

    class CoverSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "cover-doc-1", "name": "待覆盖.docx",
                     "path": "/桥接测试库/待覆盖.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "待覆盖.docx", "workspace_id": WS, "node_id": "cover-doc-1",
                     "extension": "docx", "created_at": old_iso, "updated_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", CoverSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99912100001", "待覆盖", "99141", gmt=now_ms, action_view="覆盖文件")
    summary = run_bridge(org)
    assert summary["confirmed"] == 1 and summary.get("direct_upserts") == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99912100001"))
        assert event.processed is True and event.resolution == "done"
        doc = db.get(Document, "cover-doc-1")
        assert doc.storage_dentry_id == "99912100001"
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "cover-doc-1")).all()
        assert [job.trigger for job in jobs] == ["audit"]
        for job in jobs:
            db.delete(job)
        db.delete(doc)
        db.commit()


@pytest.mark.parametrize("action_view", (
    "知识库上传文件", "创建文档", "创建副本", "复制或转发文件", "文档导入 ",
))
def test_all_production_creation_actions_ignore_updated_at(action_view):
    """生产审计中所有会创建节点的动作都必须走 created_at 互证。"""
    event = SimpleNamespace(action_view=action_view)
    assert audit_bridge._is_creation_event(event) is True


def test_creation_event_rejects_payload_size_conflict(env):
    """两个同名节点即使都在 15 分钟窗口内创建，大小冲突也不能确认。"""
    settings, walks, _ = env
    event = SimpleNamespace(action_view="知识库上传文件", extension="docx", size=123,
                            gmt_create=DEFAULT_GMT, biz_id="99914000001")
    node = {"node_id": "same-name-recent", "extension": "docx", "size": 456,
            "created_at": gmt_iso(DEFAULT_GMT), "updated_at": gmt_iso(DEFAULT_GMT)}
    with SessionLocal() as db:
        assert audit_bridge._event_matches_node(db, event, node) is False


def test_creation_event_accepts_dingtalk_beijing_wall_clock_labeled_z(env):
    """Production Wiki batchQuery emits Beijing time but labels it ``Z``.

    The audit timestamp is authoritative UTC.  Without the compatibility
    interpretation below, this exact same upload looks eight hours apart and
    remains permanently pending before any body/model work can start.
    """
    from datetime import datetime, timedelta, timezone

    moment = datetime.fromtimestamp(DEFAULT_GMT / 1000, timezone.utc)
    mislabeled = (moment + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%MZ")
    event = SimpleNamespace(action_view="知识库上传文件", extension="docx", size=0,
                            gmt_create=DEFAULT_GMT, biz_id="99914000002")
    node = {"node_id": "same-upload-beijing-clock", "extension": "docx", "size": 607350,
            "created_at": mislabeled, "updated_at": mislabeled}
    with SessionLocal() as db:
        assert audit_bridge._event_matches_node(db, event, node) is True


def test_explicit_utc_node_time_is_not_reinterpreted_as_beijing(env):
    """Only DingTalk's malformed trailing-Z form gets the compatibility path."""
    from datetime import datetime, timedelta, timezone

    later = datetime.fromtimestamp((DEFAULT_GMT + 8 * 3600 * 1000) / 1000, timezone.utc)
    assert audit_bridge._near_event(later.isoformat(), DEFAULT_GMT) is False


def test_confirmed_finish_budget_rotates(env, monkeypatch):
    """codex 第七轮 P0：confirmed 收尾额度按最久未尝试轮转——文档迟迟
    不来的老事件不得堵死后来者。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "CONFIRM_FINISH_BUDGET", 2)
    monkeypatch.setattr(audit_bridge, "BATCH", 0)  # 隔离批处理通道，只看全局收尾的轮转
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    now_ms = int(time_module.time() * 1000)
    now_iso = gmt_iso(now_ms)
    with SessionLocal() as db:
        for index in (0, 1):  # 两个"文档永不入镜像"的老 confirmed
            db.add(FileAuditEvent(biz_id=f"999131000{index}", gmt_create=now_ms + index,
                                  action_view="知识库上传文件", module_view="团队空间",
                                  resource=f"永不入镜像{index}", extension="docx", target_space_id="99150",
                                  matched_node_id=f"ghost-doc-{index}", match_status="confirmed"))
        db.merge(Document(node_id="late-cp-doc", workspace_id=WS, name="轮转收尾.docx", extension="docx",
                          file_class="document", storage_dentry_id="",
                          source_created_at=now_iso, source_updated_at=now_iso))
        db.add(FileAuditEvent(biz_id="9991310002", gmt_create=now_ms + 2, action_view="知识库上传文件",
                              module_view="团队空间", resource="轮转收尾", extension="docx",
                              target_space_id="99150", matched_node_id="late-cp-doc", match_status="confirmed"))
        db.commit()
    run_bridge(org)  # 额度 2：本轮只尝试两个 ghost
    with SessionLocal() as db:
        third = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "9991310002"))
        assert third.processed is False
    run_bridge(org)  # ghost 已盖章转到队尾 → 第三条获得额度并完成
    with SessionLocal() as db:
        third = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "9991310002"))
        assert third.processed is True and third.resolution == "done"
        doc = db.get(Document, "late-cp-doc")
        assert doc.storage_dentry_id == "9991310002"
        db.query(ReviewJob).filter(ReviewJob.node_id == "late-cp-doc").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_audit_event_direct_upserts_new_document(env, monkeypatch):
    """2026-08-14 流程定稿：事件确认后直接建档——新库自动注册占位
    （不拉回连续轮巡）、path 落库、目录待定、键挂上、评审入队，
    全程零整库巡走。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class NewDocSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "fresh-node-1", "name": "全新直建.docx",
                     "path": "/新库/子目录/全新直建.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "全新直建.docx", "workspace_id": "brand-new-ws", "node_id": "fresh-node-1",
                     "extension": "docx", "size": 10, "created_at": gmt_iso(now_ms), "creator_id": "tester"}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", NewDocSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99941000001", "全新直建", "99160", gmt=now_ms)
    summary = run_bridge(org)
    assert summary.get("direct_upserts") == 1 and summary["walks"] == []
    with SessionLocal() as db:
        from app.db import Workspace

        doc = db.get(Document, "fresh-node-1")
        assert doc is not None and doc.storage_dentry_id == "99941000001"
        assert doc.path == "/新库/子目录/全新直建.docx" and doc.directory_pending is True
        workspace = db.get(Workspace, "brand-new-ws")
        assert workspace is not None and workspace.watch_seeded  # 不拉回连续轮巡
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "fresh-node-1")).all()
        assert [job.trigger for job in jobs] == ["audit"]
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99941000001"))
        assert event.processed is True and event.resolution == "done"
        for job in jobs:
            db.delete(job)
        db.delete(doc)
        db.delete(workspace)
        db.commit()


def test_rename_event_updates_metadata_without_review(env, monkeypatch):
    """重命名/移动只动元数据与路径，不触发质量评审（2026-08-14 流程定稿）。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)
    old_iso = "2026-08-01T09:00:00+00:00"
    with SessionLocal() as db:
        db.merge(Document(node_id="rn-doc-1", workspace_id=WS, name="旧名字.docx", extension="docx",
                          file_class="document", storage_dentry_id="",
                          source_created_at=old_iso, source_updated_at=old_iso))
        db.commit()

    class RenameSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "rn-doc-1", "name": "新名字.docx", "path": "/桥接测试库/新名字.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "新名字.docx", "workspace_id": WS, "node_id": "rn-doc-1",
                     "extension": "docx", "created_at": old_iso, "updated_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", RenameSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("99942000001", "新名字", "99170", gmt=now_ms, action_view="知识库重命名文件")
    summary = run_bridge(org)
    assert summary.get("direct_upserts") == 1
    with SessionLocal() as db:
        doc = db.get(Document, "rn-doc-1")
        assert doc.name == "新名字.docx" and doc.path == "/桥接测试库/新名字.docx"
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "rn-doc-1")).all() == []
        db.delete(doc)
        db.commit()


def test_walk_queue_continues_next_pass(env):
    """codex 第四轮 P1：巡走队列持久化——预算外的库在没有新事件的
    下一轮照样续走，成功才出队。"""
    from app.db import BridgeWalk

    settings, walks, _ = env
    with SessionLocal() as db:
        for index in range(6):
            db.add(BridgeWalk(workspace_id=f"queued-ws-{index}"))
        db.commit()
    s1 = run_bridge(settings)  # 没有任何事件，仅消费队列
    assert len(s1["walks"]) == 5 and s1.get("walks_deferred") == 1
    s2 = run_bridge(settings)
    assert len(s2["walks"]) == 1 and s2["walks"][0]["workspace_id"].startswith("queued-ws-")
    with SessionLocal() as db:
        assert db.scalars(select(BridgeWalk)).all() == []


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
                          review_scope="full_content", rule_version="V1.1",
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


def test_metadata_only_push_labeled_as_precheck():
    """历史 metadata_only 文案不得承诺自动补评；当前门禁不会再创建或
    推送这种实例，但渲染兼容仍须明确其已停用。"""
    from types import SimpleNamespace

    from app import notify as notify_module

    doc = SimpleNamespace(name="口径样例.docx", node_id="pre-1")
    partial = SimpleNamespace(ai_score=88.0, verdict="pass", review_scope="metadata_only",
                              rule_version="V1.1", findings=[])
    title, body = notify_module.build_message(doc, partial, "https://kg.example.com")
    assert "初检通过" in title and "已停用" in body and "不会自动补评" in body and "质量达标" not in body
    full = SimpleNamespace(ai_score=88.0, verdict="pass", review_scope="full_content",
                           rule_version="V1.1", findings=[])
    title_full, body_full = notify_module.build_message(doc, full)
    assert "评审通过" in title_full and "初检" not in body_full and "质量达标" in body_full
    low_partial = SimpleNamespace(ai_score=54.0, verdict="return", review_scope="metadata_only",
                                  rule_version="V1.1", findings=[{"message": "标题未标注版本号"}])
    title_low, body_low = notify_module.build_message(doc, low_partial)
    assert "初检低分说明" in title_low and "不会自动补评" in body_low
    digest_title, digest_body = notify_module.digest_message([
        {"name": "a.docx", "score": 88, "verdict": "pass", "scope": "metadata_only"},
        {"name": "b.docx", "score": 90, "verdict": "pass", "scope": "full_content"},
    ])
    assert "历史初检（已停用）" in digest_body and digest_body.count("历史初检（已停用）") == 1


def test_notify_department_allowlist():
    from types import SimpleNamespace

    from app import notify as notify_module

    low = SimpleNamespace(review_instance_id="ri-dept", ai_score=50.0, verdict="return",
                          review_scope="full_content", rule_version="V1.1", findings=[])
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
        db.flush()  # column default for status lands at flush time
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
                                  verdict="return", review_scope="full_content")
        settings = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": "other-ws"})
        row = enqueue_review_notification(db, settings, doc, instance)
        assert row.status == "skipped" and row.error_code == "workspace_not_allowlisted"
        settings_open = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": ""})
        row2 = enqueue_review_notification(db, settings_open, doc, instance)
        db.add(doc); db.add(instance); db.flush()  # column defaults apply at flush
        assert row2.status == "pending" and row2.target_union_id == "u1"
        db.rollback()


# ---------------------------------------------------------------------------
# 2026-08-14 动作白名单 + 软删除 + 合并窗 + 降噪矩阵边界（codex 点名用例）
# ---------------------------------------------------------------------------


class _RaisingSearchClient:
    """定位器被调用即失败——证明该类事件零远程消费。"""

    def __init__(self, _settings):
        pass

    async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
        raise AssertionError("locator must not be called for this action kind")

    async def batch_query_wiki_nodes(self, node_ids, operator_id):
        raise AssertionError("locator must not be called for this action kind")


def test_action_kind_whitelist_classification():
    ns = lambda view: SimpleNamespace(action_view=view)
    assert audit_bridge._action_kind(ns("知识库上传文件")) == "review"
    assert audit_bridge._action_kind(ns("创建副本")) == "review"
    assert audit_bridge._action_kind(ns("知识库修改文件")) == "modify"
    assert audit_bridge._action_kind(ns("编辑文档")) == "modify"
    assert audit_bridge._action_kind(ns("更新文件")) == "modify"
    assert audit_bridge._action_kind(ns("知识库重命名文件")) == "metadata"
    assert audit_bridge._action_kind(ns("移动文件")) == "metadata"
    assert audit_bridge._action_kind(ns("知识库删除文件")) == "delete"
    assert audit_bridge._action_kind(ns("移动到回收站")) == "delete"
    assert audit_bridge._action_kind(ns("从回收站恢复文件")) == "restore"  # 恢复优先于回收站
    assert audit_bridge._action_kind(ns("知识库分享文件")) == "ignore"
    assert audit_bridge._action_kind(ns("添加知识库协作成员")) == "ignore"
    assert audit_bridge._action_kind(ns("移除知识库成员")) == "ignore"    # 成员动作优先于"移除"
    # 白名单外一律 unknown——绝不默认评审（codex 第八轮 P0）
    assert audit_bridge._action_kind(ns("某未知写操作")) == "unknown"
    assert audit_bridge._action_kind(ns("")) == "unknown"
    # 评审触发类整名精确匹配（codex 第九轮 P0）：带宾语后缀的非正文操作
    # 不得因裸子串"修改/更新/编辑"进入评审
    assert audit_bridge._action_kind(ns("修改文档标题")) == "unknown"
    assert audit_bridge._action_kind(ns("更新知识库描述")) == "unknown"
    assert audit_bridge._action_kind(ns("修改")) == "unknown"
    assert audit_bridge._action_kind(ns("上传头像")) == "unknown"
    assert audit_bridge._action_kind(ns(" 知识库上传文件 ")) == "review"  # 前后空白归一
    assert audit_bridge._action_kind(ns("覆盖文件")) == "review"          # 生产真实动作名
    assert audit_bridge._is_creation_event(ns("覆盖文件")) is False       # 复用旧节点，用 updated_at 互证


def test_unknown_action_terminal_never_reviews(env, monkeypatch):
    """白名单外动作：终态 ignored_unknown_action，不评审、不置合并窗、
    零定位消费（codex 第八轮 P0：未知动作默认评审违反白名单原则）。"""
    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _RaisingSearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("tb-unk1", "桥接测试文档", "99280", action_view="知识库某新奇操作")
    summary = run_bridge(org)
    assert summary.get("unknown_actions") == 1 and summary["walks"] == [] and summary["unlocated"] == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-unk1"))
        assert event.processed is True and event.resolution == "ignored_unknown_action"
        doc = db.get(Document, "bridge-A")  # 同名镜像文档不受任何影响
        assert doc.review_due_at is None
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "bridge-A")).all() == []


def test_ignore_action_finishes_without_any_lookup(env, monkeypatch):
    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _RaisingSearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("tb-ign1", "被分享的文件", "99210", action_view="知识库分享文件")
    summary = run_bridge(org)
    assert summary.get("ignored") == 1 and summary["walks"] == [] and summary["unlocated"] == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-ign1"))
        assert event.processed is True and event.resolution == "ignored_action"
        assert event.last_attempt_at is None  # 批处理阶段直接终态，从未进定位队列


def test_delete_event_soft_deletes_cancels_and_keeps_history(env):
    from app.db import Notification
    from app.db import utcnow as db_utcnow

    settings, walks, _ = env
    with SessionLocal() as db:
        db.merge(Document(node_id="del-doc-1", workspace_id=WS, name="要删的文件.docx", extension="docx",
                          file_class="document", storage_dentry_id="99959000001",
                          source_created_at=gmt_iso(), source_updated_at=gmt_iso()))
        db.add(ReviewJob(job_id="job-del-1", node_id="del-doc-1", trigger="audit", status="pending"))
        db.add(ReviewInstance(review_instance_id="ri-del-keep", node_id="del-doc-1", ai_score=77,
                              verdict="pass", review_scope="full_content"))
        db.add(Notification(node_id="del-doc-1", review_instance_id="ri-del-keep",
                            target_union_id="u-del", title="旧通知", body="正文", status="pending"))
        db.commit()
    add_event("99959000001", "要删的文件", "99220", action_view="知识库删除文件")
    summary = run_bridge(settings)
    assert summary.get("deleted") == 1
    with SessionLocal() as db:
        doc = db.get(Document, "del-doc-1")
        assert doc.is_deleted is True and doc.deleted_at is not None
        job = db.get(ReviewJob, "job-del-1")
        assert job.status == "skipped" and job.error_code == "document_deleted"
        note = db.scalar(select(Notification).where(Notification.node_id == "del-doc-1"))
        assert note.status == "skipped" and note.error_code == "skipped_document_deleted"
        assert db.get(ReviewInstance, "ri-del-keep") is not None  # 评审历史全量保留
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99959000001"))
        assert event.processed is True and event.resolution == "deleted"
        db.delete(job); db.delete(note)
        db.query(ReviewInstance).filter(ReviewInstance.node_id == "del-doc-1").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_delete_never_matches_by_name_and_never_calls_locator(env, monkeypatch):
    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _RaisingSearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    with SessionLocal() as db:
        db.merge(Document(node_id="same-name-del", workspace_id=WS, name="同名勿删.docx", extension="docx",
                          file_class="document", storage_dentry_id="",
                          source_created_at=gmt_iso(), source_updated_at=gmt_iso()))
        db.commit()
    add_event("tb-del-un", "同名勿删", "99230", action_view="知识库删除文件")
    summary = run_bridge(org)
    assert summary["walks"] == [] and summary.get("deleted", 0) == 0
    with SessionLocal() as db:
        assert db.get(Document, "same-name-del").is_deleted is False  # 绝不按同名删除
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-del-un"))
        assert event.processed is False  # 保持 pending 至 48h 死信；月度核对兜底真实删除
        db.query(Document).filter(Document.node_id == "same-name-del").delete(synchronize_session=False)
        db.commit()


def test_restore_event_undeletes_without_review(env):
    from datetime import datetime as dt, timezone as tz

    settings, walks, _ = env
    with SessionLocal() as db:
        db.merge(Document(node_id="rest-doc-1", workspace_id=WS, name="被恢复的文件.docx", extension="docx",
                          file_class="document", storage_dentry_id="99960000001", is_deleted=True,
                          deleted_at=dt.now(tz.utc), watch_misses=2,
                          source_created_at=gmt_iso(), source_updated_at=gmt_iso()))
        db.commit()
    add_event("99960000001", "被恢复的文件", "99240", action_view="知识库恢复文件")
    summary = run_bridge(settings)
    assert summary.get("restored") == 1
    with SessionLocal() as db:
        doc = db.get(Document, "rest-doc-1")
        assert doc.is_deleted is False and doc.deleted_at is None and doc.watch_misses == 0
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "rest-doc-1")).all() == []  # 恢复不评审
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99960000001"))
        assert event.processed is True and event.resolution == "restored"
        db.delete(doc)
        db.commit()


def test_modify_event_merges_into_window_even_same_timestamp(env, monkeypatch):
    """正文修改不立即评审：置合并窗（同时间戳的覆盖保存也置脏——codex P0-2
    点名：不依赖 updated_at 变化判断）；后续修改顺延窗口；到点由收割器
    入队一次 trigger=modify_merged。"""
    import time as time_module
    from datetime import timedelta as td

    from app import service

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)
    same_iso = gmt_iso(now_ms)  # 载荷 updated_at 与镜像完全一致：时间戳没变

    class ModifySearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "mod-doc-1", "name": "被修改的文件.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "被修改的文件.docx", "workspace_id": WS, "node_id": "mod-doc-1",
                     "extension": "docx", "updated_at": same_iso}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", ModifySearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    with SessionLocal() as db:
        db.merge(Document(node_id="mod-doc-1", workspace_id=WS, name="被修改的文件.docx", extension="docx",
                          file_class="document", storage_dentry_id="99961000001",
                          source_created_at="2026-08-01T09:00:00+00:00", source_updated_at=same_iso,
                          review_due_at=None, dirty_since=None))
        db.commit()
    add_event("99961000001", "被修改的文件", "99250", gmt=now_ms, action_view="知识库修改文件")
    run_bridge(org)
    with SessionLocal() as db:
        doc = db.get(Document, "mod-doc-1")
        assert doc.review_due_at is not None and doc.dirty_since is not None  # 同时间戳仍置脏
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "mod-doc-1")).all() == []
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "99961000001"))
        assert event.processed is True and event.resolution == "done"
        due_first, dirty_first = doc.review_due_at, doc.dirty_since
    add_event("99961000002", "被修改的文件", "99250", gmt=now_ms + 60000, action_view="知识库修改文件")
    run_bridge(org)
    with SessionLocal() as db:
        doc = db.get(Document, "mod-doc-1")
        assert doc.review_due_at > due_first          # 后续修改顺延窗口
        assert doc.dirty_since == dirty_first          # 首次置脏时间不动（6h 封顶基准）
        # 到点收割：把窗口拨到过去，收割器应入队一次合并评审
        doc.review_due_at = doc.review_due_at - td(hours=2)
        db.commit()
        harvested = service.harvest_due_reviews(db, get_settings())
        assert harvested >= 1
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "mod-doc-1")).all()
        assert [job.trigger for job in jobs] == ["modify_merged"]
        doc = db.get(Document, "mod-doc-1")
        assert doc.review_due_at is None and doc.dirty_since is None
        for job in jobs:
            db.delete(job)
        db.delete(doc)
        db.commit()


def test_confirmed_non_digit_biz_becomes_dead_letter_not_done(env, monkeypatch):
    """codex P0-3：非数字 bizId 挂不上下载键——即便 locator 确认并直建了
    文档，也必须转 dead_letter_no_numeric_biz_id，绝不伪装 done。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class NonDigitSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "nd-doc-1", "name": "非数字键文件.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "非数字键文件.docx", "workspace_id": WS, "node_id": "nd-doc-1",
                     "extension": "docx", "created_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", NonDigitSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    add_event("tb-nd1", "非数字键文件", "99260", gmt=now_ms)
    summary = run_bridge(org)
    assert summary["confirmed"] == 1 and summary.get("dead_letter") == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-nd1"))
        assert event.processed is True and event.resolution == "dead_letter_no_numeric_biz_id"
        doc = db.get(Document, "nd-doc-1")
        assert doc is not None and doc.storage_dentry_id == ""  # 镜像照建，键决不造假
        db.query(ReviewJob).filter(ReviewJob.node_id == "nd-doc-1").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def test_native_adoc_uses_audit_extension_fallback_and_enters_review(env, monkeypatch):
    """Native DingTalk documents are body-ready by node id: a non-numeric audit
    bizId must not turn a valid .adoc event into a storage-key dead letter. The
    audit extension also covers an occasionally sparse batchQuery payload."""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class NativeDocSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            assert space_ids is None  # native documents are not in the shared-storage scoped index
            return [{"dentry_uuid": "adoc-native-1", "name": "在线方案.adoc"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "在线方案.adoc", "workspace_id": WS, "node_id": "adoc-native-1",
                     "creator_id": "native-author", "created_at": gmt_iso(now_ms)}]  # extension intentionally absent

    monkeypatch.setattr(audit_bridge, "DingtalkClient", NativeDocSearch)
    org = settings.model_copy(update={"bridge_locator_enabled": True, "dingtalk_sync_operator_id": "op",
                                      "wiki_storage_space_id": "", "bridge_sweep_max_governed": 0})
    add_event("tb-native-adoc-1", "在线方案", "99265", extension="adoc", gmt=now_ms,
              action_view="创建文档", operator="native-author")
    summary = run_bridge(org)
    assert summary["confirmed"] == 1 and summary.get("dead_letter", 0) == 0
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-native-adoc-1"))
        assert event.processed is True and event.resolution == "done"
        doc = db.get(Document, "adoc-native-1")
        assert doc is not None and doc.file_class == "native_doc" and doc.storage_dentry_id == ""
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "adoc-native-1")).all()
        assert [job.trigger for job in jobs] == ["audit"]
        for job in jobs:
            db.delete(job)
        db.delete(doc)
        db.commit()


def test_native_adoc_creation_rejects_same_time_different_creator(env, monkeypatch):
    """A same-name native document owned by another person must stay pending.

    The native locator intentionally searches globally because adoc nodes do
    not appear in the shared storage scope.  The creator check keeps that
    broader search from binding an event to a colleague's concurrent document.
    """
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class WrongCreatorSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            assert space_ids is None
            return [{"dentry_uuid": "adoc-native-wrong-owner", "name": "同名在线文档"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "同名在线文档", "workspace_id": WS,
                     "node_id": "adoc-native-wrong-owner", "extension": "adoc",
                     "creator_id": "another-author", "created_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", WrongCreatorSearch)
    org = settings.model_copy(update={"bridge_locator_enabled": True, "dingtalk_sync_operator_id": "op",
                                      "wiki_storage_space_id": "", "bridge_sweep_max_governed": 0})
    add_event("tb-native-adoc-wrong-owner", "同名在线文档", "99266", extension="adoc", gmt=now_ms,
              action_view="创建文档", operator="event-author")
    summary = run_bridge(org)
    assert summary["confirmed"] == 0 and summary["uncorroborated"] == 1
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-native-adoc-wrong-owner"))
        assert event.processed is False and event.matched_node_id == ""
        assert db.get(Document, "adoc-native-wrong-owner") is None


def test_native_adoc_locator_is_prioritized_over_attachment_backlog(env, monkeypatch):
    """A new online document must not wait behind the 20-item file locator cap."""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class PrioritySearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            if keyword == "优先在线文档":
                assert space_ids is None
                return [{"dentry_uuid": "adoc-native-priority", "name": "优先在线文档"}]
            return []

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "优先在线文档", "workspace_id": WS,
                     "node_id": "adoc-native-priority", "extension": "adoc",
                     "creator_id": "priority-author", "created_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", PrioritySearch)
    org = settings.model_copy(update={"bridge_locator_enabled": True, "dingtalk_sync_operator_id": "op",
                                      "wiki_storage_space_id": "", "bridge_sweep_max_governed": 0})
    # These are newer but cannot be located without the shared storage config.
    # The native document must nevertheless get a locator slot this cycle.
    for index in range(audit_bridge.WIKI_LOCATE_BUDGET + 5):
        add_event(f"tb-native-priority-file-{index}", f"附件{index}.docx", "99267", extension="docx",
                  gmt=now_ms + index + 1)
    add_event("tb-native-priority", "优先在线文档", "99267", extension="adoc", gmt=now_ms,
              action_view="创建文档", operator="priority-author")
    run_bridge(org)
    with SessionLocal() as db:
        event = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == "tb-native-priority"))
        assert event.processed is True and event.match_status == "confirmed"
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "adoc-native-priority")).all()
        assert [job.trigger for job in jobs] == ["audit"]
        for job in jobs:
            db.delete(job)
        db.delete(db.get(Document, "adoc-native-priority"))
        db.commit()


def test_modify_event_never_overwrites_uploader(env, monkeypatch):
    """归属保护（codex feb567a P1）：修改人是操作者不是上传人——已有文档的
    后续事件只记 last_modifier_key，上传人与部门归属保持不变。"""
    import time as time_module

    settings, walks, _ = env
    now_ms = int(time_module.time() * 1000)

    class TouchSearch:
        def __init__(self, _settings):
            pass

        async def search_dentries(self, keyword, operator_id, space_ids=None, max_results=20):
            return [{"dentry_uuid": "own-doc-1", "name": "归属保护.docx"}]

        async def batch_query_wiki_nodes(self, node_ids, operator_id):
            return [{"name": "归属保护.docx", "workspace_id": WS, "node_id": "own-doc-1",
                     "extension": "docx", "creator_id": "payload-creator", "updated_at": gmt_iso(now_ms)}]

    monkeypatch.setattr(audit_bridge, "DingtalkClient", TouchSearch)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    with SessionLocal() as db:
        db.merge(Document(node_id="own-doc-1", workspace_id=WS, name="归属保护.docx", extension="docx",
                          file_class="document", storage_dentry_id="99962000001",
                          uploader_key="creator-A", uploader_name="张三", department_name="数字化转型部",
                          org_matched=True, source_created_at="2026-08-01T09:00:00+00:00",
                          source_updated_at="2026-08-01T09:00:00+00:00"))
        db.commit()
    add_event("99962000001", "归属保护", "99270", gmt=now_ms,
              action_view="知识库修改文件", operator="editor-B")
    run_bridge(org)
    with SessionLocal() as db:
        doc = db.get(Document, "own-doc-1")
        assert doc.uploader_key == "creator-A" and doc.uploader_name == "张三"
        assert doc.department_name == "数字化转型部"          # 归属与统计口径不被修改人污染
        assert doc.last_modifier_key == "editor-B"            # 操作者另行记录
        db.query(ReviewJob).filter(ReviewJob.node_id == "own-doc-1").delete(synchronize_session=False)
        db.delete(doc)
        db.commit()


def _mk_instance(db, node_id, ri, score, verdict, when):
    row = ReviewInstance(review_instance_id=ri, node_id=node_id, ai_score=score,
                         verdict=verdict, review_scope="full_content")
    db.add(row)
    db.flush()
    row.created_at = when
    db.flush()
    return row


def _matrix_doc(db, node_id):
    from app.db import Notification

    for model, field in ((ReviewJob, ReviewJob.node_id), (ReviewInstance, ReviewInstance.node_id),
                         (Notification, Notification.node_id)):
        db.query(model).filter(field == node_id).delete(synchronize_session=False)
    db.merge(Document(node_id=node_id, workspace_id=WS, name=f"{node_id}.docx", extension="docx",
                      file_class="document", uploader_key="u-matrix", department_name="AI应用研发部"))
    db.commit()
    return db.get(Document, node_id)


def _matrix_settings():
    return get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": "",
                                             "notify_departments": "", "notify_on_pass": True})


def _cn_day_start_naive():
    from datetime import datetime, timezone as tz
    from app import notify as notify_module
    return (datetime.now(notify_module.CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(tz.utc).replace(tzinfo=None))


def test_renotify_matrix_flip_notifies_minor_silences_capped(env):
    """降噪矩阵（2026-08-14 拍板）：结论翻转必通知；同结论 |Δ|<10 留痕静默；
    降幅 ≥10 想通知但撞每日 1 条重评上限时留痕跳过。"""
    from datetime import timedelta as td

    from app import notify as notify_module

    settings = _matrix_settings()
    day0 = _cn_day_start_naive()
    yesterday = day0 - td(hours=3)
    with SessionLocal() as db:
        doc = _matrix_doc(db, "matrix-1")
        inst1 = _mk_instance(db, "matrix-1", "ri-m1-a", 85, "pass", yesterday)
        row1 = notify_module.enqueue_review_notification(db, settings, doc, inst1)
        db.flush()
        assert row1.status == "pending"  # 首评照旧
        row1.status, row1.created_at = "sent", yesterday
        db.flush()
        inst2 = _mk_instance(db, "matrix-1", "ri-m1-b", 55, "return", day0 + td(minutes=5))
        row2 = notify_module.enqueue_review_notification(db, settings, doc, inst2)
        db.flush()
        assert row2.status == "pending" and "低分说明" in row2.title  # 通过→低分必通知
        row2.status = "sent"
        db.flush()
        inst3 = _mk_instance(db, "matrix-1", "ri-m1-c", 48, "return", day0 + td(minutes=15))
        row3 = notify_module.enqueue_review_notification(db, settings, doc, inst3)
        db.flush()
        assert row3.status == "skipped" and row3.error_code == "suppressed_minor_change"
        inst4 = _mk_instance(db, "matrix-1", "ri-m1-d", 30, "return", day0 + td(minutes=25))
        row4 = notify_module.enqueue_review_notification(db, settings, doc, inst4)
        db.flush()  # 降 18 分本应提醒，但当日重评额度（1 条）已被 row2 用掉
        assert row4.status == "skipped" and row4.error_code == "suppressed_daily_cap"
        _matrix_doc(db, "matrix-1")
        db.query(Document).filter(Document.node_id == "matrix-1").delete(synchronize_session=False)
        db.commit()


def test_renotify_improved_bypasses_pass_gate(env):
    from datetime import timedelta as td

    from app import notify as notify_module

    settings = _matrix_settings().model_copy(update={"notify_on_pass": False})
    day0 = _cn_day_start_naive()
    with SessionLocal() as db:
        doc = _matrix_doc(db, "matrix-2")
        _mk_instance(db, "matrix-2", "ri-m2-a", 55, "return", day0 - td(hours=5))
        inst2 = _mk_instance(db, "matrix-2", "ri-m2-b", 88, "pass", day0 + td(minutes=10))
        row = notify_module.enqueue_review_notification(db, settings, doc, inst2)
        db.flush()
        # 低分→通过：改善反馈必达，不受 KG_NOTIFY_ON_PASS 限制
        assert row.status == "pending" and "已改善" in row.title
        assert "55 分 →" in row.body and "达标" in row.body
        _matrix_doc(db, "matrix-2")
        db.query(Document).filter(Document.node_id == "matrix-2").delete(synchronize_session=False)
        db.commit()


def test_renotify_drop_warning_keeps_pass_wording(env):
    from datetime import timedelta as td

    from app import notify as notify_module

    settings = _matrix_settings()
    day0 = _cn_day_start_naive()
    with SessionLocal() as db:
        doc = _matrix_doc(db, "matrix-3")
        _mk_instance(db, "matrix-3", "ri-m3-a", 95, "pass", day0 - td(hours=5))
        inst2 = _mk_instance(db, "matrix-3", "ri-m3-b", 80, "pass", day0 + td(minutes=10))
        row = notify_module.enqueue_review_notification(db, settings, doc, inst2)
        db.flush()
        assert row.status == "pending" and "评分下降提醒" in row.title
        assert "较上次评审下降" in row.body and "结论仍为通过" in row.body
        _matrix_doc(db, "matrix-3")
        db.query(Document).filter(Document.node_id == "matrix-3").delete(synchronize_session=False)
        db.commit()
