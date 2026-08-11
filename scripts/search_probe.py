"""Probe the wiki node search API (the bridge locator's engine).

Usage: python scripts/search_probe.py <keyword>
Prints matched node names and workspaces — never file contents.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.integrations import DingtalkClient, IntegrationError


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    if not keyword:
        print(json.dumps({"error": "usage: search_probe.py <keyword>"}))
        return
    settings = get_settings()
    try:
        nodes = asyncio.run(DingtalkClient(settings).search_wiki_nodes(keyword, settings.dingtalk_sync_operator_id))
    except IntegrationError as exc:
        print(json.dumps({"error": exc.code, "status": exc.status_code, "message": str(exc)}, ensure_ascii=False))
        return
    print(json.dumps({"keyword": keyword, "hits": [
        {"name": node.get("name"), "workspace_id": node.get("workspace_id"),
         "node_id": node.get("node_id"), "extension": node.get("extension")}
        for node in nodes[:10]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
