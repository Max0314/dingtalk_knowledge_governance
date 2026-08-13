"""人工补偿入口：把 dead_letter_* 死信事件重新放回桥接重试队列。

场景：搜索索引长期滞后、权限修复后补挂下载键等。重开后事件按正常
生命周期重试（定位额度公平轮转），到期仍失败会再次转死信。

    docker compose exec -T api python scripts/reopen_dead_letters.py            # 预览
    docker compose exec -T api python scripts/reopen_dead_letters.py --apply    # 执行
    可选 --days N 只重开最近 N 天的死信（默认 7）。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import FileAuditEvent, SessionLocal, init_db, utcnow


def main() -> None:
    apply_changes = "--apply" in sys.argv
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    threshold_ms = int((utcnow() - timedelta(days=days)).timestamp() * 1000)
    init_db()
    with SessionLocal() as db:
        rows = db.scalars(select(FileAuditEvent)
                          .where(FileAuditEvent.resolution.like("dead_letter%"),
                                 FileAuditEvent.gmt_create >= threshold_ms)).all()
        print(json.dumps({"mode": "APPLIED" if apply_changes else "DRY-RUN（加 --apply 执行）",
                          "days": days, "dead_letters_to_reopen": len(rows)}, ensure_ascii=False))
        for event in rows[:50]:
            print(f"[死信] {event.resolution} · {event.resource or '?'} · biz={event.biz_id}")
        if not apply_changes:
            return
        now = utcnow()
        for event in rows:
            event.processed = False
            event.resolution = ""
            event.last_attempt_at = None
            event.received_at = now  # 重试窗口基准重置：获得完整的 48h 新生命周期
        db.commit()
        print("done")


if __name__ == "__main__":
    main()
