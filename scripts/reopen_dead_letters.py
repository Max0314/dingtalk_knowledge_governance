"""人工补偿入口：把选定的审计事件重新放回桥接重试队列。

默认只选择 dead_letter_*；也可在动作白名单扩展后用
``--unknown-action 完整动作名`` 精确重开此前被忽略的该动作。重开后事件
按正常生命周期重试，到期仍失败会再次转死信。

    docker compose exec -T api python scripts/reopen_dead_letters.py            # 预览
    docker compose exec -T api python scripts/reopen_dead_letters.py --apply    # 执行
    docker compose exec -T api python scripts/reopen_dead_letters.py \
      --unknown-action 覆盖文件                                                # 预览
    可选 --days N 只重开最近 N 天的死信（默认 7）。

重要：2026-08-17 决策是不补评历史无正文欠账。本工具不会自动运行；不要用
它批量重开那批历史事件。仅在权限/白名单问题已修复并明确选定范围后执行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import FileAuditEvent, SessionLocal, init_db, utcnow


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="预览或重开审计死信/已确认的未知动作")
    parser.add_argument("--apply", action="store_true", help="执行；缺省仅预览")
    parser.add_argument("--days", type=int, default=7, help="只选择最近 N 天（默认 7）")
    parser.add_argument("--unknown-action", default="", help="精确选择 ignored_unknown_action 的完整动作名")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 必须大于 0")
    return args


def main() -> None:
    args = _arguments()
    threshold_ms = int((utcnow() - timedelta(days=args.days)).timestamp() * 1000)
    if args.unknown_action:
        # Fail closed: replay is meaningful only after this exact action has
        # been classified in the current code version.
        from app.audit_bridge import _action_kind

        kind = _action_kind(SimpleNamespace(action_view=args.unknown_action))
        if kind == "unknown":
            raise SystemExit("该动作在当前代码中仍未进入白名单，拒绝重开。")
    init_db()
    with SessionLocal() as db:
        stmt = select(FileAuditEvent).where(FileAuditEvent.gmt_create >= threshold_ms)
        if args.unknown_action:
            stmt = stmt.where(FileAuditEvent.resolution == "ignored_unknown_action",
                              FileAuditEvent.action_view == args.unknown_action)
            selection = f"unknown_action:{args.unknown_action}"
        else:
            stmt = stmt.where(FileAuditEvent.resolution.like("dead_letter%"))
            selection = "dead_letter"
        rows = db.scalars(stmt.order_by(FileAuditEvent.gmt_create)).all()
        print(json.dumps({"mode": "APPLIED" if args.apply else "DRY-RUN（加 --apply 执行）",
                          "days": args.days, "selection": selection,
                          "events_to_reopen": len(rows)}, ensure_ascii=False))
        for event in rows[:50]:
            # Do not print document names, bizIds, people or other source data.
            print(f"[事件] id={event.id} · {event.resolution} · {event.action_view or '?'}")
        if not args.apply:
            return
        now = utcnow()
        for event in rows:
            event.processed = False
            event.resolution = ""
            event.last_attempt_at = None
            # 重试窗口基准用独立列重置；received_at 是原始入库审计字段，不动
            event.retry_started_at = now
        db.commit()
        print("done")


if __name__ == "__main__":
    main()
