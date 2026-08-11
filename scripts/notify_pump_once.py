"""Run the notification pump once, surfacing any exception it hides.

Usage: python scripts/notify_pump_once.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import get_settings
from app.db import Notification, SessionLocal, init_db
from app.notify import process_pending_notifications


def main() -> None:
    settings = get_settings()
    init_db()
    out: dict = {"notify_enabled": settings.notify_enabled,
                 "override": bool(settings.notify_override_user_id)}
    with SessionLocal() as db:
        try:
            out["attempted"] = process_pending_notifications(db, settings)
        except Exception:
            out["exception"] = traceback.format_exc()[-800:]
        rows = db.scalars(select(Notification).order_by(Notification.created_at.desc()).limit(3)).all()
        out["rows"] = [{"node_id": row.node_id, "status": row.status, "error_code": row.error_code,
                        "target_user_id": row.target_user_id} for row in rows]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
