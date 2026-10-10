"""Quota regression tests never contact DingTalk or read real credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import api_usage, audit_bridge, scan_scheduler, service
from app.config import Settings
from app.db import ApiUsage, Base, BridgeWalk, ScanCycle, ScanTask, utcnow
from app.integrations import DingtalkClient, IntegrationError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "quota.db").as_posix(),
                           connect_args={"check_same_thread": False, "timeout": 20})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(api_usage, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def production(**overrides):
    return Settings(_env_file=None, **{"demo_mode": False, "watch_workspaces": "*", **overrides})


def test_budget_is_atomic_shared_and_persistent(isolated):
    settings = production(dingtalk_search_daily_limit=5)

    def call(_):
        try:
            api_usage.reserve_call(settings, "storage.search")
            return True
        except IntegrationError as exc:
            assert exc.code == "dingtalk_budget_exhausted"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(call, range(20))) == 5
    # New Session/client has the same counter; blocked reservations roll back.
    assert call(None) is False
    with isolated() as db:
        rows = db.scalars(select(ApiUsage)).all()
        assert len(rows) == 4 and all(r.calls == 5 for r in rows)


def test_per_scan_budget_survives_a_new_context(isolated):
    settings = production(scan_max_api_calls=2)
    for _ in range(2):
        with api_usage.scan_budget("2026-10"):
            api_usage.reserve_call(settings, "wiki.nodes")
    with api_usage.scan_budget("2026-10"), pytest.raises(IntegrationError, match="预算"):
        api_usage.reserve_call(settings, "wiki.nodes")
    with isolated() as db:
        assert db.get(ApiUsage, ("scan:2026-10", "total")).calls == 2


def test_unavailable_counter_blocks_before_http(monkeypatch):
    def unavailable():
        raise RuntimeError("database offline")
    monkeypatch.setattr(api_usage.SessionLocal, "begin", unavailable)
    with pytest.raises(IntegrationError) as exc:
        api_usage.reserve_call(production(), "wiki.nodes")
    assert exc.value.code == "dingtalk_budget_unavailable"


def test_exhausted_node_budget_never_fetches_token_or_sends_http(isolated, monkeypatch):
    settings = production(dingtalk_nodes_monthly_limit=1)
    api_usage.reserve_call(settings, "wiki.nodes")
    client = DingtalkClient(settings)

    async def forbidden():
        raise AssertionError("quota exhausted: token/network must not be touched")
    monkeypatch.setattr(client, "_token_value", forbidden)
    with pytest.raises(IntegrationError) as exc:
        asyncio.run(client.list_nodes("ws", "op", "root"))
    assert exc.value.code == "dingtalk_budget_exhausted"


def test_disabled_or_unscheduled_full_scans_do_not_call_dingtalk(isolated, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled scan touched tenant")
    monkeypatch.setattr(scan_scheduler, "resolve_watch_targets", forbidden)
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, production())["status"] == "disabled"
        with pytest.raises(IntegrationError):
            asyncio.run(service.watch_workspace(db, production(), "ws"))
        with pytest.raises(IntegrationError):
            asyncio.run(service.sync_from_dingtalk(db, production()))


def test_monthly_progress_resumes_without_rescanning_good_workspaces(isolated, monkeypatch):
    settings = production(full_scan_enabled=True, scan_days="1,24", scan_retry_seconds=60)
    walked = []

    async def targets(*args, **kwargs):
        return {"resolved": [{"workspace_id": w, "name": w, "space": {"root_node_id": "root"}}
                             for w in ("a", "b", "c")], "unresolved": []}

    async def walk(db, settings, workspace_id, space=None, mode="watch"):
        walked.append(workspace_id)
        return SimpleNamespace(run_id="r", status="failed" if workspace_id == "b" else "succeeded",
                               error_code="failed" if workspace_id == "b" else "")
    monkeypatch.setattr(scan_scheduler, "resolve_watch_targets", targets)
    monkeypatch.setattr(scan_scheduler, "watch_workspace", walk)
    # Every slice uses a new DB session, like process restarts.
    for _ in range(4):
        with isolated() as db:
            scan_scheduler.run_scan_slice(db, settings)
    assert walked == ["a", "b", "c"]
    for _ in range(2):
        with isolated() as db:
            task = db.scalar(select(ScanTask).where(ScanTask.workspace_id == "b"))
            task.next_attempt_at = utcnow() - timedelta(seconds=1)
            db.commit()
            result = scan_scheduler.run_scan_slice(db, settings)
    assert result["status"] == "completed_with_errors"
    assert walked == ["a", "b", "c", "b", "b"]
    for _ in range(3):
        with isolated() as db:
            assert asyncio.run(scan_scheduler.run_scan_slice_async(db, settings, force=True))["walked"] == []
    assert len(walked) == 5  # No rotation reset for a permanently unseeded library.


def test_scan_budget_exhaustion_is_terminal_for_month(isolated, monkeypatch):
    settings = production(full_scan_enabled=True, scan_days="1")
    calls = []

    async def targets(*args, **kwargs):
        return {"resolved": [{"workspace_id": "a", "name": "a", "space": {}}]}

    async def walk(*args, **kwargs):
        calls.append(1)
        raise IntegrationError("dingtalk_budget_exhausted", "预算", 429)
    monkeypatch.setattr(scan_scheduler, "resolve_watch_targets", targets)
    monkeypatch.setattr(scan_scheduler, "watch_workspace", walk)
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, settings)["status"] == "budget_exhausted"
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, settings)["walked"] == []
    assert len(calls) == 1


def test_legacy_failed_bridge_queue_is_quarantined(isolated, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("legacy queue must never walk production")
    monkeypatch.setattr(audit_bridge, "watch_workspace", forbidden)
    with isolated() as db:
        db.add(BridgeWalk(workspace_id="old", failures=347))
        db.add(BridgeWalk(workspace_id="new", failures=0))
        db.commit()
        assert audit_bridge.drain_bridge_walks(db, production(bridge_walks_enabled=True))["walks"] == []
        assert db.get(BridgeWalk, "old").status == "dead_letter"
        assert db.get(BridgeWalk, "new").status == "disabled"


def test_same_month_legacy_second_day_cannot_start_another_rotation(isolated, monkeypatch):
    class Clock(datetime):
        day = 10

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, cls.day, tzinfo=tz)
    monkeypatch.setattr(scan_scheduler, "datetime", Clock)
    calls = []

    async def targets(*args, **kwargs):
        calls.append("targets")
        return {"resolved": [{"workspace_id": "a", "name": "a", "space": {}}]}

    async def walk(*args, **kwargs):
        calls.append("walk")
        return SimpleNamespace(run_id="ok", status="succeeded", error_code="")
    monkeypatch.setattr(scan_scheduler, "resolve_watch_targets", targets)
    monkeypatch.setattr(scan_scheduler, "watch_workspace", walk)
    settings = production(full_scan_enabled=True, scan_days="10,24")
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, settings)["status"] == "completed"
    Clock.day = 24
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, settings)["walked"] == []
    assert calls == ["targets", "walk"]


def test_interrupted_task_consumes_attempt_and_waits_after_restart(isolated, monkeypatch):
    settings = production(full_scan_enabled=True, scan_days="1")
    month = datetime.now(api_usage.CST).strftime("%Y-%m")
    with isolated() as db:
        db.add(ScanCycle(month=month))
        db.add(ScanTask(month=month, workspace_id="crashed", status="running", attempts=3))
        db.commit()
    with isolated() as db:
        assert scan_scheduler.run_scan_slice(db, settings)["status"] == "completed_with_errors"
        task = db.get(ScanTask, (month, "crashed"))
        assert task.attempts == 3 and task.status == "dead_letter"
