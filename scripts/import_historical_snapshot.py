"""Import a completed metadata-only DingTalk history snapshot into the governance DB.

Two input formats:
  * collector JSON: {"status": "completed", "nodes": [...], "totals": {...}}
  * scan NDJSON (*.ndjson): one node object per line, as produced by the
    2026-08-05 full wiki scan (runtime/dataset/wiki_scan_nodes.ndjson).
    Totals are derived from the nodes; duplicate node_ids are collapsed.

Snapshots are immutable: re-importing an existing snapshot_id is refused.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import HistoricalFileNode, HistoricalSnapshot, SessionLocal, init_db


def load_collector_json(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "completed":
        raise SystemExit("Only a completed collector snapshot can be imported.")
    return payload.get("nodes", [])


def load_ndjson(path: Path) -> list[dict]:
    nodes: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            node = json.loads(line)
            if node.get("node_id") and node.get("node_type") != "folder":
                nodes[node["node_id"]] = node
    return list(nodes.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Collector JSON or scan NDJSON with metadata-only nodes.")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--scope", default="current_authorization_accessible_org_wiki_spaces")
    args = parser.parse_args()

    path = Path(args.input)
    nodes = load_ndjson(path) if path.suffix == ".ndjson" else load_collector_json(path)
    if not nodes:
        raise SystemExit("No file nodes found in the input.")

    def year_count(year: str) -> int:
        return sum(1 for item in nodes if (item.get("created_at") or "").startswith(year))

    init_db()
    with SessionLocal() as db:
        if db.get(HistoricalSnapshot, args.snapshot_id):
            raise SystemExit(f"Snapshot {args.snapshot_id} already exists; immutable snapshots cannot be overwritten.")
        snapshot = HistoricalSnapshot(
            snapshot_id=args.snapshot_id,
            source="dingtalk",
            scope=args.scope,
            timezone="Asia/Shanghai",
            status="completed",
            collected_at=datetime.now(timezone.utc),
            definition={"include": "non-folder nodes", "year_attribution": "source createTime in Asia/Shanghai", "body_storage": "disabled"},
            total_file_nodes=len(nodes),
            created_2025=year_count("2025"),
            created_2026=year_count("2026"),
        )
        db.add(snapshot)
        db.flush()
        for offset in range(0, len(nodes), 500):
            db.add_all(HistoricalFileNode(
                snapshot_id=args.snapshot_id,
                workspace_id=item["workspace_id"],
                node_id=item["node_id"],
                name=item.get("name", ""),
                node_type=item.get("node_type", ""),
                extension=item.get("extension", ""),
                source_created_at=item.get("created_at", ""),
                source_updated_at=item.get("updated_at", ""),
            ) for item in nodes[offset:offset + 500])
            db.flush()
        db.commit()
    print(json.dumps({"snapshot_id": args.snapshot_id, "nodes": len(nodes), "status": "imported"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
