"""Probe the locator chain: storage dentry search -> wiki node batch query.

Usage: python scripts/search_probe.py <keyword>
Prints names, dentryUuids and workspaces — never file contents.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.integrations import DingtalkClient, IntegrationError


async def probe(keyword: str) -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    operator = settings.dingtalk_sync_operator_id
    out: dict = {"keyword": keyword, "space_id": settings.wiki_storage_space_id}
    try:
        dentries = await client.search_dentries(keyword, operator,
                                                [settings.wiki_storage_space_id] if settings.wiki_storage_space_id else None)
        out["dentry_hits"] = [{"name": d["name"], "dentry_uuid": d["dentry_uuid"], "path": d["path"][:80]}
                              for d in dentries[:8]]
        ids = [d["dentry_uuid"] for d in dentries if d.get("dentry_uuid")][:8]
        if ids:
            nodes = await client.batch_query_wiki_nodes(ids, operator)
            out["node_hits"] = [{"name": n.get("name"), "node_id": n.get("node_id"),
                                 "workspace_id": n.get("workspace_id")} for n in nodes[:8]]
    except IntegrationError as exc:
        out["error"] = {"code": exc.code, "status": exc.status_code, "message": str(exc)}
    return out


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    if not keyword:
        print(json.dumps({"error": "usage: search_probe.py <keyword>"}))
        return
    print(json.dumps(asyncio.run(probe(keyword)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
