import os

from fastapi.testclient import TestClient

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from app.main import app
from app.scoring import effective_config, score_document

SAMPLE = "摘要\n一句话。\n\n1、开始\n" + "这是一个特别长的段落，" * 20 + "\n"


def _clear_rule_rows():
    from app.db import ScoringRuleConfig, ScoringRuleConfigHistory, SessionLocal
    with SessionLocal() as db:
        db.query(ScoringRuleConfigHistory).delete(synchronize_session=False)
        db.query(ScoringRuleConfig).delete(synchronize_session=False)
        db.commit()


def test_default_config_reproduces_v11_baseline():
    plain = score_document("接口文档.md", SAMPLE)
    explicit = score_document("接口文档.md", SAMPLE, "document", effective_config(None))
    assert plain == explicit


def test_disabling_and_reweighting_rules_changes_score():
    baseline = score_document("接口文档.md", SAMPLE)
    assert any(f["rule"] == "5.1" for f in baseline["findings"])  # 文件名缺 _Vx.x
    off = effective_config({"dimensions": {"format": {"rules": {"5.1": {"enabled": False}}}}})
    result = score_document("接口文档.md", SAMPLE, "document", off)
    assert not any(f["rule"] == "5.1" for f in result["findings"])
    assert result["ai_score"] == baseline["ai_score"] + 3
    heavier = effective_config({"dimensions": {"format": {"cap": 10, "rules": {"5.1": {"points": 9}}}}})
    assert score_document("接口文档.md", SAMPLE, "document", heavier)["ai_score"] == baseline["ai_score"] - 6


def test_thresholds_and_verdict_lines_follow_config():
    strict = effective_config({"pass_score": 99, "return_score": 99})
    assert score_document("接口发布规范_V1.1.md", SAMPLE, "document", strict)["verdict"] == "return"
    relaxed = effective_config({"dimensions": {"structure": {"rules": {"3.4": {"params": {"para_chars": 1000}}}}}})
    assert not any(f["rule"] == "3.4" for f in score_document("接口文档.md", SAMPLE, "document", relaxed)["findings"])
    assert any(f["rule"] == "3.4" for f in score_document("接口文档.md", SAMPLE)["findings"])


def test_effective_config_clamps_and_drops_garbage():
    cfg = effective_config({"pass_score": 300, "return_score": -5, "rule_weight": 7,
                            "dimensions": {"nope": {}, "format": {"cap": "abc", "rules": {"5.1": {"points": 999}}}}})
    assert cfg["pass_score"] == 100 and cfg["return_score"] == 0 and cfg["rule_weight"] == 1
    assert "nope" not in cfg["dimensions"]
    assert cfg["dimensions"]["format"]["cap"] == 5 and cfg["dimensions"]["format"]["rules"]["5.1"]["points"] == 100
    assert effective_config({"return_score": 90})["return_score"] == 70  # never above pass line


def test_rules_api_crud_and_review_resolution():
    from app.config import get_settings
    from app.db import ReviewInstance, SessionLocal
    from app.service import run_review

    with TestClient(app) as client:
        _clear_rule_rows()
        try:
            listing = client.get("/api/v1/scoring-rules").json()
            assert listing["global"]["config_id"] is None and listing["catalog"][0]["key"] == "metadata"
            assert listing["permissions"]["is_admin"] is True  # auth off = open dev mode

            saved = client.put("/api/v1/scoring-rules/global",
                               json={"config": {"pass_score": 80, "dimensions": {"format": {"rules": {"5.1": {"enabled": False}}}}}}).json()
            assert saved["version"] == 1 and saved["config"]["pass_score"] == 80
            assert saved["config"]["dimensions"]["format"]["rules"]["5.1"]["enabled"] is False

            created = client.post("/api/v1/scoring-rules/departments", json={"department_name": "研发中心"}).json()
            assert created["config"]["pass_score"] == 80  # department starts as a copy of global
            client.put("/api/v1/scoring-rules/departments/研发中心",
                       json={"config": {"pass_score": 90, "return_score": 40}})
            assert client.post("/api/v1/scoring-rules/departments", json={"department_name": "研发中心"}).status_code == 409

            # demo-001 的上传人部门是研发中心 -> 命中部门配置；版本随两次保存为 v2
            with SessionLocal() as db:
                instance = run_review(db, get_settings(), "demo-001", "manual_rerun")
                assert instance.rule_config_ref == "department:研发中心@v2"
                db.query(ReviewInstance).filter(ReviewInstance.review_instance_id == instance.review_instance_id).delete()
                db.commit()

            row = client.get("/api/v1/scoring-rules").json()
            dept = next(x for x in row["departments"] if x["department_name"] == "研发中心")
            history = client.get(f"/api/v1/scoring-rules/{dept['config_id']}/history").json()["items"]
            assert {h["action"] for h in history} >= {"create", "update"}
            rollback_to = next(h for h in history if h["action"] == "create")
            rolled = client.post(f"/api/v1/scoring-rules/{dept['config_id']}/rollback/{rollback_to['id']}").json()
            assert rolled["version"] == dept["version"] + 1
            after = client.get("/api/v1/scoring-rules").json()
            assert next(x for x in after["departments"] if x["department_name"] == "研发中心")["config"]["pass_score"] == 80

            deleted = client.delete("/api/v1/scoring-rules/departments/研发中心").json()
            assert deleted["fallback"] == "global"
            with SessionLocal() as db:
                instance = run_review(db, get_settings(), "demo-001", "manual_rerun")
                assert instance.rule_config_ref == "global@v1"
                db.query(ReviewInstance).filter(ReviewInstance.review_instance_id == instance.review_instance_id).delete()
                db.commit()
        finally:
            _clear_rule_rows()


def test_rules_permissions_admin_and_department_editor():
    from app import auth as auth_module
    from app.config import get_settings

    os.environ["KG_AUTH_ENABLED"] = "true"
    os.environ["KG_ADMIN_UNION_IDS"] = "admin-union"
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            _clear_rule_rows()
            admin = auth_module.issue_session({"union_id": "admin-union", "name": "管理员"})
            editor = auth_module.issue_session({"union_id": "editor-union", "name": "部门维护人"})
            ordinary = auth_module.issue_session({"union_id": "ordinary", "name": "普通员工"})

            client.cookies.set(auth_module.COOKIE_NAME, ordinary)
            view = client.get("/api/v1/scoring-rules")
            assert view.status_code == 200 and view.json()["permissions"]["is_admin"] is False  # 全员可查看
            assert client.put("/api/v1/scoring-rules/global", json={"config": {}}).status_code == 403
            assert client.post("/api/v1/scoring-rules/departments", json={"department_name": "测试部"}).status_code == 403

            client.cookies.set(auth_module.COOKIE_NAME, admin)
            assert client.put("/api/v1/scoring-rules/global", json={"config": {}}).status_code == 200
            assert client.post("/api/v1/scoring-rules/departments", json={"department_name": "测试部"}).status_code == 201
            assert client.post("/api/v1/scoring-rules/departments", json={"department_name": "别的部"}).status_code == 201
            assert client.put("/api/v1/scoring-rules/departments/测试部/editors",
                              json={"editors": [{"union_id": "editor-union", "name": "部门维护人"}]}).status_code == 200

            client.cookies.set(auth_module.COOKIE_NAME, editor)
            me = client.get("/api/v1/scoring-rules").json()
            assert me["permissions"]["editable_departments"] == ["测试部"]
            assert client.put("/api/v1/scoring-rules/departments/测试部", json={"config": {"pass_score": 75}}).status_code == 200
            assert client.put("/api/v1/scoring-rules/departments/别的部", json={"config": {}}).status_code == 403
            assert client.put("/api/v1/scoring-rules/global", json={"config": {}}).status_code == 403
            assert client.put("/api/v1/scoring-rules/departments/测试部/editors", json={"editors": []}).status_code == 403
            assert client.delete("/api/v1/scoring-rules/departments/测试部").status_code == 403
    finally:
        os.environ["KG_AUTH_ENABLED"] = "false"
        os.environ.pop("KG_ADMIN_UNION_IDS", None)
        get_settings.cache_clear()
        _clear_rule_rows()
