import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import metrics, read_cache
from app.config import get_settings
from app.db import Document, ReadCacheEntry, ReviewJob, SessionLocal, Workspace, init_db, utcnow
from app.main import app


@pytest.fixture
def caching(monkeypatch):
    init_db()
    settings = get_settings().model_copy(update={"demo_mode": False, "read_cache_enabled": True,
                  "read_cache_ttl_seconds": 30, "read_cache_max_age_seconds": 60})
    monkeypatch.setattr(read_cache, "get_settings", lambda: settings)
    read_cache.clear_memory()
    yield settings
    read_cache.clear_memory()


def test_cold_callers_singleflight_and_restart_reads_persistent_cache(caching):
    name = "test_" + uuid.uuid4().hex
    calls = []
    def compute(db):
        calls.append(1)
        time.sleep(.08)
        return {"total": 42}
    read_cache._registry[name] = (compute, {})
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: read_cache.read(name, {}), range(16)))
        assert len(calls) == 1
        assert all(json.loads(result.body)["total"] == 42 for result in results)
        read_cache.clear_memory()
        read_cache._registry[name] = (lambda db: pytest.fail("restart must use persistent response"), {})
        result = read_cache.read(name, {})
        assert result.headers["x-kg-cache"] == "hit"
    finally:
        read_cache._registry.pop(name)
        with SessionLocal() as db:
            db.execute(delete(ReadCacheEntry).where(ReadCacheEntry.endpoint == name))
            db.commit()


def test_stale_is_fast_but_expired_failure_never_serves_old_data(caching, monkeypatch):
    generated = utcnow() - timedelta(seconds=45)
    monkeypatch.setattr(read_cache, "_load", lambda key: (b'{"total":1}', generated))
    future = Future()
    future.set_exception(HTTPException(503, "unavailable"))
    monkeypatch.setattr(read_cache, "_launch", lambda *args: future)
    result = read_cache.read("fake", {})
    assert result.headers["x-kg-cache"] == "stale"
    generated = utcnow() - timedelta(seconds=61)
    with pytest.raises(HTTPException) as error:
        read_cache.read("fake", {})
    assert error.value.status_code == 503


def test_memory_budget_evicts_and_does_not_cache_auth_routes(caching):
    for i in range(read_cache.MAX_KEYS + 10):
        read_cache._remember(str(i), {"total": i}, utcnow())
    assert len(read_cache._memory) == read_cache.MAX_KEYS
    assert read_cache._bytes <= read_cache.MAX_BYTES
    assert "auth_config" not in read_cache._registry
    assert "model_configs" not in read_cache._registry


def test_http_warm_response_and_auth_gate(monkeypatch, caching):
    import app.auth as auth
    settings = caching.model_copy(update={"auth_enabled": True})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/overview")
        assert response.status_code == 401
        monkeypatch.setattr(auth, "resolve_session", lambda token: {"union_id": "synthetic-viewer"})
        response = client.get("/api/v1/dashboard/overview")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert client.get("/api/v1/dashboard/overview").headers["x-kg-cache"] == "hit"


def test_soft_delete_refresh_even_when_record_count_unchanged(monkeypatch):
    metrics.invalidate_cache()
    calls = []
    monkeypatch.setattr(metrics, "_collect", lambda db: calls.append(1) or {"total_files": len(calls)})
    with SessionLocal() as db:
        assert metrics.collected(db)["total_files"] == 1
        metrics._cache["at"] -= metrics.CACHE_MAX_AGE_SECONDS + 1
        assert metrics.collected(db)["total_files"] == 2
    metrics.invalidate_cache()


def test_restart_does_not_requeue_another_workers_live_lease():
    from app.service import sweep_stale_runs
    suffix = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(Workspace(workspace_id=suffix, name="Synthetic"))
        db.flush()
        db.add(Document(node_id=suffix, workspace_id=suffix, name="Synthetic"))
        db.flush()
        db.add(ReviewJob(job_id=suffix, node_id=suffix, status="running", lease_until=utcnow() + timedelta(minutes=30)))
        db.commit()
        sweep_stale_runs(db, sync_runs=False, bridge_walks=False)
        assert db.get(ReviewJob, suffix).status == "running"
        db.get(ReviewJob, suffix).lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
        sweep_stale_runs(db, sync_runs=False, bridge_walks=False)
        assert db.get(ReviewJob, suffix).status == "pending"
        db.delete(db.get(ReviewJob, suffix)); db.flush()
        db.delete(db.get(Document, suffix)); db.flush()
        db.delete(db.get(Workspace, suffix)); db.commit()


def test_paused_review_and_live_browse_fail_closed(monkeypatch):
    import app.main as main
    settings = get_settings().model_copy(update={"demo_mode": False, "review_enabled": False,
                  "live_browse_enabled": False, "auth_enabled": False})
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(app) as client:
        assert client.post("/api/v1/documents/demo-001/reviews", json={}).status_code == 423
        assert client.get("/api/v1/dingtalk/knowledge-bases?operator_id=synthetic").status_code == 423
        assert client.get("/api/ready").status_code == 200


def test_readiness_rejects_database_failure():
    import app.main as main
    from sqlalchemy.exc import OperationalError
    class FailedDB:
        def execute(self, statement):
            raise OperationalError("", {}, Exception("synthetic"))
    main.app.dependency_overrides[main.db_session] = lambda: FailedDB()
    try:
        response = TestClient(app).get("/api/ready")
        assert response.status_code == 503
        assert "synthetic" not in response.text
    finally:
        main.app.dependency_overrides.pop(main.db_session, None)
