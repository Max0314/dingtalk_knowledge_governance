"""Login entry and failure-path tests; no real identity or provider requests."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import auth
from app.config import Settings
from app.main import app


@pytest.fixture
def login_settings(monkeypatch):
    settings = Settings(_env_file=None, auth_enabled=True,
                        auth_secret="unit-test-signature-only",
                        public_base_url="https://example.invalid/knowledge_governance",
                        DINGTALK_APP_KEY="unit-test-app")
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "resolve_session", lambda _: None)
    return settings


@pytest.mark.parametrize("raw", [
    "//outside.invalid/", "https://example.invalid.evil.invalid/knowledge_governance/",
    "https://example.invalid/knowledge_governance_other/",
    "/other-app/", "/knowledge_governance/login.html",
    "https://outside.invalid/", "/\\outside.invalid/", "/\nother-app/", None,
])
def test_return_url_rejects_external_or_non_workspace_entry(login_settings, raw):
    assert auth.sanitize_return_url(login_settings, raw) == login_settings.public_base_url + "/"


def test_return_url_keeps_valid_deep_link(login_settings):
    expected = login_settings.public_base_url + "/?v=release#/reviews"
    assert auth.sanitize_return_url(login_settings, expected) == expected
    assert auth.sanitize_return_url(login_settings, "/knowledge_governance/#/increments") == (
        login_settings.public_base_url + "/#/increments")


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_anonymous_entry_redirects_before_loading_workspace(login_settings, path):
    response = TestClient(app).get(path, follow_redirects=False)
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/knowledge_governance/login.html"
    assert response.headers["cache-control"] == "no-store"


def test_login_document_has_no_menu_or_charts(login_settings):
    client = TestClient(app)
    response = client.get("/login.html")
    assert response.status_code == 200
    assert 'id="login-button"' in response.text
    assert "app-sidebar" not in response.text
    assert "echarts" not in response.text
    assert "app.js" not in response.text
    assert client.get("/login.js").status_code == 200
    assert client.get("/login.css").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/v1/dashboard/overview").status_code == 401


def test_valid_session_can_open_workspace(login_settings, monkeypatch):
    monkeypatch.setattr(auth, "resolve_session", lambda _: {"union_id": "synthetic-identity"})
    assert TestClient(app).get("/").status_code == 200


def test_session_store_failure_does_not_open_workspace(login_settings, monkeypatch):
    def unavailable(_):
        raise OperationalError("", {}, Exception("synthetic failure"))
    monkeypatch.setattr(auth, "resolve_session", unavailable)
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["session_store_unavailable"]
    assert client.get("/api/v1/dashboard/overview").status_code == 503
    assert client.get("/api/auth/me").status_code == 503
    assert client.get("/api/auth/me").headers["cache-control"] == "no-store"


@pytest.mark.parametrize("payload", [[], {"t": "bad"}, {"t": int(time.time()) + 1000}, {"t": 10 ** 1000}])
def test_signed_malformed_or_future_state_is_rejected(login_settings, payload):
    token = json.dumps(payload).encode().hex()
    with pytest.raises(ValueError):
        auth.read_state(login_settings, token + "." + auth._sign(login_settings, token))


def test_callback_failure_returns_to_standalone_login(login_settings):
    response = TestClient(app).get("/api/auth/dingtalk/callback?state=invalid", follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["state_invalid"]


def test_callback_network_failure_is_retryable(login_settings, monkeypatch):
    async def unavailable(*_):
        raise httpx.ConnectError("synthetic network failure")
    monkeypatch.setattr(auth, "exchange_user", unavailable)
    target = login_settings.public_base_url + "/#/reviews"
    state = auth.make_state(login_settings, target)
    response = TestClient(app).get("/api/auth/dingtalk/callback", params={"authCode": "synthetic-code", "state": state}, follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert response.status_code == 303
    assert query["error"] == ["authorization_unavailable"]
    assert query["return_url"] == [target]


def test_callback_session_failure_is_retryable(login_settings, monkeypatch):
    async def user(*_):
        return {"union_id": "synthetic-identity"}
    def unavailable(_):
        raise OperationalError("", {}, Exception("synthetic failure"))
    monkeypatch.setattr(auth, "exchange_user", user)
    monkeypatch.setattr(auth, "issue_session", unavailable)
    state = auth.make_state(login_settings, login_settings.public_base_url + "/")
    response = TestClient(app).get("/api/auth/dingtalk/callback", params={"code": "synthetic-code", "state": state}, follow_redirects=False)
    assert response.status_code == 303
    assert "session_store_unavailable" in response.headers["location"]


def test_callback_success_restores_deep_link(login_settings, monkeypatch):
    async def user(*_):
        return {"union_id": "synthetic-identity"}
    monkeypatch.setattr(auth, "exchange_user", user)
    monkeypatch.setattr(auth, "issue_session", lambda _: "synthetic-session")
    target = login_settings.public_base_url + "/#/documents"
    response = TestClient(app).get("/api/auth/dingtalk/callback", params={"code": "synthetic-code", "state": auth.make_state(login_settings, target)}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == target
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
