"""One authorized incident window; bounded enrollment, immutable prior attempts."""
import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, or_

from .db import (FileAuditEvent, MetadataCompensationItem, MetadataCompensationRun,
                 SessionLocal, utcnow)
from .metadata_mode import naive

REOPENABLE = frozenset({'metadata_event_expired', 'dead_letter_locator_limit',
    'dead_letter_metadata_unavailable', 'dead_letter_unmatched', 'dead_letter_no_doc', 'dead_letter_interrupted_limit'})


def _mode(settings):
    if not settings.metadata_only or any((settings.review_enabled, settings.notify_enabled,
                                         settings.content_extract_enabled, settings.model_allow_content_transfer)):
        raise ValueError('compensation_requires_paused_metadata_mode')


def prepare(db, settings, run_id, since_ms, until_ms, *, apply=False):
    _mode(settings)
    if str(UUID(run_id)) != run_id:
        raise ValueError('invalid_compensation_id')
    prior = db.get(MetadataCompensationRun, run_id)
    if prior:
        if (prior.since_ms, prior.until_ms) != (since_ms, until_ms):
            raise ValueError('compensation_scope_conflict')
        return {'status': prior.status, 'runId': run_id, 'enrolled': prior.enrolled}
    now_ms = int(utcnow().timestamp()*1000)
    if not now_ms-7*86400000 <= since_ms < until_ms <= now_ms+300000:
        raise ValueError('compensation_requires_explicit_recent_window')
    if db.scalar(select(MetadataCompensationRun.run_id).where(MetadataCompensationRun.status=='running').limit(1)):
        raise ValueError('another_compensation_is_running')
    lo, hi = db.execute(select(func.min(FileAuditEvent.id), func.max(FileAuditEvent.id)).where(
        FileAuditEvent.gmt_create >= since_ms, FileAuditEvent.gmt_create <= until_ms)).one()
    result = {'status': 'prepared' if apply else 'preview', 'runId': run_id,
              'sinceMs': since_ms, 'untilMs': until_ms, 'maxEventId': hi or 0,
              'batchSize': 100, 'resumesScanTasks': False}
    if apply:
        db.add(MetadataCompensationRun(run_id=run_id, since_ms=since_ms, until_ms=until_ms,
            cursor_id=(lo-1) if lo else 0, max_event_id=hi or 0))
    return result


def advance(db, settings, *, limit=100):
    """Called inside the shared bridge lock, before its normal event processing."""
    _mode(settings)
    run = db.scalar(select(MetadataCompensationRun).where(MetadataCompensationRun.status=='running')
                    .order_by(MetadataCompensationRun.created_at).limit(1))
    if run is None:
        return 0
    from .audit_bridge import _action_kind, _remote_action_filter, IGNORE_ACTIONS, RESTORE_ACTIONS, DELETE_ACTIONS
    view = func.coalesce(FileAuditEvent.action_view, '')
    local = or_(*(view.contains(word) for word in RESTORE_ACTIONS + DELETE_ACTIONS))
    ignored = or_(*(view.contains(word) for word in IGNORE_ACTIONS))
    rows = list(db.scalars(select(FileAuditEvent).where(
        FileAuditEvent.id > run.cursor_id, FileAuditEvent.id <= run.max_event_id,
        FileAuditEvent.gmt_create >= run.since_ms, FileAuditEvent.gmt_create <= run.until_ms,
        FileAuditEvent.retry_started_at.is_(None), FileAuditEvent.metadata_applied_at.is_(None),
        or_(FileAuditEvent.processed.is_(False), FileAuditEvent.resolution.in_(REOPENABLE)),
        or_(FileAuditEvent.action_view.contains('知识库'), FileAuditEvent.module_view=='团队空间'),
        or_(_remote_action_filter(), local & ~ignored))
        .order_by(FileAuditEvent.id).limit(min(max(limit, 1), 100))))
    existing = set(db.scalars(select(MetadataCompensationItem.event_id).where(
        MetadataCompensationItem.event_id.in_([e.id for e in rows])))) if rows else set()
    now = utcnow()
    enrolled = 0
    for event in rows:
        if (not run.since_ms <= event.gmt_create <= run.until_ms or event.id in existing
                or event.metadata_applied_at is not None or event.retry_started_at is not None
                or (event.processed and event.resolution not in REOPENABLE)
                or _action_kind(event) not in {'review', 'modify', 'metadata', 'delete', 'restore'}):
            continue
        db.add(MetadataCompensationItem(event_id=event.id, run_id=run.run_id,
            prior_attempts=event.locate_attempts, prior_processed=event.processed,
            prior_resolution=event.resolution, prior_next_attempt_at=event.next_locate_at))
        event.processed, event.resolution, event.locate_attempts = False, '', 0
        event.last_attempt_at, event.retry_started_at = None, now
        # Preserve any still valid provider/local cooldown. Only elapsed waits
        # are cleared; a new allowance never bypasses Retry-After.
        if event.next_locate_at and naive(event.next_locate_at) <= naive(now):
            event.next_locate_at = None
        enrolled += 1
    run.cursor_id = rows[-1].id if rows else run.max_event_id
    run.enrolled += enrolled
    if run.cursor_id >= run.max_event_id or len(rows) < min(max(limit, 1), 100):
        run.cursor_id = run.max_event_id
        run.status, run.finished_at = 'completed', now
    db.commit()
    return enrolled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    moments = [datetime.fromisoformat(x.replace('Z', '+00:00')) for x in (args.since, args.until)]
    if any(x.tzinfo is None for x in moments):
        parser.error('timestamps must include timezone')
    from .config import get_settings
    from .dingtalk_rate import shared_lock
    settings = get_settings()
    with shared_lock(settings, 'metadata-bridge') as handle:
        if handle is None:
            raise SystemExit('metadata bridge busy; retry preparation later')
        with SessionLocal() as db:
            result = prepare(db, settings, args.run_id, *(int(x.timestamp()*1000) for x in moments), apply=args.apply)
            db.commit()
            print(json.dumps(result))


if __name__ == '__main__':
    main()
