"""Migration: model-config parameter columns, history table, creator index."""
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
    init_db()  # creates model_config_history + any new tables/indexes on fresh DBs
    mysql = engine.dialect.name == "mysql"
    with engine.connect() as connection:
        run(connection, "ALTER TABLE model_configs ADD COLUMN api_key TEXT" + (" NOT NULL" if False else ""), "model_configs.api_key")
        run(connection, "ALTER TABLE model_configs ADD COLUMN temperature FLOAT NULL", "model_configs.temperature")
        run(connection, "ALTER TABLE model_configs ADD COLUMN thinking_mode VARCHAR(16) NOT NULL DEFAULT ''", "model_configs.thinking_mode")
        if mysql:
            run(connection, "UPDATE model_configs SET api_key='' WHERE api_key IS NULL", "api_key backfill")
            run(connection, "CREATE INDEX ix_hfn_snapshot_creator ON historical_file_nodes (snapshot_id, creator_user_id)", "creator index")
        else:
            run(connection, "CREATE INDEX IF NOT EXISTS ix_hfn_snapshot_creator ON historical_file_nodes (snapshot_id, creator_user_id)", "creator index")


if __name__ == "__main__":
    main()
