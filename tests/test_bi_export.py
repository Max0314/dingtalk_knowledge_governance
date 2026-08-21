from __future__ import annotations

import os

from fastapi.testclient import TestClient

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from app.config import get_settings
from app.db import EmployeeMap, SessionLocal, UploaderMonthStat
from app.main import app


HEADERS = {"X-API-Key": "export-test-key"}
SNAPSHOT_ID = "zz-bi-export-test"
MONTH = "2026-08"


def _configure(monkeypatch, *, enabled: str = "true", keys: str = "export-test-key") -> None:
    monkeypatch.setenv("KG_BI_EXPORT_ENABLED", enabled)
    monkeypatch.setenv("KG_BI_EXPORT_API_KEYS", keys)
    monkeypatch.setenv("KG_BI_EXPORT_MAX_PAGE_SIZE", "200")
    get_settings.cache_clear()


def _seed_export_facts() -> None:
    with SessionLocal() as db:
        db.query(UploaderMonthStat).filter(UploaderMonthStat.snapshot_id == SNAPSHOT_ID).delete(
            synchronize_session=False
        )
        db.query(EmployeeMap).filter(
            EmployeeMap.user_id.in_(("export-a", "export-alias", "export-robot"))
        ).delete(synchronize_session=False)
        db.add_all(
            [
                UploaderMonthStat(
                    snapshot_id=SNAPSHOT_ID,
                    workspace_id="ws-product",
                    workspace_name="产品知识库",
                    creator_user_id="export-a",
                    month=MONTH,
                    file_count=5,
                ),
                UploaderMonthStat(
                    snapshot_id=SNAPSHOT_ID,
                    workspace_id="ws-product",
                    workspace_name="产品知识库",
                    creator_user_id="export-alias",
                    month=MONTH,
                    file_count=2,
                ),
                UploaderMonthStat(
                    snapshot_id=SNAPSHOT_ID,
                    workspace_id="ws-rd",
                    workspace_name="研发知识库",
                    creator_user_id="export-a",
                    month=MONTH,
                    file_count=3,
                ),
                UploaderMonthStat(
                    snapshot_id=SNAPSHOT_ID,
                    workspace_id="ws-robot",
                    workspace_name="机器人库",
                    creator_user_id="export-robot",
                    month=MONTH,
                    file_count=8,
                ),
                UploaderMonthStat(
                    snapshot_id=SNAPSHOT_ID,
                    workspace_id="ws-unmapped",
                    workspace_name="未映射库",
                    creator_user_id="export-unmapped",
                    month=MONTH,
                    file_count=4,
                ),
                EmployeeMap(
                    user_id="export-a",
                    employee_key="union-export-a",
                    name="不应导出",
                    department_name="不应导出",
                    matched=True,
                    include_official=True,
                ),
                EmployeeMap(
                    user_id="export-alias",
                    employee_key="union-export-a",
                    matched=True,
                    include_official=True,
                ),
                EmployeeMap(
                    user_id="export-robot",
                    employee_key="union-export-robot",
                    matched=True,
                    include_official=True,
                ),
            ]
        )
        db.commit()


def _clean_export_facts() -> None:
    with SessionLocal() as db:
        db.query(UploaderMonthStat).filter(UploaderMonthStat.snapshot_id == SNAPSHOT_ID).delete(
            synchronize_session=False
        )
        db.query(EmployeeMap).filter(
            EmployeeMap.user_id.in_(("export-a", "export-alias", "export-robot"))
        ).delete(synchronize_session=False)
        db.commit()


def test_export_upload_facts_are_paginated_and_safe(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("KG_ROBOT_USER_IDS", "export-robot")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            _seed_export_facts()

            latest = client.get("/api/export/v1/knowledge-governance/latest", headers=HEADERS)
            assert latest.status_code == 200
            assert latest.json()["data"]["latestMonth"] == MONTH
            assert latest.json()["meta"]["contractVersion"] == 1

            summary = client.get(
                "/api/export/v1/knowledge-governance/monthly-summary",
                params={"month": MONTH},
                headers=HEADERS,
            )
            assert summary.status_code == 200
            payload = summary.json()["data"]
            assert payload["allObserved"]["uploadedFileCount"] == 22
            assert payload["officialEmployees"]["uploadedFileCount"] == 10
            assert payload["officialEmployees"]["employeeCount"] == 1
            assert payload["diagnostics"]["excludedUploadedFileCount"] == 12

            employees = client.get(
                "/api/export/v1/knowledge-governance/monthly-employees",
                params={"month": MONTH, "pageSize": 1},
                headers=HEADERS,
            )
            assert employees.status_code == 200
            employee = employees.json()["data"][0]
            assert employee == {
                "month": MONTH,
                "employeeKey": "union-export-a",
                "uploadedFileCount": 10,
                "workspaceCount": 2,
            }
            assert employees.json()["pagination"] == {"page": 1, "pageSize": 1, "total": 1, "totalPages": 1}
            assert "sourceUserId" not in employee and "name" not in employee and "departmentName" not in employee

            spaces = client.get(
                "/api/export/v1/knowledge-governance/monthly-employee-workspaces",
                params={"month": MONTH, "pageSize": 1},
                headers=HEADERS,
            )
            assert spaces.status_code == 200
            first = spaces.json()["data"][0]
            assert first["workspaceId"] == "ws-product"
            assert first["workspaceName"] == "产品知识库"
            assert first["uploadedFileCount"] == 7
            assert spaces.json()["pagination"] == {"page": 1, "pageSize": 1, "total": 2, "totalPages": 2}
    finally:
        _clean_export_facts()
        monkeypatch.delenv("KG_ROBOT_USER_IDS", raising=False)
        get_settings.cache_clear()


def test_export_guard_and_request_validation(monkeypatch):
    _configure(monkeypatch)
    try:
        with TestClient(app) as client:
            assert client.get("/api/export/v1/knowledge-governance/latest").status_code == 401
            assert client.get(
                "/api/export/v1/knowledge-governance/latest", headers={"X-API-Key": "wrong"}
            ).status_code == 401
            invalid_month = client.get(
                "/api/export/v1/knowledge-governance/monthly-summary",
                params={"month": "2026-13"},
                headers=HEADERS,
            )
            assert invalid_month.status_code == 400
            assert invalid_month.json() == {"ok": False, "error": "invalid_month"}
            invalid_page = client.get(
                "/api/export/v1/knowledge-governance/monthly-employees",
                params={"month": MONTH, "page": "0"},
                headers=HEADERS,
            )
            assert invalid_page.status_code == 400
            assert invalid_page.json() == {"ok": False, "error": "invalid_pagination"}
    finally:
        get_settings.cache_clear()


def test_export_bypasses_cookie_guard_but_requires_own_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("KG_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/api/export/v1/knowledge-governance/latest", headers=HEADERS)
            assert response.status_code == 200
            assert client.get("/api/v1/dashboard/overview").status_code == 401
    finally:
        monkeypatch.setenv("KG_AUTH_ENABLED", "false")
        get_settings.cache_clear()


def test_export_disabled_or_unconfigured(monkeypatch):
    _configure(monkeypatch, enabled="false")
    try:
        with TestClient(app) as client:
            disabled = client.get("/api/export/v1/knowledge-governance/latest", headers=HEADERS)
            assert disabled.status_code == 404
            assert disabled.json() == {"ok": False, "error": "export_disabled"}
    finally:
        get_settings.cache_clear()

    _configure(monkeypatch, keys="")
    try:
        with TestClient(app) as client:
            unconfigured = client.get("/api/export/v1/knowledge-governance/latest", headers=HEADERS)
            assert unconfigured.status_code == 503
            assert unconfigured.json() == {"ok": False, "error": "export_not_configured"}
    finally:
        get_settings.cache_clear()
