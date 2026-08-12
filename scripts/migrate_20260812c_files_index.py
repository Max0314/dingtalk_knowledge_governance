"""Migration: ix_hfn_snapshot_created — newest-first paging index for the
merged document list (/api/v1/files)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine, init_db


def main() -> None:
    init_db()
    with engine.connect() as connection:
        try:
            connection.execute(text(
                "CREATE INDEX ix_hfn_snapshot_created ON historical_file_nodes (snapshot_id, source_created_at)"))
            connection.commit()
            print("ok: ix_hfn_snapshot_created")
        except Exception as exc:  # already exists → fine
            print("skip:", str(exc).splitlines()[0][:120])


if __name__ == "__main__":
    main()
