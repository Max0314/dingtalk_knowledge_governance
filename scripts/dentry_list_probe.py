"""Probe the v1 storage dentries LISTING as a uuid->numeric-id converter:
list children of the pilot KB root with several parentId shapes and print
item KEY NAMES and id-shaped fields only (no content).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import get_settings
from app.integrations import DingtalkClient

ROOT_UUID = "NZQYprEoWoblEwoRHr6yz3yB81waOeDk"


async def probe() -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    sid = settings.wiki_storage_space_id
    out: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for label, params in (
                ("list_parent_0", {"unionId": operator, "parentId": "0", "maxResults": 5}),
                ("list_parent_uuid", {"unionId": operator, "parentId": ROOT_UUID, "maxResults": 5}),
                ("list_no_parent", {"unionId": operator, "maxResults": 5})):
            resp = await http.get(f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries",
                                  params=params, headers=headers)
            if resp.is_error:
                out[label] = {"status": resp.status_code, "body": resp.text[:180]}
            else:
                payload = resp.json()
                items = payload.get("dentries", payload.get("items", [])) or []
                first = items[0] if items else {}
                out[label] = {"status": 200, "count": len(items), "top_keys": sorted(payload.keys()),
                              "item_keys": sorted(first.keys()),
                              "id_fields": {key: str(value)[:40] for key, value in first.items()
                                            if "id" in key.lower() or "uuid" in key.lower()}}
    return out


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
