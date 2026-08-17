import logging
import time
from .audit_bridge import process_audit_events
from .audit_pull import run_audit_pull
from .config import get_settings
from .db import SessionLocal, init_db
from .notify import process_pending_notifications
from .service import (harvest_due_reviews, mark_scan_cycle_complete, process_next_job, run_watch_slice, seed_demo,
                      sweep_stale_runs, watch_scan_decision)
from .stream import start_stream_consumer

IDLE_CHECK_SECONDS = 600  # 非扫描期：十分钟看一眼日历，零外部调用

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
        swept = sweep_stale_runs(db)
        if swept:
            logger.info("swept %s stale running sync runs", swept)
    # Audit gets the earlier boot tick and runs FIRST in the loop: it is the
    # storage-key and change-signal source, and must not queue behind a long
    # walk or a batch of model reviews (codex 2026-08-13 finding).
    next_watch_at = time.time() + 10 if settings.watch_workspaces else None
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
                        bridge = process_audit_events(db, settings)
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
        if next_watch_at is not None and time.time() >= next_watch_at:
            # 巡走只在两种情况推进：首轮补种未完成（连续切片），或到达
            # 每月计划扫描日（默认 10/24，Asia/Shanghai）。其余时间空转看
            # 日历——日常变化发现由审计增量拉取 + 桥接定向巡走负责
            # （2026-08-14 决策：全量扫描一个月两次足够）。
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
                                    [(r["name"], r["mode"], r["status"], r["documents_seen"], r["documents_new"],
                                      r["documents_changed"]) for r in sl["walked"]], sl["remaining"], sl["total"])
                    if sl["cycle_completed"]:
                        if sl["unresolved"]:
                            logger.info("watch cycle complete, unresolved: %s", sl["unresolved"])
                        with SessionLocal() as db:
                            # 传本轮真实成员：整轮缺席的注册库自动标记不可见
                            mark_scan_cycle_complete(db, settings,
                                                     set(sl.get("cycle_workspace_ids") or []))
                        next_watch_at = time.time() + max(60, settings.watch_interval_seconds)
                    else:
                        next_watch_at = time.time()  # continue after draining jobs and pushes
            except Exception:
                logger.exception("watch slice failed")
                next_watch_at = time.time() + 60
        time.sleep(0.5 if processed or notified else 3)


if __name__ == "__main__":
    main()
