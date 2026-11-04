import asyncio
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import audit_bridge, audit_pull, api_usage, service
from app.api_usage import scan_budget
from app.config import Settings
from app.content import fetch_document_content
from app.db import (AuditState, Base, Document, FileAuditEvent, Notification, ReviewInstance, ReviewJob, Workspace, utcnow)


@pytest.fixture
def collected(tmp_path, monkeypatch):
    s = Settings(_env_file=None, metadata_only=True, content_extract_enabled=False, full_scan_enabled=True)
    e = create_engine("sqlite:///" + str(tmp_path / "metadata.db"))
    Base.metadata.create_all(e)
    sessions = sessionmaker(bind=e, autoflush=False)
    monkeypatch.setattr(api_usage, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add(Workspace(workspace_id="synthetic-space", name="C-Synthetic", watch_seeded=True))
    yield sessions, s
    e.dispose()


def test_complete_metadata_walk_never_queues_review(collected, monkeypatch):
    sessions, s = collected
    class Client:
        def __init__(self, *_):
            pass
        async def list_nodes(self, *args, **kwargs):
            return {"items": [{"node_id": "synthetic-native", "name": "synthetic", "extension": "adoc", "has_children": False,
                               "type": "file", "creator_id": "synthetic-source", "created_at": "2026-11-03T08:00:00Z"},
                              {"node_id": "synthetic-empty-folder", "name": "synthetic", "type": "folder", "has_children": False}], "next_token": ""}
    monkeypatch.setattr(service, "DingtalkClient", Client)
    with sessions() as db, scan_budget("2026-11"):
        run = asyncio.run(service.watch_workspace(db, s, "synthetic-space", {"name": "C-Synthetic", "root_node_id": "synthetic-root"}))
        assert run.status == "succeeded"
        assert db.get(Document, "synthetic-empty-folder").is_folder
        doc = db.get(Document, "synthetic-native")
        assert doc.source_creator_key == "synthetic-source" and doc.review_due_at is None
        assert db.scalars(select(ReviewJob)).all() == []
        assert db.scalars(select(ReviewInstance)).all() == []
        assert db.scalars(select(Notification)).all() == []
        assert asyncio.run(fetch_document_content(s, doc)) == ("", "metadata_only")


def test_metadata_delete_preserves_paused_tasks(collected):
    sessions, s = collected
    with sessions.begin() as db:
        db.add(Document(node_id="synthetic-doc", workspace_id="synthetic-space", name="synthetic", review_due_at=utcnow()))
        db.add(ReviewJob(job_id="synthetic-job", node_id="synthetic-doc", trigger="test", status="pending"))
        db.add(Notification(node_id="synthetic-doc", status="pending"))
    with sessions.begin() as db:
        event = FileAuditEvent(event_key="synthetic-event", biz_id="1", gmt_create=1, matched_node_id="synthetic-doc", match_status="confirmed")
        db.add(event)
        summary = {}
        audit_bridge._handle_delete_restore(db, s, event, "delete", summary)
        assert db.get(Document, "synthetic-doc").is_deleted
        assert db.get(Document, "synthetic-doc").review_due_at is not None
        assert db.get(ReviewJob, "synthetic-job").status == "pending"
        assert db.scalars(select(Notification)).one().status == "pending"


def test_metadata_audit_pages_resume_paired_cursor(collected, monkeypatch):
    sessions, s = collected
    monkeypatch.setattr(audit_pull, "PAGE_SIZE", 2)
    monkeypatch.setattr(audit_pull, "MAX_PAGES_PER_CYCLE", 1)
    stamp = int(utcnow().timestamp() * 1000) - 1000
    calls = []
    def row(n):
        return {"bizId": str(n), "gmtCreate": stamp + n, "actionView": "知识库上传", "operateModuleView": "团队空间", "resource": "synthetic.adoc"}
    async def page(client, settings, start, end, cg, cb):
        calls.append((start, end, cg, cb))
        return [row(1), row(2)] if cg is None else [row(3)]
    monkeypatch.setattr(audit_pull, "_audit_page", page)
    with sessions() as db:
        first = audit_pull.run_audit_pull(db, s)
        assert not first["window_complete"]
        a = db.get(AuditState, 1)
        assert (a.next_gmt_create, a.next_biz_id) == (stamp + 2, "2")
        end = a.window_end_ms
    with sessions() as db:
        second = audit_pull.run_audit_pull(db, s)
        a = db.get(AuditState, 1)
        assert second["window_complete"] and a.completed_end_ms == end
        assert a.window_start_ms is None
        assert len(db.scalars(select(FileAuditEvent)).all()) == 3
        assert calls[0][:2] == calls[1][:2]
        assert calls[1][2:] == (stamp + 2, "2")


def test_audit_page_failure_retains_committed_checkpoint(collected, monkeypatch):
    sessions, s = collected
    monkeypatch.setattr(audit_pull, "PAGE_SIZE", 1)
    monkeypatch.setattr(audit_pull, "MAX_PAGES_PER_CYCLE", 1)
    stamp = int(utcnow().timestamp() * 1000) - 1000
    async def page(client, settings, start, end, cg, cb):
        if cg is not None:
            raise RuntimeError("synthetic interruption")
        return [{"bizId": "1", "gmtCreate": stamp, "actionView": "知识库上传", "resource": "synthetic"}]
    monkeypatch.setattr(audit_pull, "_audit_page", page)
    with sessions() as db:
        audit_pull.run_audit_pull(db, s)
    with sessions() as db, pytest.raises(RuntimeError):
        audit_pull.run_audit_pull(db, s)
    with sessions() as db:
        state = db.get(AuditState, 1)
        assert state.next_biz_id == "1" and state.completed_end_ms is None
        assert len(db.scalars(select(FileAuditEvent)).all()) == 1


def test_known_node_event_refreshes_metadata_before_finishing(collected, monkeypatch):
    sessions, s = collected
    with sessions.begin() as db:
        db.add(Document(node_id="synthetic-known", workspace_id="synthetic-space", name="before", source_created_at="2026-11-01T00:00:00Z"))
        db.add(FileAuditEvent(event_key="synthetic-known-event", biz_id="123", gmt_create=int(utcnow().timestamp() * 1000),
               action_view="知识库重命名", module_view="团队空间", matched_node_id="synthetic-known", match_status="confirmed"))
    calls = []
    class Client:
        def __init__(self, *_):
            pass
        async def batch_query_wiki_nodes(self, ids, operator):
            calls.append(ids)
            return [{"node_id": "synthetic-known", "workspace_id": "synthetic-space", "name": "after", "has_children": False, "extension": "adoc"}]
    monkeypatch.setattr(audit_bridge, "DingtalkClient", Client)
    with sessions() as db:
        e = db.scalars(select(FileAuditEvent)).one()
        assert not audit_bridge._try_finish_confirmed(db, e, s, {}, set())
        audit_bridge._refresh_confirmed_metadata(db, s, {})
        assert e.processed and e.metadata_applied_at is not None
        assert db.get(Document, "synthetic-known").name == "after"
        assert calls == [["synthetic-known"]]
        assert db.scalars(select(ReviewJob)).all() == []


def test_same_time_audit_cursor_regression_is_rejected(collected, monkeypatch):
    sessions, s = collected
    monkeypatch.setattr(audit_pull, "PAGE_SIZE", 1)
    monkeypatch.setattr(audit_pull, "MAX_PAGES_PER_CYCLE", 1)
    stamp = int(utcnow().timestamp() * 1000) - 1000
    with sessions.begin() as db:
        db.add(AuditState(id=1, window_start_ms=stamp - 1000, window_end_ms=stamp + 1000, next_gmt_create=stamp, next_biz_id="100"))
    async def page(*_):
        return [{"bizId": "99", "gmtCreate": stamp, "actionView": "知识库上传", "resource": "synthetic"}]
    monkeypatch.setattr(audit_pull, "_audit_page", page)
    from app.integrations import IntegrationError
    with sessions() as db, pytest.raises(IntegrationError) as e:
        audit_pull.run_audit_pull(db, s)
    assert e.value.code == "audit_cursor_stalled"
