"""Migration: documents.watch_misses column for the targeted workspace watcher."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine, init_db


def run(connection, ddl: str, label: str) -> None:
    try:
        connection.execute(text(ddl))
        connection.commit()
        print("ok:", label)
    except Exception:
        print("skip:", label)


def main() -> None:
    init_db()
    with engine.connect() as connection:
        run(connection, "ALTER TABLE documents ADD COLUMN watch_misses INTEGER NOT NULL DEFAULT 0",
            "documents.watch_misses")


if __name__ == "__main__":
    main()
