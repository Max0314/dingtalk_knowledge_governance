import os
from fastapi.testclient import TestClient

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from app.main import app


def test_dashboard_and_document_endpoints():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        overview = client.get("/api/v1/dashboard/overview")
        assert overview.status_code == 200
        assert overview.json()["metrics"]["total_files"] >= 3
        increments = client.get("/api/v1/metrics/monthly-increments")
        assert increments.status_code == 200
        assert sum(row["total"] for row in increments.json()["rows"]) >= 3
        coverage = client.get("/api/v1/metrics/coverage")
        assert coverage.status_code == 200
        assert coverage.json()["summary"]["visible_workspaces"] >= 1
        docs = client.get("/api/v1/documents").json()["items"]
        assert docs
        detail = client.get(f"/api/v1/documents/{docs[0]['node_id']}")
        assert detail.status_code == 200
        queued = client.post(f"/api/v1/documents/{docs[0]['node_id']}/reviews", json={"trigger": "test"})
        assert queued.status_code == 202


def test_baseline_endpoints_and_notification_outbox():
    from app.config import get_settings
    from app.db import Document, HistoricalFileNode, HistoricalSnapshot, Notification, ReviewInstance, SessionLocal
    from app.notify import enqueue_review_notification

    with TestClient(app) as client:
        with SessionLocal() as db:
            if not db.get(HistoricalSnapshot, "test-snap"):
                db.add(HistoricalSnapshot(snapshot_id="test-snap", total_file_nodes=2))
                db.add_all([
                    HistoricalFileNode(snapshot_id="test-snap", workspace_id="demo-workspace", node_id="hist-A",
                                       parent_node_id="folder-1", name="历史文档甲.docx", extension="docx",
                                       source_created_at="2026-07-15T10:00:00+08:00"),
                    HistoricalFileNode(snapshot_id="test-snap", workspace_id="demo-workspace", node_id="hist-B",
                                       parent_node_id="folder-1", name="历史文档乙.pdf", extension="pdf",
                                       source_created_at="2026-07-16T10:00:00+08:00"),
                ])
                db.commit()
        # snapshot_id passed explicitly: uploader stats seeded by a sibling test
        # would otherwise win the default-snapshot pick and empty the listing.
        folders = client.get("/api/v1/baseline/workspaces/demo-workspace/folders", params={"snapshot_id": "test-snap"}).json()
        assert folders["items"] and folders["items"][0]["parent_node_id"] == "folder-1"
        files = client.get("/api/v1/baseline/files", params={"workspace_id": "demo-workspace", "query": "历史", "snapshot_id": "test-snap"}).json()
        assert files["total"] == 2

        # A failed review enqueues exactly when notify is enabled and uploader is known.
        with SessionLocal() as db:
            doc = db.query(Document).filter(Document.node_id == "demo-001").one()
            instance = db.query(ReviewInstance).filter(ReviewInstance.node_id == doc.node_id).first()
            settings = get_settings().model_copy(update={"notify_enabled": True})
            instance.verdict = "return"
            # 2026-08-17 拍板后只有拿到正文的评审才推送；样本按完整评审模拟
            instance.review_scope = "full_content"
            row = enqueue_review_notification(db, settings, doc, instance)
            assert row is not None and row.target_union_id == doc.uploader_key
            db.rollback()
        listing = client.get("/api/v1/notifications").json()
        assert "notify_enabled" in listing and isinstance(listing["items"], list)


def test_uploader_stats_read_only_aggregates():
    from app.db import EmployeeMap, SessionLocal, UploaderMonthStat

    with TestClient(app) as client:
        with SessionLocal() as db:
            if not db.get(EmployeeMap, "u-100"):
                db.add_all([
                    UploaderMonthStat(snapshot_id="snap-t", workspace_id="ws1", workspace_name="研发库",
                                      creator_user_id="u-100", month="2026-07", file_count=7),
                    UploaderMonthStat(snapshot_id="snap-t", workspace_id="ws1", workspace_name="研发库",
                                      creator_user_id="u-robot", month="2026-07", file_count=99),
                    EmployeeMap(user_id="u-100", employee_key="uk-100", name="张三", department_name="研发中心",
                                biz_group_name="平台组", matched=True, include_official=True),
                ])
                db.commit()
        months = client.get("/api/v1/metrics/uploaders/months").json()
        assert any(m["month"] == "2026-07" for m in months["months"])
        data = client.get("/api/v1/metrics/uploaders", params={"month": "2026-07"}).json()
        assert [item["name"] for item in data["items"]] == ["张三"]
        assert data["unmatched_files"] == 99
        both = client.get("/api/v1/metrics/uploaders", params={"month": "2026-07", "exclude_unmatched": "false"}).json()
        assert len(both["items"]) == 2
        detail = client.get("/api/v1/metrics/uploaders/u-100").json()
        assert detail["months"] == [{"month": "2026-07", "count": 7}]
        dept = client.get("/api/v1/metrics/departments", params={"month": "2026-07"}).json()
        assert any(item["department_name"] == "研发中心" and item["files"] == 7 for item in dept["items"])


def test_auth_guard_blocks_api_when_enabled():
    from app.config import get_settings

    os.environ["KG_AUTH_ENABLED"] = "true"
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/auth/me").status_code == 401
            assert client.get("/api/v1/dashboard/overview").status_code == 401
            login = client.get("/api/auth/login-url", params={"return_url": "/"})
            assert login.status_code in (200, 400)
    finally:
        os.environ["KG_AUTH_ENABLED"] = "false"
        get_settings.cache_clear()


def test_connectivity_never_claims_unconfigured_integrations_are_healthy():
    with TestClient(app) as client:
        payload = client.get("/api/v1/diagnostics/connectivity").json()
        statuses = {item["name"]: item["status"] for item in payload["items"]}
        assert statuses["钉钉知识库"] == "not_configured"


def test_bulk_classification_and_increments_tree(monkeypatch):
    from app import metrics
    from app.config import get_settings
    from app.db import Document, SessionLocal

    monkeypatch.setattr(metrics, "PERSON_DAY_BULK_MIN", 3)
    os.environ["KG_ROBOT_USER_IDS"] = "r-bot"
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            with SessionLocal() as db:
                db.query(Document).filter(Document.node_id.like("bulk-t-%")).delete(synchronize_session=False)
                rows = [Document(node_id="bulk-t-r", workspace_id="demo-workspace", name="机器人导入.docx",
                                 uploader_key="r-bot", source_created_at="2030-05-10", file_class="document")]
                rows += [Document(node_id=f"bulk-t-h{i}", workspace_id="demo-workspace", name=f"搬家{i}.docx",
                                  uploader_key="h-mover", source_created_at="2030-05-11", file_class="document")
                         for i in range(3)]
                rows.append(Document(node_id="bulk-t-n", workspace_id="demo-workspace", name="正常上传.docx",
                                     uploader_key="h-normal", source_created_at="2030-05-11", file_class="document"))
                db.add_all(rows); db.commit()
                metrics.invalidate_cache()
            months = client.get("/api/v1/metrics/increments/tree", params={"year": "2030"}).json()
            row = next(r for r in months["rows"] if r["key"] == "2030-05")
            # robot(1) + mover person-day of 3 = bulk 4; the single normal upload stays routine
            assert row["total"] == 5 and row["bulk"] == 4 and row["routine"] == 1
            years = client.get("/api/v1/metrics/increments/tree").json()
            assert years["level"] == "year" and years["rows"][0]["key"] == "2030"  # recent years on top
            days = client.get("/api/v1/metrics/increments/tree", params={"month": "2030-05"}).json()
            assert {r["key"]: r["bulk"] for r in days["rows"]} == {"2030-05-10": 1, "2030-05-11": 3}
            with SessionLocal() as db:
                db.query(Document).filter(Document.node_id.like("bulk-t-%")).delete(synchronize_session=False)
                db.commit()
                metrics.invalidate_cache()
    finally:
        os.environ.pop("KG_ROBOT_USER_IDS", None)
        get_settings.cache_clear()


def test_soft_deleted_document_leaves_the_headline_total():
    from app import metrics
    from app.db import Document, SessionLocal

    with TestClient(app) as client:
        with SessionLocal() as db:
            # 自愈：上一次失败会把 hist-A 留在已删除状态（断言中止在恢复之前），
            # 先复位再测量，否则前后都是"已删"基线，永远差不出 1。
            stale = db.get(Document, "hist-A")
            if stale is not None and stale.is_deleted:
                stale.is_deleted = False
                db.commit()
            metrics.invalidate_cache()
            baseline_total = metrics.monthly_increments(db)["total_files"]
            # hist-A exists in the primary baseline snapshot; a mirrored soft
            # delete must remove it from the merged headline count.
            doc = db.get(Document, "hist-A")
            if not doc:
                doc = Document(node_id="hist-A", workspace_id="demo-workspace", name="历史文档甲.docx",
                               extension="docx", file_class="document")
                db.add(doc)
            doc.is_deleted = True
            db.commit()
            metrics.invalidate_cache()  # bust the metrics cache
            after = metrics.monthly_increments(db)["total_files"]
            assert after == baseline_total - 1
            doc.is_deleted = False
            db.commit()
            metrics.invalidate_cache()


def test_admin_guard_gates_model_configs():
    from app import auth as auth_module
    from app.config import get_settings

    os.environ["KG_AUTH_ENABLED"] = "true"
    os.environ["KG_ADMIN_UNION_IDS"] = "admin-union"
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            client.cookies.set(auth_module.COOKIE_NAME, auth_module.issue_session({"union_id": "ordinary", "name": "普通员工"}))
            assert client.get("/api/v1/model-configs").status_code == 403
            assert client.get("/api/v1/diagnostics/connectivity").status_code == 403
            assert client.get("/api/v1/documents").status_code == 200  # data views stay open to logged-in users
            client.cookies.set(auth_module.COOKIE_NAME, auth_module.issue_session({"union_id": "admin-union", "name": "管理员"}))
            assert client.get("/api/v1/model-configs").status_code == 200
    finally:
        os.environ["KG_AUTH_ENABLED"] = "false"
        os.environ.pop("KG_ADMIN_UNION_IDS", None)
        get_settings.cache_clear()


def test_workspace_registry_classification_and_filters():
    from app.db import SessionLocal, Workspace, WorkspaceRole

    with TestClient(app) as client:
        with SessionLocal() as db:
            if not db.get(Workspace, "ws-C1"):
                db.add_all([Workspace(workspace_id="ws-C1", name="C-公司制度库"),
                            Workspace(workspace_id="ws-D1", name="D-研发部资料"),
                            Workspace(workspace_id="ws-I1", name="I-张三")])
                db.add(WorkspaceRole(workspace_id="ws-D1", employee_key="a1", role="administrator", display_name="李管理"))
                db.commit()
        by_level = client.get("/api/v1/workspaces", params={"level": "D"}).json()
        assert by_level["total"] >= 1 and all(item["level"] == "D" for item in by_level["items"])
        by_admin = client.get("/api/v1/workspaces", params={"admin": "李管理"}).json()
        assert [item["workspace_id"] for item in by_admin["items"]] == ["ws-D1"]
        paged = client.get("/api/v1/workspaces", params={"limit": 1}).json()
        assert len(paged["items"]) == 1 and paged["total"] >= 4 and paged["levels"]
        search = client.get("/api/v1/workspaces", params={"query": "研发部"}).json()
        assert search["total"] == 1
