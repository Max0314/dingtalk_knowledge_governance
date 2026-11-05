"""Resumable metadata calibration. No database transaction spans an HTTP call."""
import hashlib
import asyncio
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from .db import Document, SyncRun, Workspace, utcnow
from .integrations import IntegrationError
from .metadata_mode import database_now, naive
from .scan_models import ScanDirectory, ScanPageToken, ScanProgress, ScanSeen
from .dingtalk_errors import diagnostic_detail


async def _directory_page(client, settings, workspace_id, parent, token):
    # Short retries stay on the exact same page, consume real API budgets,
    # and never hold a SQL transaction. Long Retry-After goes to the scheduler.
    async with asyncio.timeout(30):
        for attempt in range(3):
            try:
                return await client.list_nodes(workspace_id, settings.dingtalk_sync_operator_id, parent, token, max_results=30)
            except IntegrationError as exc:
                delay = getattr(exc, 'retry_after', None) or 2 ** (attempt+1)
                if not getattr(exc, 'retryable', False) or attempt == 2 or delay > 10:
                    raise
                await asyncio.sleep(delay)


def cleanup_completed(db, limit=500):
    from datetime import timedelta
    from sqlalchemy import delete, tuple_
    from .db import ScanCycle
    run_id = db.scalar(select(ScanProgress.run_id).join(SyncRun, SyncRun.run_id == ScanProgress.run_id)
        .join(ScanCycle, ScanCycle.month == ScanProgress.cycle_month)
        .where(ScanProgress.phase == 'complete', SyncRun.status == 'succeeded', SyncRun.finished_at < utcnow()-timedelta(days=7),
               ScanCycle.status.in_(('completed', 'completed_with_errors'))).limit(1))
    if not run_id:
        db.rollback()
        return 0
    count = 0
    for model in (ScanSeen, ScanPageToken, ScanDirectory):
        pk = list(model.__table__.primary_key)
        keys = db.execute(select(*pk).where(model.run_id == run_id).limit(limit-count)).all()
        if keys:
            db.execute(delete(model).where(tuple_(*pk).in_(keys)).execution_options(synchronize_session=False))
            count += len(keys)
        if count >= limit:
            break
    if count == 0:
        db.execute(delete(ScanProgress).where(ScanProgress.run_id == run_id))
    db.commit()
    return count


@asynccontextmanager
async def _connection(client):
    if hasattr(client, 'reuse_http'):
        async with client.reuse_http():
            yield
    else:  # Deterministic provider substitutes used by tests.
        yield


def _token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _validated(page, workspace_id):
    if not isinstance(page, dict) or not isinstance(page.get('items'), list) or not isinstance(page.get('next_token', ''), str):
        raise IntegrationError('scan_page_invalid', '目录分页响应格式异常。', 502)
    items = page['items']
    # Provider cap is 30; allow an exact duplicate response from old API variants.
    if len(items) > 60 or len(page.get('next_token', '')) > 2048:
        raise IntegrationError('scan_page_invalid', '目录分页响应超过上限。', 502)
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('node_id'), str) or not 0 < len(item['node_id']) <= 128 or not isinstance(item.get('name'), str) or item.get('workspace_id') not in (None, '', workspace_id):
            raise IntegrationError('scan_node_invalid', '目录节点标识或所属知识库异常。', 502)
        if not isinstance(item.get('has_children', False), bool):
            raise IntegrationError('scan_node_invalid', '目录子节点标识异常。', 502)
        item.setdefault('has_children', False)
    return items, page.get('next_token', '')


async def watch_paged(db, settings, workspace_id, space, mode, client, resume_run_id=None):
    from sqlalchemy.orm import Session
    db.commit()
    # Pin one physical connection across page commits. It has no transaction
    # during HTTP, and session settings are restored on this same connection.
    with db.get_bind().connect() as conn:
        with Session(bind=conn, autoflush=False, expire_on_commit=False) as scoped:
            result = await _watch_paged(scoped, settings, workspace_id, space, mode, client, resume_run_id)
            scoped.refresh(result)
            scoped.commit()
            scoped.expunge(result)
            return result


async def _watch_paged(db, settings, workspace_id, space, mode, client, resume_run_id=None):
    from .service import _upsert_document, workspace_name_is_ignored
    run_id = resume_run_id or str(uuid.uuid4())
    run = db.get(SyncRun, run_id)
    progress = db.get(ScanProgress, run_id)
    if run and run.workspace_id != workspace_id:
        raise IntegrationError('scan_checkpoint_conflict', '校准断点与知识库不一致。', 409)
    if progress and progress.workspace_id != workspace_id:
        raise IntegrationError('scan_checkpoint_conflict', '校准断点与知识库不一致。', 409)
    if progress and progress.phase == 'complete':
        run.status, run.error_code, run.error_detail = 'succeeded', '', ''
        run.finished_at = run.finished_at or utcnow()
        db.commit()
        return run
    if run and run.status == 'succeeded':
        return run
    if run is None:
        run = SyncRun(run_id=run_id, status='running', mode=mode, workspace_id=workspace_id, workspace_name=(space or {}).get('name', ''))
        db.add(run)
    run.status, run.error_code, run.error_detail, run.finished_at = 'running', '', '', None
    db.commit()
    old_lock_wait = None
    try:
        if db.bind.dialect.name == 'mysql':
            old_lock_wait = db.scalar(text('SELECT @@SESSION.innodb_lock_wait_timeout'))
            db.execute(text('SET SESSION innodb_lock_wait_timeout=3'))
            db.commit()
        async with _connection(client):
            if progress is None:
                raw = dict(space or {})
                db.commit()
                if not raw.get('root_node_id'):
                    try:
                        raw = await asyncio.wait_for(client.workspace_detail(workspace_id, settings.dingtalk_sync_operator_id), timeout=30)
                    except IntegrationError as exc:
                        if exc.status_code != 404 and exc.code != 'dingtalk_permission_denied':
                            raise
                        raw = None  # Personal-library detail compatibility only.
                    if not raw or not raw.get('root_node_id'):
                        token, tokens = '', set()
                        for _ in range(1000):
                            listing = await asyncio.wait_for(client.list_workspaces(settings.dingtalk_sync_operator_id, token), timeout=30)
                            raw = next((v for v in listing['items'] if v['workspace_id'] == workspace_id), None)
                            token = listing.get('next_token', '')
                            if raw or not token:
                                break
                            if token in tokens:
                                raise IntegrationError('scan_pagination_cycle', '知识库列表分页游标重复。', 502)
                            tokens.add(token)
                if not raw or not raw.get('root_node_id'):
                    raise IntegrationError('workspace_not_visible', '缺少可验证的知识库根目录。', 404)
                if workspace_name_is_ignored(settings, raw.get('name', '')):
                    run.status, run.error_code, run.finished_at = 'skipped', 'workspace_prefix_ignored', utcnow()
                    db.commit()
                    return run
                ws = db.get(Workspace, workspace_id)
                seeding = ws is None or not ws.watch_seeded or ws.name == ws.workspace_id
                if ws is None:
                    ws = Workspace(workspace_id=workspace_id, name=raw.get('name') or workspace_id)
                    db.add(ws)
                for field in ('name', 'description', 'url'):
                    if raw.get(field):
                        setattr(ws, field, raw[field])
                for field, key in [('source_created_at', 'created_at'), ('source_updated_at', 'updated_at'), ('creator_key', 'creator_id')]:
                    if raw.get(key):
                        setattr(ws, field, raw[key])
                ws.synced_at = ws.metadata_confirmed_at = utcnow()
                ws.is_active, ws.unreachable_misses = True, 0
                run.workspace_name, run.workspaces_seen = ws.name, 1
                run.mode = f'{mode}_seed' if seeding else mode
                run.collection_started_at = database_now(db)
                from .api_usage import scan_month, CST
                from datetime import datetime
                progress = ScanProgress(run_id=run_id, workspace_id=workspace_id, seeding=seeding,
                                        cycle_month=scan_month.get() or datetime.now(CST).strftime('%Y-%m'))
                db.add_all([progress, ScanDirectory(run_id=run_id, parent_id=raw['root_node_id'])])
                db.commit()
            started = run.collection_started_at
            for _ in range(settings.scan_slice_pages):
                progress = db.get(ScanProgress, run_id)
                if progress.phase == 'walk':
                    directory = db.scalar(select(ScanDirectory).where(ScanDirectory.run_id == run_id, ScanDirectory.done.is_(False))
                                          .order_by(ScanDirectory.parent_id).limit(1))
                    if directory is None:
                        progress.phase = 'reconcile'
                        db.commit()
                        continue
                    parent, incoming = directory.parent_id, directory.next_token
                    # Every SQL transaction (including read snapshots) ends before remote I/O.
                    db.commit()
                    page = await _directory_page(client, settings, workspace_id, parent, incoming)
                    items, next_token = _validated(page, workspace_id)
                    if next_token and (next_token == incoming or db.get(ScanPageToken, (run_id, parent, _token(next_token)))):
                        raise IntegrationError('scan_pagination_cycle', '目录分页游标重复，校准未完成。', 502)
                    ids = {r['node_id'] for r in items}
                    seen = set(db.scalars(select(ScanSeen.node_id).where(ScanSeen.run_id == run_id, ScanSeen.node_id.in_(ids))))
                    run = db.get(SyncRun, run_id)
                    for item in items:
                        node = item['node_id']
                        if node in seen:
                            continue
                        seen.add(node)
                        await _upsert_document(db, settings, run, workspace_id, item, enqueue=False,
                            trigger_new='watch', trigger_change='watch_change', parent_node_id=parent, scan_started_at=started)
                        db.add(ScanSeen(run_id=run_id, node_id=node))
                        if item.get('has_children') and not db.get(ScanDirectory, (run_id, node)):
                            db.add(ScanDirectory(run_id=run_id, parent_id=node))
                    directory = db.get(ScanDirectory, (run_id, parent))
                    directory.next_token, directory.done = next_token, not bool(next_token)
                    db.add(ScanPageToken(run_id=run_id, parent_id=parent, token_hash=_token(incoming)))
                    db.commit()  # Nodes + seen IDs + next page advance together.
                elif progress.phase == 'reconcile':
                    docs = db.scalars(select(Document).where(Document.workspace_id == workspace_id, Document.node_id > progress.delete_cursor,
                            Document.is_deleted.is_(False)).order_by(Document.node_id).limit(100).with_for_update()).all()
                    if docs:
                        seen = set(db.scalars(select(ScanSeen.node_id).where(ScanSeen.run_id == run_id, ScanSeen.node_id.in_([d.node_id for d in docs]))))
                        for doc in docs:
                            if doc.node_id in seen or doc.metadata_applied_at and naive(doc.metadata_applied_at) > naive(started):
                                continue
                            doc.watch_misses += 1
                            if doc.watch_misses >= max(1, settings.watch_delete_misses):
                                doc.is_deleted, doc.deleted_at, doc.metadata_applied_at = True, utcnow(), database_now(db)
                        progress.delete_cursor = docs[-1].node_id
                        db.commit()  # Absence cursor and misses cannot be applied twice after restart.
                    else:
                        ws = db.get(Workspace, workspace_id)
                        ws.watch_seeded = True
                        progress.phase = 'complete'
                        run = db.get(SyncRun, run_id)
                        run.status, run.error_code, run.error_detail, run.finished_at = 'succeeded', '', '', utcnow()
                        db.commit()
                        return run
            return db.get(SyncRun, run_id)
    except Exception as exc:
        # Also resets a Session whose commit succeeded but acknowledgement or
        # after_commit handling failed. The pinned physical connection remains.
        db.close()
        run = db.get(SyncRun, run_id)
        completed = db.get(ScanProgress, run_id)
        if completed and completed.phase == 'complete':
            run.status, run.error_code, run.error_detail = 'succeeded', '', ''
            run.finished_at = run.finished_at or utcnow()
            db.commit()
            return run
        code = exc.code if isinstance(exc, IntegrationError) else 'scan_page_failed'
        run.status = 'paused' if code == 'dingtalk_scan_daily_reserved' else 'failed'
        run.error_code = f'{code}:{exc.status_code}'[:64] if isinstance(exc, IntegrationError) else code
        run.error_detail = diagnostic_detail(exc)
        run.finished_at = utcnow()
        db.commit()
        return run
    finally:
        if old_lock_wait is not None:
            db.rollback()
            db.execute(text('SET SESSION innodb_lock_wait_timeout=:value'), {'value': old_lock_wait})
            db.commit()
