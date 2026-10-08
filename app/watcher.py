"""Dedicated slow-path Wiki walker.

Audit pulls, review jobs and notifications stay in app.worker. This process
owns targeted bridge walks and scheduled/seed scans so a multi-minute Wiki
tree traversal can never delay realtime ingestion.
"""
import logging
import time

from .config import get_settings
from .db import SessionLocal, init_db
from .service import sweep_stale_runs
from .scan_scheduler import run_scan_slice, scan_lock

IDLE_CHECK_SECONDS = 600
logger = logging.getLogger("kg.watcher")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    init_db()
    if not settings.full_scan_enabled:
        logger.info("Full scans disabled; watcher exits without DingTalk requests")
        return
    with SessionLocal() as db:
        with scan_lock(db) as got:
            swept = sweep_stale_runs(db, review_jobs=False, bridge_walks=False) if got else 0
        if swept:
            logger.info("swept %s stale scan run(s)", swept)

    while True:
        try:
            with SessionLocal() as db:
                result = run_scan_slice(db, settings)
            if result["walked"]:
                logger.info("monthly scan: %s", result)
        except Exception as exc:
            # Exception text may carry SQL/request parameters; log type only.
            logger.warning("monthly scan failed: %s", type(exc).__name__)
            result = {"walked": []}
        time.sleep(3 if result["walked"] else 60)


if __name__ == "__main__":
    main()
