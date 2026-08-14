import os

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import audit_bridge
from app.config import get_settings
from app.db import Document, FileAuditEvent, ReviewJob, SessionLocal, SpaceMap, Workspace, init_db
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
        from app.db import BridgeWalk
        db.query(BridgeWalk).delete(synchronize_session=False)
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("tb-%")).delete(synchronize_session=False)
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.like("999%")).delete(synchronize_session=False)
        db.query(SpaceMap).filter(SpaceMap.space_id.like("990%")).delete(synchronize_session=False)
        # 上一次失败运行可能留下测试文档（各测试的收尾清理没跑到），开场兜底
        stale_ids = ["bridge-late-doc", "old-same-2", "old-node-same", "dup-a", "dup-b", "stock-doc-1",
                     "late-cp-doc"]
        stale_ids += [row[0] for row in db.execute(select(Document.node_id)
                                                   .where(Document.node_id.like("cp-doc-%")))]
        for node_id in stale_ids:
            leftover = db.get(Document, node_id)
            if leftover:
                db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
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
              module_view="团队空间", extension="docx", gmt=1786400000000, received=None):
    with SessionLocal() as db:
        kwargs = dict(biz_id=biz_id, gmt_create=gmt, action_view=action_view,
                      module_view=module_view, resource=resource, extension=extension,
                      target_space_id=space_id)
        if received is not None:
            kwargs["received_at"] = received  # 重试窗口基准（默认=入库当下）
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
        assert [job.trigger for job in jobs] == ["content_key"]
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


def test_same_workspace_double_enqueue_no_conflict(env, monkeypatch):
    """codex 第五轮 P0：同一库连续多条事件同一事务内排队巡走，不得撞
    bridge_walk_queue 主键。"""
    import time as time_module

    settings, walks, _ = env
    monkeypatch.setattr(audit_bridge, "DingtalkClient", _EmptySearchClient)
    org = locator_settings(settings).model_copy(update={"bridge_sweep_max_governed": 0})
    with SessionLocal() as db:
        db.merge(Document(node_id="dup-a", workspace_id=WS, name="同库文件A.docx",
                          extension="docx", file_class="document"))
        db.merge(Document(node_id="dup-b", workspace_id=WS, name="同库文件B.docx",
                          extension="docx", file_class="document"))
        db.commit()
    now_ms = int(time_module.time() * 1000)
    add_event("99960000001", "同库文件A", "99070", gmt=now_ms)
    add_event("99960000002", "同库文件B", "99070", gmt=now_ms + 1)
    summary = run_bridge(org)  # 修复前：IntegrityError 使整轮回滚
    assert any(walk["workspace_id"] == WS for walk in summary["walks"])
    with SessionLocal() as db:
        for node_id in ("dup-a", "dup-b"):
            doc = db.get(Document, node_id)
            if doc:
                db.delete(doc)
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


def test_metadata_only_push_labeled_as_precheck():
    """codex 2026-08-14：metadata_only 不得自称"评审通过/质量达标"——
    称"初检"并注明正文评审待补做，汇总行带初检标记。"""
    from types import SimpleNamespace

    from app import notify as notify_module

    doc = SimpleNamespace(name="口径样例.docx", node_id="pre-1")
    partial = SimpleNamespace(ai_score=88.0, verdict="pass", review_scope="metadata_only",
                              rule_version="V1.1", findings=[])
    title, body = notify_module.build_message(doc, partial, "https://kg.example.com")
    assert "初检通过" in title and "自动补做" in body and "质量达标" not in body
    full = SimpleNamespace(ai_score=88.0, verdict="pass", review_scope="full_content",
                           rule_version="V1.1", findings=[])
    title_full, body_full = notify_module.build_message(doc, full)
    assert "评审通过" in title_full and "初检" not in body_full and "质量达标" in body_full
    low_partial = SimpleNamespace(ai_score=54.0, verdict="return", review_scope="metadata_only",
                                  rule_version="V1.1", findings=[{"message": "标题未标注版本号"}])
    title_low, body_low = notify_module.build_message(doc, low_partial)
    assert "初检低分说明" in title_low and "正文评审将自动补做" in body_low
    digest_title, digest_body = notify_module.digest_message([
        {"name": "a.docx", "score": 88, "verdict": "pass", "scope": "metadata_only"},
        {"name": "b.docx", "score": 90, "verdict": "pass", "scope": "full_content"},
    ])
    assert "· 初检" in digest_body and digest_body.count("· 初检") == 1


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
                                  verdict="return", review_scope="metadata_only")
        settings = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": "other-ws"})
        row = enqueue_review_notification(db, settings, doc, instance)
        assert row.status == "skipped" and row.error_code == "workspace_not_allowlisted"
        settings_open = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": ""})
        row2 = enqueue_review_notification(db, settings_open, doc, instance)
        db.add(doc); db.add(instance); db.flush()  # column defaults apply at flush
        assert row2.status == "pending" and row2.target_union_id == "u1"
        db.rollback()
