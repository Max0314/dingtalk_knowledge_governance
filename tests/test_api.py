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
        assert overview.json()["metrics"]["document_count"] >= 3
        docs = client.get("/api/v1/documents").json()["items"]
        assert docs
        detail = client.get(f"/api/v1/documents/{docs[0]['node_id']}")
        assert detail.status_code == 200
        queued = client.post(f"/api/v1/documents/{docs[0]['node_id']}/reviews", json={"trigger": "test"})
        assert queued.status_code == 202


def test_connectivity_never_claims_unconfigured_integrations_are_healthy():
    with TestClient(app) as client:
        payload = client.get("/api/v1/diagnostics/connectivity").json()
        statuses = {item["name"]: item["status"] for item in payload["items"]}
        assert statuses["钉钉知识库"] == "not_configured"
