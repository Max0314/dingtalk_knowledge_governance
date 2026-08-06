"""Migration: detail columns on historical_file_nodes for the folder-inclusive
creator scan (category/url/size/word_count/modifier_user_id). Idempotent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine, init_db

MYSQL_COLUMNS = [
    ("category", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("url", "VARCHAR(1024) NOT NULL DEFAULT ''"),
    ("size", "INT NOT NULL DEFAULT 0"),
    ("word_count", "INT NOT NULL DEFAULT 0"),
    ("modifier_user_id", "VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
]
SQLITE_COLUMNS = [
    ("category", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("url", "VARCHAR(1024) NOT NULL DEFAULT ''"),
    ("size", "INTEGER NOT NULL DEFAULT 0"),
    ("word_count", "INTEGER NOT NULL DEFAULT 0"),
    ("modifier_user_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
]


def main() -> None:
    init_db()
    columns = MYSQL_COLUMNS if engine.dialect.name == "mysql" else SQLITE_COLUMNS
    with engine.connect() as connection:
        for name, ddl in columns:
            try:
                connection.execute(text(f"ALTER TABLE historical_file_nodes ADD COLUMN `{name}` {ddl}"
                                        if engine.dialect.name == "mysql" else
                                        f"ALTER TABLE historical_file_nodes ADD COLUMN {name} {ddl}"))
                connection.commit()
                print(f"added {name}")
            except Exception:
                print(f"{name} already present")


if __name__ == "__main__":
    main()
