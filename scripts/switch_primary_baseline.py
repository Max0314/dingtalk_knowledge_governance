"""Switch the headline primary baseline to another snapshot.

Sets definition.is_primary_baseline on the target snapshot and clears it
everywhere else, then prints the freshly recomputed headline totals so the
effect is verified in the same breath. Restart the containers afterwards —
the metrics cache in running processes only refreshes on data changes.

Usage: python scripts/switch_primary_baseline.py <snapshot_id> [--apply]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import metrics
from app.db import HistoricalSnapshot, SessionLocal, init_db


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    apply = "--apply" in sys.argv
    init_db()
    with SessionLocal() as db:
        snapshots = db.scalars(select(HistoricalSnapshot).order_by(HistoricalSnapshot.collected_at)).all()
        out: dict = {"apply": apply, "target": target, "snapshots": [
            {"snapshot_id": s.snapshot_id, "total_file_nodes": s.total_file_nodes,
             "is_primary": bool((s.definition or {}).get("is_primary_baseline"))} for s in snapshots]}
        if target and not any(s.snapshot_id == target for s in snapshots):
            out["error"] = "snapshot_not_found"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        if apply and target:
            for snapshot in snapshots:
                definition = dict(snapshot.definition or {})
                definition["is_primary_baseline"] = (snapshot.snapshot_id == target)
                snapshot.definition = definition
                flag_modified(snapshot, "definition")
            db.commit()
            out["switched_to"] = target
            fresh = metrics.monthly_increments(db)
            out["recomputed"] = {"total_files": fresh["total_files"],
                                 "yearly": fresh["yearly"],
                                 "primary_snapshot_id": metrics.primary_snapshot_id(db)}
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
