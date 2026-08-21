"""Dedicated slow-path Wiki walker.

Audit pulls, review jobs and notifications stay in app.worker. This process
owns targeted bridge walks and scheduled/seed scans so a multi-minute Wiki
tree traversal can never delay realtime ingestion.
"""
import logging
import time

from .audit_bridge import drain_bridge_walks
from .config import get_settings
from .db import SessionLocal, init_db
from .service import (mark_scan_cycle_complete, run_watch_slice, sweep_stale_runs,
                      watch_scan_decision)

IDLE_CHECK_SECONDS = 600
logger = logging.getLogger("kg.watcher")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        swept = sweep_stale_runs(db, review_jobs=False, bridge_walks=False)
        if swept:
            logger.info("swept %s stale scan run(s)", swept)

    next_watch_at = time.time() + 5 if settings.watch_workspaces else None
    while True:
        # Targeted walks always outrank org-wide seed/monthly work. The row is
        # durable, so a watcher restart neither loses nor duplicates the ask.
        if settings.bridge_enabled:
            try:
                with SessionLocal() as db:
                    bridge = drain_bridge_walks(db, settings)
                if bridge["walks"]:
                    logger.info("bridge walks: %s", bridge)
                    time.sleep(0.5)
                    continue
            except Exception:
                logger.exception("bridge walk failed")
                time.sleep(3)
                continue

        if next_watch_at is not None and time.time() >= next_watch_at:
            try:
                with SessionLocal() as db:
                    decision = watch_scan_decision(db, settings)
                if decision == "idle":
                    next_watch_at = time.time() + IDLE_CHECK_SECONDS
                else:
                    with SessionLocal() as db:
                        sl = run_watch_slice(db, settings, batch=max(1, settings.watch_slice_size))
                    if sl["walked"]:
                        logger.info("watch slice: %s (remaining %s/%s)",
                                    [(r["name"], r["mode"], r["status"], r["documents_seen"],
                                      r["documents_new"], r["documents_changed"])
                                     for r in sl["walked"]], sl["remaining"], sl["total"])
                    if sl["cycle_completed"]:
                        if sl["unresolved"]:
                            logger.info("watch cycle complete, unresolved: %s", sl["unresolved"])
                        with SessionLocal() as db:
                            mark_scan_cycle_complete(db, settings,
                                                     set(sl.get("cycle_workspace_ids") or []))
                        next_watch_at = time.time() + max(60, settings.watch_interval_seconds)
                    else:
                        next_watch_at = time.time()
            except Exception:
                logger.exception("watch slice failed")
                next_watch_at = time.time() + 60
        time.sleep(3)


if __name__ == "__main__":
    main()
