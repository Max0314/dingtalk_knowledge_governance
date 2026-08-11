"""Seed or correct a space_id -> workspaceId mapping.

Ground truth comes from upload responses, DingTalk admin views, or manual
verification; the bridge itself only learns mappings it can prove unique.

Usage: python scripts/seed_space_map.py <space_id> <workspace_id> [source]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, SpaceMap, Workspace, init_db


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: seed_space_map.py <space_id> <workspace_id> [source]"}))
        return
    space_id, workspace_id = sys.argv[1], sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "seed"
    init_db()
    with SessionLocal() as db:
        entry = db.get(SpaceMap, space_id)
        if not entry:
            entry = SpaceMap(space_id=space_id)
            db.add(entry)
        entry.workspace_id = workspace_id
        entry.source = source
        workspace = db.get(Workspace, workspace_id)
        entry.workspace_name = workspace.name if workspace else ""
        db.commit()
        print(json.dumps({"space_id": space_id, "workspace_id": workspace_id,
                          "workspace_name": entry.workspace_name, "source": source}, ensure_ascii=False))


if __name__ == "__main__":
    main()
