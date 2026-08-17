"""2026-08-13 运维加固回归：切片轮转、数字员工闸、统计口径、僵尸清理、正文原因。"""
import os
from datetime import timedelta

from fastapi.testclient import TestClient

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from sqlalchemy import func, select

from app.main import app
from app.config import get_settings
from app.db import Document, ReviewInstance, ReviewJob, SessionLocal, SyncRun, Workspace, init_db, utcnow


def _ensure_demo_workspace(db) -> None:
    if not db.get(Workspace, "demo-workspace"):
        db.add(Workspace(workspace_id="demo-workspace", name="演示库"))
        db.commit()


def test_watch_slice_rotation(monkeypatch):
    from types import SimpleNamespace

    from app import service

    service._watch_rotation["queue"] = []
    targets = [{"workspace_id": f"ws-{i}", "name": f"库{i}", "space": {}} for i in range(5)]
    walked_log = []

    async def fake_resolve(settings, force=False):
        return {"resolved": targets, "unresolved": []}

    async def fake_walk(db, settings, ws_id, space, mode="watch"):
        walked_log.append(ws_id)
        return SimpleNamespace(run_id="r-" + ws_id, mode="watch", status="succeeded",
                               documents_seen=1, documents_new=0, documents_changed=0, error_code="")

    monkeypatch.setattr(service, "resolve_watch_targets", fake_resolve)
    monkeypatch.setattr(service, "watch_workspace", fake_walk)
    settings = get_settings()
    s1 = service.run_watch_slice(None, settings, batch=2)
    assert [w["workspace_id"] for w in s1["walked"]] == ["ws-0", "ws-1"]
    assert not s1["cycle_completed"] and s1["remaining"] == 3
    service.run_watch_slice(None, settings, batch=2)
    s3 = service.run_watch_slice(None, settings, batch=2)
    assert s3["cycle_completed"] and s3["remaining"] == 0
    assert walked_log == [f"ws-{i}" for i in range(5)]
    s4 = service.run_watch_slice(None, settings, batch=2)  # 新一轮自动补队列
    assert [w["workspace_id"] for w in s4["walked"]] == ["ws-0", "ws-1"]
    service._watch_rotation["queue"] = []


def test_robot_uploader_detection_and_job_skip():
    from app.service import is_robot_uploader, process_next_job

    settings = get_settings()
    assert is_robot_uploader(settings, "数字员工")        # 姓名前缀默认命中
    assert is_robot_uploader(settings, "", "数字员工4")
    assert not is_robot_uploader(settings, "陈鹏列")
    assert is_robot_uploader(settings.model_copy(update={"robot_user_ids": "robot-001"}), "robot-001")

    init_db()
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        db.merge(Document(node_id="robot-doc-1", workspace_id="demo-workspace", name="禅道同步件.docx",
                          extension="docx", uploader_key="u-robot", uploader_name="数字员工",
                          department_name="AI应用研发部", org_matched=True))
        db.merge(ReviewJob(job_id="job-robot-1", node_id="robot-doc-1", trigger="watch"))
        db.commit()
        # 队列里可能有其他测试排入的人类任务，逐个泵直到机器人任务被处理
        for _ in range(10):
            db.expire_all()
            if db.get(ReviewJob, "job-robot-1").status != "pending":
                break
            if not process_next_job(db, settings):
                break
        job = db.get(ReviewJob, "job-robot-1")
        assert job.status == "skipped" and job.error_code == "robot_uploader"
        assert (db.scalar(select(func.count()).select_from(ReviewInstance)
                          .where(ReviewInstance.node_id == "robot-doc-1")) or 0) == 0
        db.delete(job)
        db.delete(db.get(Document, "robot-doc-1"))
        db.commit()


def test_dashboard_average_uses_latest_instance_per_doc():
    with TestClient(app) as client:
        with SessionLocal() as db:
            _ensure_demo_workspace(db)
            db.merge(Document(node_id="avg-doc", workspace_id="demo-workspace", name="均值口径.docx", extension="docx"))
            db.merge(ReviewInstance(review_instance_id="avg-old", node_id="avg-doc", ai_score=0,
                                    verdict="return", review_scope="metadata_only"))
            db.merge(ReviewInstance(review_instance_id="avg-new", node_id="avg-doc", ai_score=100,
                                    verdict="pass", review_scope="metadata_only"))
            db.commit()
            old = db.get(ReviewInstance, "avg-old")
            old.created_at = utcnow() - timedelta(days=1)
            db.commit()
            latest = {}
            for item in db.scalars(select(ReviewInstance).order_by(ReviewInstance.created_at.desc())).all():
                latest.setdefault(item.node_id, item)
            expected = round(sum(x.ai_score for x in latest.values()) / len(latest), 1)
            all_rows = db.scalars(select(ReviewInstance)).all()
            all_mean = round(sum(x.ai_score for x in all_rows) / len(all_rows), 1)
        got = client.get("/api/v1/dashboard/overview").json()["metrics"]["average_ai_score"]
        assert got == expected
        if all_mean != expected:  # 旧 0 分被排除才会出现的差异
            assert got != all_mean
        with SessionLocal() as db:
            for pk in ("avg-old", "avg-new"):
                row = db.get(ReviewInstance, pk)
                if row:
                    db.delete(row)
            doc = db.get(Document, "avg-doc")
            if doc:
                db.delete(doc)
            db.commit()


def test_sweep_stale_runs():
    from app.db import BridgeWalk, ReviewJob
    from app.service import sweep_stale_runs

    init_db()
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        db.merge(SyncRun(run_id="stale-1", status="running", mode="watch", workspace_id="ws-z"))
        db.merge(BridgeWalk(workspace_id="stale-walk-ws"))  # 跨重启残留的巡走队列行
        db.query(ReviewJob).filter(ReviewJob.job_id == "stale-review-1").delete(synchronize_session=False)
        db.query(Document).filter(Document.node_id == "stale-review-doc").delete(synchronize_session=False)
        db.add(Document(node_id="stale-review-doc", workspace_id="demo-workspace",
                        name="被打断的评审.docx", extension="docx", file_class="document"))
        db.add(ReviewJob(job_id="stale-review-1", node_id="stale-review-doc",
                         trigger="audit", status="running"))
        db.commit()
        assert sweep_stale_runs(db) >= 2
        row = db.get(SyncRun, "stale-1")
        assert row.status == "failed" and row.error_code == "interrupted_by_restart" and row.finished_at
        from sqlalchemy import select as sa_select
        assert db.scalars(sa_select(BridgeWalk)).all() == []  # 启动清空，避免反复 404 烧预算
        # codex 第九轮 P0：被重启打断的模型评审重新排队（指纹去重防重复出分），
        # 不许永久卡 running 拖死合并窗
        job = db.get(ReviewJob, "stale-review-1")
        assert job.status == "pending"
        db.delete(row); db.delete(job)
        db.query(Document).filter(Document.node_id == "stale-review-doc").delete(synchronize_session=False)
        db.commit()


def test_harvest_due_reviews_window_and_cap():
    """合并窗收割：到期收割、6 小时封顶强制收割、未到期不动；收割后窗口
    字段清零，任务 trigger=modify_merged。"""
    from datetime import timedelta

    from app.db import ReviewJob, utcnow
    from app.service import harvest_due_reviews

    init_db()
    now = utcnow().replace(tzinfo=None)
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        for node_id in ("harv-1", "harv-2", "harv-3"):
            db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
            db.query(ReviewInstance).filter(ReviewInstance.node_id == node_id).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id == node_id).delete(synchronize_session=False)
        db.add(Document(node_id="harv-1", workspace_id="demo-workspace", name="到期收割.docx",
                        extension="docx", file_class="document",
                        review_due_at=now - timedelta(minutes=5), dirty_since=now - timedelta(minutes=35)))
        db.add(Document(node_id="harv-2", workspace_id="demo-workspace", name="持续编辑封顶.docx",
                        extension="docx", file_class="document",
                        review_due_at=now + timedelta(minutes=25), dirty_since=now - timedelta(hours=7)))
        db.add(Document(node_id="harv-3", workspace_id="demo-workspace", name="窗口未到.docx",
                        extension="docx", file_class="document",
                        review_due_at=now + timedelta(minutes=25), dirty_since=now - timedelta(minutes=5)))
        db.commit()
        harvested = harvest_due_reviews(db, get_settings())
        assert harvested >= 2
        jobs1 = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "harv-1")).all()
        jobs2 = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "harv-2")).all()
        assert [j.trigger for j in jobs1] == ["modify_merged"]   # 30 分钟无修改 → 收割
        assert [j.trigger for j in jobs2] == ["modify_merged"]   # 持续编辑 6 小时封顶 → 强制收割
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "harv-3")).all() == []
        one = db.get(Document, "harv-1")
        assert one.review_due_at is None and one.dirty_since is None
        three = db.get(Document, "harv-3")
        assert three.review_due_at is not None  # 未到期保持等待
        for node_id in ("harv-1", "harv-2", "harv-3"):
            db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id == node_id).delete(synchronize_session=False)
        db.commit()


def test_harvest_due_docs_never_starved_by_undue_rows(monkeypatch):
    """codex 第八轮 P0 回归：到期筛选在 SQL 层——批量额度再小、未到期行
    再多，已到期的文档也必须被收割到。"""
    from datetime import timedelta

    from app import service
    from app.db import ReviewJob, utcnow

    init_db()
    monkeypatch.setattr(service, "HARVEST_BATCH", 2)  # 额度 2 < 未到期行数 3
    now = utcnow().replace(tzinfo=None)
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        ids = ["hstarve-a", "hstarve-b", "hstarve-c", "hstarve-due"]
        for node_id in ids:
            db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id == node_id).delete(synchronize_session=False)
        for node_id in ("hstarve-a", "hstarve-b", "hstarve-c"):
            db.add(Document(node_id=node_id, workspace_id="demo-workspace", name=f"{node_id}.docx",
                            extension="docx", file_class="document",
                            review_due_at=now + timedelta(minutes=20), dirty_since=now - timedelta(minutes=10)))
        # 到期时间放到两天前：按最早到期排序必然进入本轮额度
        db.add(Document(node_id="hstarve-due", workspace_id="demo-workspace", name="唯一到期.docx",
                        extension="docx", file_class="document",
                        review_due_at=now - timedelta(days=2), dirty_since=now - timedelta(days=2)))
        db.commit()
        assert service.harvest_due_reviews(db, get_settings()) >= 1
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "hstarve-due")).all()
        assert [j.trigger for j in jobs] == ["modify_merged"]
        for node_id in ("hstarve-a", "hstarve-b", "hstarve-c"):
            undue = db.get(Document, node_id)
            assert undue.review_due_at is not None  # 未到期行不被 SQL 筛选捞走
        for node_id in ids:
            db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id == node_id).delete(synchronize_session=False)
        db.commit()


def test_harvest_keeps_window_while_job_running():
    """codex 第八轮 P0 回归：撞上 running 任务时保留到期标记（正文可能已被
    抓走），任务完成后下一轮收割补评；绝不清了标记又不建任务。"""
    from datetime import timedelta

    from app.db import ReviewJob, utcnow
    from app.service import harvest_due_reviews

    init_db()
    now = utcnow().replace(tzinfo=None)
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        for node_id in ("hrun-1",):
            db.query(ReviewJob).filter(ReviewJob.node_id == node_id).delete(synchronize_session=False)
            db.query(Document).filter(Document.node_id == node_id).delete(synchronize_session=False)
        db.add(Document(node_id="hrun-1", workspace_id="demo-workspace", name="在途任务.docx",
                        extension="docx", file_class="document",
                        review_due_at=now - timedelta(days=2), dirty_since=now - timedelta(days=2)))
        db.add(ReviewJob(job_id="hrun-job-1", node_id="hrun-1", trigger="audit", status="running"))
        db.commit()
        harvest_due_reviews(db, get_settings())
        doc = db.get(Document, "hrun-1")
        assert doc.review_due_at is not None and doc.dirty_since is not None  # 窗口保留
        assert db.scalars(select(ReviewJob).where(ReviewJob.node_id == "hrun-1",
                                                  ReviewJob.trigger == "modify_merged")).all() == []
        job = db.get(ReviewJob, "hrun-job-1")
        job.status = "succeeded"
        db.commit()
        harvest_due_reviews(db, get_settings())   # 任务结束后补评
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.node_id == "hrun-1",
                                                  ReviewJob.trigger == "modify_merged")).all()
        assert len(jobs) == 1
        doc = db.get(Document, "hrun-1")
        assert doc.review_due_at is None and doc.dirty_since is None
        db.query(ReviewJob).filter(ReviewJob.node_id == "hrun-1").delete(synchronize_session=False)
        db.query(Document).filter(Document.node_id == "hrun-1").delete(synchronize_session=False)
        db.commit()


def test_inactive_workspace_baseline_hidden_from_search_and_metrics():
    """codex 第九轮 P1：不可见库的**基线**文件同样退出当前检索与总览统计
    （此前只滤了实时臂）；恢复可见即回归。"""
    from app import metrics as metrics_module
    from app.db import HistoricalFileNode, HistoricalSnapshot

    init_db()
    with SessionLocal() as db:
        primary = metrics_module.primary_snapshot_id(db)
        if not primary:
            db.add(HistoricalSnapshot(snapshot_id="wiki-baseline-2026-08-05"))
            db.commit()
            primary = metrics_module.primary_snapshot_id(db)
        files_snap = metrics_module.uploader_snapshot_id(db) or primary
        db.query(HistoricalFileNode).filter(
            HistoricalFileNode.node_id.in_(("inact-base-1", "inact-base-2"))).delete(synchronize_session=False)
        db.merge(Workspace(workspace_id="inactive-base-ws", name="X-基线不可见库",
                           watch_seeded=True, is_active=False))
        db.add(HistoricalFileNode(snapshot_id=primary, workspace_id="inactive-base-ws",
                                  node_id="inact-base-1", name="基线残留文件.docx", extension="docx",
                                  node_type="file", source_created_at="2026-05-01T09:00:00"))
        if files_snap != primary:
            db.add(HistoricalFileNode(snapshot_id=files_snap, workspace_id="inactive-base-ws",
                                      node_id="inact-base-2", name="基线残留文件.docx", extension="docx",
                                      node_type="file", source_created_at="2026-05-01T09:00:00"))
        db.commit()
        assert "inactive-base-ws" not in metrics_module._collect(db)["space_totals"]
    with TestClient(app) as client:
        items = client.get("/api/v1/files", params={"query": "基线残留文件"}).json()["items"]
        assert all(item["workspace_id"] != "inactive-base-ws" for item in items)
    with SessionLocal() as db:
        db.get(Workspace, "inactive-base-ws").is_active = True
        db.commit()
        assert metrics_module._collect(db)["space_totals"].get("inactive-base-ws") == 1
        db.query(HistoricalFileNode).filter(
            HistoricalFileNode.node_id.in_(("inact-base-1", "inact-base-2"))).delete(synchronize_session=False)
        db.query(Workspace).filter(Workspace.workspace_id == "inactive-base-ws").delete(synchronize_session=False)
        db.commit()


def test_workspaces_api_hides_inactive():
    """codex 第八轮 P1：不可见库（连续缺席/404 自动标记）退出知识库列表。"""
    init_db()
    with SessionLocal() as db:
        db.merge(Workspace(workspace_id="api-inactive-1", name="X-已删除测试库",
                           watch_seeded=True, is_active=False))
        db.commit()
    with TestClient(app) as client:
        ids = [item["workspace_id"] for item in
               client.get("/api/v1/workspaces", params={"query": "X-已删除测试库", "limit": 200}).json()["items"]]
        assert "api-inactive-1" not in ids
    with SessionLocal() as db:
        db.get(Workspace, "api-inactive-1").is_active = True
        db.commit()
    with TestClient(app) as client:
        ids = [item["workspace_id"] for item in
               client.get("/api/v1/workspaces", params={"query": "X-已删除测试库", "limit": 200}).json()["items"]]
        assert "api-inactive-1" in ids
    with SessionLocal() as db:
        db.query(Workspace).filter(Workspace.workspace_id == "api-inactive-1").delete(synchronize_session=False)
        db.commit()


def test_no_content_review_skips_with_reason():
    """2026-08-17 拍板：拿不到正文不评审、不推送——任务 skipped 带
    content_unavailable:* 原因落日志；手动重评也必须经过正文门禁。"""
    import pytest

    from app import service
    from app.db import ReviewJob

    init_db()
    with SessionLocal() as db:
        _ensure_demo_workspace(db)
        db.query(ReviewJob).filter(ReviewJob.node_id == "note-doc").delete(synchronize_session=False)
        db.query(ReviewInstance).filter(ReviewInstance.node_id == "note-doc").delete(synchronize_session=False)
        db.merge(Document(node_id="note-doc", workspace_id="demo-workspace", name="正文原因_V1.0.docx",
                          extension="docx", uploader_name="张三", department_name="研发中心",
                          file_class="document"))
        db.commit()
        with pytest.raises(service.ContentUnavailableError):
            service.run_review(db, get_settings(), "note-doc", trigger="audit")
        assert db.scalars(select(ReviewInstance).where(ReviewInstance.node_id == "note-doc")).all() == []
        # 任务执行层：同样的失败映射成 skipped + 原因码
        db.add(ReviewJob(job_id="nc-job-1", node_id="note-doc", trigger="audit", status="pending"))
        db.commit()
        for _ in range(50):  # 共享库可能有别的残留任务排在前面，逐个沥干
            job = db.get(ReviewJob, "nc-job-1")
            if job.status != "pending" or not service.process_next_job(db, get_settings()):
                break
        job = db.get(ReviewJob, "nc-job-1")
        assert job.status == "skipped" and job.error_code.startswith("content_unavailable")
        # 手动重评同样不能在无正文时生成一个貌似完整的分数。
        with pytest.raises(service.ContentUnavailableError):
            service.run_review(db, get_settings(), "note-doc", trigger="manual_rerun")
        assert db.scalars(select(ReviewInstance).where(ReviewInstance.node_id == "note-doc")).all() == []
        db.merge(Document(node_id="note-folder", workspace_id="demo-workspace", name="目录",
                          is_folder=True, file_class="folder"))
        db.commit()
        with pytest.raises(service.ContentUnavailableError, match="unsupported"):
            service.run_review(db, get_settings(), "note-folder", trigger="manual_rerun")
        db.query(Document).filter(Document.node_id == "note-folder").delete(synchronize_session=False)
        db.query(ReviewJob).filter(ReviewJob.node_id == "note-doc").delete(synchronize_session=False)
        db.query(ReviewInstance).filter(ReviewInstance.node_id == "note-doc").delete(synchronize_session=False)
        db.query(Document).filter(Document.node_id == "note-doc").delete(synchronize_session=False)
        db.commit()


def test_metadata_only_instance_never_pushes():
    from types import SimpleNamespace

    from app import notify as notify_module

    init_db()
    with SessionLocal() as db:
        doc = SimpleNamespace(node_id="nopush-1", workspace_id="ws-x", uploader_key="u-1",
                              name="无正文.docx", department_name="AI应用研发部")
        partial = SimpleNamespace(review_instance_id="ri-nopush", ai_score=88.0, verdict="pass",
                                  review_scope="metadata_only", rule_version="V1.1", findings=[])
        settings = get_settings().model_copy(update={"notify_enabled": True, "notify_workspaces": "",
                                                     "notify_departments": "", "notify_on_pass": True})
        row = notify_module.enqueue_review_notification(db, settings, doc, partial)
        db.flush()
        assert row is not None and row.status == "skipped" and row.error_code == "no_content_not_pushed"
        db.rollback()


def test_unknown_action_reopen_is_exact_and_requires_current_whitelist(monkeypatch):
    import sys
    import time

    import pytest

    from app.db import FileAuditEvent
    from scripts import reopen_dead_letters

    init_db()
    target_id = "reopen-unknown-target"
    other_id = "reopen-unknown-other"
    now_ms = int(time.time() * 1000)
    with SessionLocal() as db:
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.in_((target_id, other_id))).delete(
            synchronize_session=False
        )
        db.add_all([
            FileAuditEvent(biz_id=target_id, gmt_create=now_ms, action_view="覆盖文件",
                           module_view="团队空间", processed=True,
                           resolution="ignored_unknown_action"),
            FileAuditEvent(biz_id=other_id, gmt_create=now_ms, action_view="别的动作",
                           module_view="团队空间", processed=True,
                           resolution="ignored_unknown_action"),
        ])
        db.commit()

    monkeypatch.setattr(sys, "argv", ["reopen_dead_letters.py", "--unknown-action", "覆盖文件",
                                       "--days", "1", "--apply"])
    reopen_dead_letters.main()
    with SessionLocal() as db:
        target = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == target_id))
        other = db.scalar(select(FileAuditEvent).where(FileAuditEvent.biz_id == other_id))
        assert target.processed is False and target.resolution == "" and target.retry_started_at is not None
        assert other.processed is True and other.resolution == "ignored_unknown_action"
        db.query(FileAuditEvent).filter(FileAuditEvent.biz_id.in_((target_id, other_id))).delete(
            synchronize_session=False
        )
        db.commit()

    monkeypatch.setattr(sys, "argv", ["reopen_dead_letters.py", "--unknown-action", "仍未识别的动作"])
    with pytest.raises(SystemExit, match="仍未进入白名单"):
        reopen_dead_letters.main()


def test_dependency_credential_log_filter_blocks_info_only():
    import logging

    from app.worker import _DependencyCredentialFilter

    guard = _DependencyCredentialFilter()
    http_info = logging.LogRecord("httpx", logging.INFO, __file__, 1, "signed url", (), None)
    stream_info = logging.LogRecord("dingtalk_stream.client", logging.INFO, __file__, 1, "ticket", (), None)
    http_warning = logging.LogRecord("httpx", logging.WARNING, __file__, 1, "failed", (), None)
    app_info = logging.LogRecord("kg.worker", logging.INFO, __file__, 1, "summary", (), None)
    assert guard.filter(http_info) is False and guard.filter(stream_info) is False
    assert guard.filter(http_warning) is True and guard.filter(app_info) is True


def test_storage_key_metric_excludes_native_documents():
    from scripts.status_brief import _storage_key_review_classes

    classes = _storage_key_review_classes("")
    assert "native_doc" not in classes
    assert {"document", "sheet", "slide", "text"} <= classes


def test_run_review_once_uses_current_trigger_and_structured_results(monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace

    from app.service import ContentUnavailableError
    from scripts import run_review_once

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(run_review_once, "init_db", lambda: None)
    monkeypatch.setattr(run_review_once, "SessionLocal", FakeSession)
    monkeypatch.setattr(run_review_once, "get_settings", lambda: object())
    triggers = []

    def fake_review(db, settings, node_id, trigger):
        triggers.append(trigger)
        return SimpleNamespace(
            review_instance_id="review-1", node_id=node_id, ai_score=88,
            verdict="pass", review_scope="full_content", rule_version="V1.1",
            model_config_version="rule-engine", content_fingerprint="abcdef1234567890",
            dimensions={}, findings=[],
        )

    monkeypatch.setattr(run_review_once, "run_review", fake_review)
    monkeypatch.setattr(sys, "argv", ["run_review_once.py", "node-1"])
    assert run_review_once.main() == 0
    succeeded = json.loads(capsys.readouterr().out)
    assert triggers == ["manual_rerun"] and succeeded["status"] == "succeeded"

    monkeypatch.setattr(run_review_once, "run_review", lambda *args: None)
    assert run_review_once.main() == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged == {"status": "skipped", "node_id": "node-1",
                         "error_code": "content_unchanged"}

    def no_content(*args):
        raise ContentUnavailableError("fetch_failed:dingtalk_adoc_export_timeout")

    monkeypatch.setattr(run_review_once, "run_review", no_content)
    assert run_review_once.main() == 0
    unavailable = json.loads(capsys.readouterr().out)
    assert unavailable["status"] == "skipped"
    assert unavailable["error_code"] == "content_unavailable:fetch_failed:dingtalk_adoc_export_timeout"

    def failed_init():
        raise RuntimeError("mysql://secret-that-must-not-leak")

    monkeypatch.setattr(run_review_once, "init_db", failed_init)
    assert run_review_once.main() == 1
    failed = capsys.readouterr().out
    assert json.loads(failed)["error_code"] == "review_execution_failed"
    assert "secret-that-must-not-leak" not in failed
