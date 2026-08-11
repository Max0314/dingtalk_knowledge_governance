"""Migration: documents.storage_dentry_id (numeric download key from audit
events) plus a backfill from already-matched audit events."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.db import Document, FileAuditEvent, SessionLocal, engine, init_db


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
        run(connection, "ALTER TABLE documents ADD COLUMN storage_dentry_id VARCHAR(64) NOT NULL DEFAULT ''",
            "documents.storage_dentry_id")
    attached = 0
    with SessionLocal() as db:
        events = db.scalars(select(FileAuditEvent).where(FileAuditEvent.matched_node_id != "")).all()
        for event in events:
            if not (event.biz_id or "").isdigit():
                continue
            doc = db.get(Document, event.matched_node_id)
            if doc and not doc.storage_dentry_id:
                doc.storage_dentry_id = event.biz_id
                attached += 1
        db.commit()
    print(f"backfilled_from_events: {attached}")


if __name__ == "__main__":
    main()
