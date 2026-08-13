import logging
import time
from .audit_bridge import process_audit_events
from .audit_pull import run_audit_pull
from .config import get_settings
from .db import SessionLocal, init_db
from .notify import process_pending_notifications
from .service import process_next_job, run_watch_slice, seed_demo, sweep_stale_runs
from .stream import start_stream_consumer

logger = logging.getLogger("kg.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings(); init_db()
    start_stream_consumer(settings)
    with SessionLocal() as db:
        if settings.demo_mode: seed_demo(db)
        swept = sweep_stale_runs(db)
        if swept:
            logger.info("swept %s stale running sync runs", swept)
    # First watch tick shortly after boot so a fresh deploy proves itself
    # without waiting a full interval; later ticks follow the configured pace.
    next_watch_at = time.time() + 5 if settings.watch_workspaces else None
    next_audit_at = time.time() + 10 if settings.audit_pull_enabled else None
    while True:
        with SessionLocal() as db:
            processed = 0
            for _ in range(20):  # drain a batch between watch slices
                if not process_next_job(db, settings):
                    break
                processed += 1
            notified = process_pending_notifications(db, settings)
        if next_watch_at is not None and time.time() >= next_watch_at:
            # Slices, not full cycles: a few workspaces per pass, with review
            # jobs / notifications / audit drained in between — an org-wide
            # walk must not monopolize the loop for hours.
            try:
                with SessionLocal() as db:
                    sl = run_watch_slice(db, settings, batch=max(1, settings.watch_slice_size))
                if sl["walked"]:
                    logger.info("watch slice: %s (remaining %s/%s)",
                                [(r["name"], r["mode"], r["status"], r["documents_seen"], r["documents_new"],
                                  r["documents_changed"]) for r in sl["walked"]], sl["remaining"], sl["total"])
                if sl["cycle_completed"]:
                    if sl["unresolved"]:
                        logger.info("watch cycle complete, unresolved: %s", sl["unresolved"])
                    next_watch_at = time.time() + max(60, settings.watch_interval_seconds)
                else:
                    next_watch_at = time.time()  # continue after draining jobs and pushes
            except Exception:
                logger.exception("watch slice failed")
                next_watch_at = time.time() + 60
        if next_audit_at is not None and time.time() >= next_audit_at:
            try:
                with SessionLocal() as db:
                    audit = run_audit_pull(db, settings)
                logger.info("audit pull: %s", audit)
            except Exception:
                logger.exception("audit pull failed")
            if settings.bridge_enabled:
                try:
                    with SessionLocal() as db:
                        bridge = process_audit_events(db, settings)
                    if bridge["wiki_events"] or bridge["walks"]:
                        logger.info("audit bridge: %s", bridge)
                except Exception:
                    logger.exception("audit bridge failed")
            next_audit_at = time.time() + max(120, settings.audit_pull_interval_seconds)
        time.sleep(0.5 if processed or notified else 3)


if __name__ == "__main__":
    main()
