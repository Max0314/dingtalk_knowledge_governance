"""Advance the durable monthly calibration by one slice. Requires explicit
KG_FULL_SCAN_ENABLED; repeat calls never rescan completed monthly tasks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.service import run_watch_cycle


def main() -> None:
    settings = get_settings()
    if not settings.watch_workspaces:
        print(json.dumps({"error": "KG_WATCH_WORKSPACES not configured"}, ensure_ascii=False))
        return
    init_db()
    with SessionLocal() as db:
        summary = run_watch_cycle(db, settings)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
