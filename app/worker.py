import logging
import time
from .audit_bridge import process_audit_events
from .audit_pull import run_audit_pull
from .config import get_settings
from .db import SessionLocal, init_db
from .notify import process_pending_notifications
from .service import harvest_due_reviews, process_next_job, seed_demo, sweep_stale_runs
from .stream import start_stream_consumer

logger = logging.getLogger("kg.worker")


class _DependencyCredentialFilter(logging.Filter):
    """Drop dependency INFO records that can contain signed URLs/tickets."""

    SENSITIVE_LOGGERS = {"httpx", "dingtalk_stream.client"}

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name not in self.SENSITIVE_LOGGERS


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx INFO includes full URLs (the .adoc export URL carries a temporary
    # signature); DingTalk Stream INFO includes its connection ticket. Keep
    # application summaries at INFO, but never persist those credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("dingtalk_stream.client").setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_DependencyCredentialFilter())
    settings = get_settings(); init_db()
    start_stream_consumer(settings)
    with SessionLocal() as db:
        if settings.demo_mode: seed_demo(db)
        # The scan worker owns SyncRun rows. A realtime restart must never mark
        # its concurrently running scan as interrupted or delete queued walks.
        swept = sweep_stale_runs(db, sync_runs=False, bridge_walks=False)
        if swept:
            logger.info("requeued %s interrupted realtime job(s)", swept)
    # Audit gets the earlier boot tick and runs FIRST in the loop: it is the
    # storage-key and change-signal source, and must not queue behind a long
    # walk or a batch of model reviews (codex 2026-08-13 finding).
    next_audit_at = time.time() + 5 if settings.audit_pull_enabled else None
    while True:
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
                        # Slow targeted walks are persisted for app.watcher;
                        # realtime audit ingestion never executes a full tree.
                        bridge = process_audit_events(db, settings, drain_walks=False)
                    if bridge["wiki_events"] or bridge["walks"]:
                        logger.info("audit bridge: %s", bridge)
                except Exception:
                    logger.exception("audit bridge failed")
            next_audit_at = time.time() + max(120, settings.audit_pull_interval_seconds)
        with SessionLocal() as db:
            harvested = harvest_due_reviews(db, settings)  # 修改合并窗到点的文档入队
            if harvested:
                logger.info("merge-window harvest queued %s review(s)", harvested)
            processed = 0
            for _ in range(3):  # 小额度：模型评审单笔可达 2 分钟，不许垄断循环
                if not process_next_job(db, settings):
                    break
                processed += 1
            notified = process_pending_notifications(db, settings)
        time.sleep(0.5 if processed or notified else 3)


if __name__ == "__main__":
    main()
