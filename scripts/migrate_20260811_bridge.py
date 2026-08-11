"""Migration: documents.file_class column (+ backfill) and space_map table.

Backfill classifies all existing mirrored documents so dashboards and the
review gate see a class on day one; the space_map table itself is created by
init_db's create_all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.db import Document, SessionLocal, engine, init_db
from app.fileclass import classify


def run(connection, ddl: str, label: str) -> None:
    try:
        connection.execute(text(ddl))
        connection.commit()
        print("ok:", label)
    except Exception:
        print("skip:", label)


def main() -> None:
    init_db()  # creates space_map on both backends
    with engine.connect() as connection:
        run(connection, "ALTER TABLE documents ADD COLUMN file_class VARCHAR(32) NOT NULL DEFAULT ''",
            "documents.file_class")
        run(connection, "CREATE INDEX ix_documents_file_class ON documents (file_class)",
            "documents.file_class index")
        if engine.dialect.name == "mysql":
            run(connection, "CREATE INDEX ix_hfn_name ON historical_file_nodes (name(191))",
                "historical_file_nodes.name index")
        else:
            run(connection, "CREATE INDEX IF NOT EXISTS ix_hfn_name ON historical_file_nodes (name)",
                "historical_file_nodes.name index")
    backfilled = 0
    with SessionLocal() as db:
        for doc in db.scalars(select(Document).where(Document.file_class == "")).all():
            doc.file_class = classify(doc.extension, doc.is_folder)
            backfilled += 1
        db.commit()
    print(f"backfilled: {backfilled}")


if __name__ == "__main__":
    main()
