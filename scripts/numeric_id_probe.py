"""Hunt the uuid->numeric-id converter: space info (numeric root id?), then
list children by numeric parent and print id-shaped fields per item.
Names/ids only — no content.
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


async def probe() -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    sid = settings.wiki_storage_space_id
    out: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        info = await http.get(f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}",
                              params={"unionId": operator}, headers=headers)
        if info.is_error:
            out["space_info"] = {"status": info.status_code, "body": info.text[:200]}
            root_numeric = ""
        else:
            payload = info.json()
            space = payload.get("space", payload)
            out["space_info"] = {"status": 200, "keys": sorted(space.keys()) if isinstance(space, dict) else [],
                                 "id_fields": {key: str(value)[:32] for key, value in (space.items() if isinstance(space, dict) else [])
                                               if "id" in key.lower()}}
            root_numeric = str(space.get("rootDentryId", "") or space.get("rootId", ""))
        for parent in [p for p in (root_numeric, "0") if p]:
            resp = await http.get(f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries",
                                  params={"unionId": operator, "parentId": parent, "maxResults": 5}, headers=headers)
            label = f"list_parent_{parent}"
            if resp.is_error:
                out[label] = {"status": resp.status_code, "body": resp.text[:180]}
            else:
                payload = resp.json()
                items = payload.get("dentries", payload.get("items", [])) or []
                first = items[0] if items else {}
                out[label] = {"status": 200, "count": len(items), "item_keys": sorted(first.keys()),
                              "id_fields": {key: str(value)[:40] for key, value in first.items()
                                            if "id" in key.lower() or "uuid" in key.lower()}}
            if resp.status_code == 200:
                break
    return out


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
