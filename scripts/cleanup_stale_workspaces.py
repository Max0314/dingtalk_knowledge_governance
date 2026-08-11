"""Remove personal-authorization-era workspace rows from the registry.

The registry sync stamps every currently-visible workspace with a fresh
synced_at; rows last touched before the cutoff and holding no mirrored
documents are the old-namespace leftovers (48 rows from the pre-digital-
employee era). Historical snapshots are untouched — they carry their own
workspace ids and remain the audit record.

Usage: python scripts/cleanup_stale_workspaces.py [--apply] [--cutoff 2026-08-10]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from app.db import Document, SessionLocal, Workspace, WorkspaceRole, init_db


def main() -> None:
    apply = "--apply" in sys.argv
    cutoff_raw = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--cutoff"), "2026-08-10")
    cutoff = datetime.strptime(cutoff_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    init_db()
    with SessionLocal() as db:
        governed = {row[0] for row in db.execute(select(Document.workspace_id).distinct())}
        stale = [ws for ws in db.scalars(select(Workspace)).all()
                 if ws.workspace_id not in governed
                 and (ws.synced_at is None or ws.synced_at.replace(tzinfo=timezone.utc) < cutoff)]
        out = {"apply": apply, "cutoff": cutoff_raw, "stale_count": len(stale),
               "sample": [{"workspace_id": ws.workspace_id, "name": ws.name} for ws in stale[:8]]}
        if apply and stale:
            ids = [ws.workspace_id for ws in stale]
            db.execute(delete(WorkspaceRole).where(WorkspaceRole.workspace_id.in_(ids)))
            db.execute(delete(Workspace).where(Workspace.workspace_id.in_(ids)))
            db.commit()
            out["deleted"] = len(ids)
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
