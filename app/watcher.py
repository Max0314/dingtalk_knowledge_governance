"""Dedicated slow-path Wiki walker.

Audit pulls, review jobs and notifications stay in app.worker. This process
owns targeted bridge walks and scheduled/seed scans so a multi-minute Wiki
tree traversal can never delay realtime ingestion.
"""
import logging
import time
import argparse
from datetime import datetime

from .config import get_settings
from .db import SessionLocal, init_db, ScanCycle
from .api_usage import CST
from .service import sweep_stale_runs
from .scan_scheduler import run_scan_slice, scan_lock

IDLE_CHECK_SECONDS = 600
logger = logging.getLogger("kg.watcher")


def cycle_is_runnable(db, month):
    if not month:
        return True
    if datetime.now(CST).strftime('%Y-%m') != month:
        return False
    cycle = db.get(ScanCycle, month)
    return bool(cycle and cycle.status == 'running')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Budgeted metadata calibration watcher')
    parser.add_argument('--cycle-month', help='Run only an already prepared YYYY-MM cycle, then exit')
    args = parser.parse_args(argv)
    if args.cycle_month:
        try:
            if datetime.strptime(args.cycle_month, '%Y-%m').strftime('%Y-%m') != args.cycle_month:
                raise ValueError()
        except ValueError:
            parser.error('--cycle-month must be YYYY-MM')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    init_db()
    if not settings.full_scan_enabled:
        logger.info("Full scans disabled; watcher exits without DingTalk requests")
        return
    with SessionLocal() as db:
        if not cycle_is_runnable(db, args.cycle_month):
            logger.info('Requested scan cycle is absent, terminal or outside its month; watcher exits')
            return
        with scan_lock(db) as got:
            swept = sweep_stale_runs(db, review_jobs=False, bridge_walks=False) if got else 0
        if swept:
            logger.info("swept %s stale scan run(s)", swept)

    while True:
        try:
            with SessionLocal() as db:
                if not cycle_is_runnable(db, args.cycle_month):
                    logger.info('Requested scan cycle finished or expired; watcher exits')
                    return
                db.commit()  # No read transaction retained during the scan.
                options = {'expected_month': args.cycle_month} if args.cycle_month else {}
                result = run_scan_slice(db, settings, **options)
            if result["walked"]:
                logger.info("monthly scan: %s", result)
        except Exception as exc:
            # Exception text may carry SQL/request parameters; log type only.
            logger.warning("monthly scan failed: %s", type(exc).__name__)
            result = {"walked": []}
        time.sleep(3 if result["walked"] else 60)


if __name__ == "__main__":
    main()
