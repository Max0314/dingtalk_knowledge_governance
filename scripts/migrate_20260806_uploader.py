"""One-shot migration: creator column, aggregate tables, primary-baseline flag.

Idempotent: safe to re-run. ALTER failures for an existing column are ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import Base, HistoricalSnapshot, SessionLocal, engine, init_db

PRIMARY_SNAPSHOT = "wiki-baseline-2026-08-05"


def main() -> None:
    init_db()  # creates uploader_month_stats / employee_map / any new tables
    ddl = ("ALTER TABLE historical_file_nodes ADD COLUMN creator_user_id "
           + ("VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"
              if engine.dialect.name == "mysql" else "VARCHAR(128) NOT NULL DEFAULT ''"))
    with engine.connect() as connection:
        try:
            connection.execute(text(ddl))
            connection.commit()
            print("added historical_file_nodes.creator_user_id")
        except Exception:
            print("creator_user_id already present")
    with SessionLocal() as db:
        snapshot = db.get(HistoricalSnapshot, PRIMARY_SNAPSHOT)
        if snapshot:
            definition = dict(snapshot.definition or {})
            if not definition.get("is_primary_baseline"):
                definition["is_primary_baseline"] = True
                snapshot.definition = definition
                db.commit()
                print(f"marked {PRIMARY_SNAPSHOT} as primary baseline")
            else:
                print("primary baseline flag already set")
        else:
            print("primary baseline snapshot missing (dev database?)")


if __name__ == "__main__":
    main()
