"""Ask each blocked endpoint to NAME its missing permission scope: calls the
locator search and the permission-roster endpoints and prints their error
bodies (DingTalk names the scope in the message). Never prints content.
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
    root = "NZQYprEoWoblEwoRHr6yz3yB81waOeDk"  # pilot KB root dentry
    out: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        search = await http.post(
            "https://api.dingtalk.com/v2.0/storage/dentries/search",
            params={"operatorId": operator}, headers=headers,
            json={"keyword": "监控链路验证报告", "option": {"maxResults": 5, "spaceIds": [settings.wiki_storage_space_id]}})
        out["dentries_search"] = {"status": search.status_code,
                                  "body": search.text[:260] if search.is_error else f"OK items={len(search.json().get('items') or [])}"}
        perms = await http.post(
            f"https://api.dingtalk.com/v2.0/storage/spaces/dentries/{root}/permissions/query",
            params={"unionId": operator}, headers=headers, json={"option": {"maxResults": 50}})
        if perms.is_error:
            out["permissions_query"] = {"status": perms.status_code, "body": perms.text[:260]}
        else:
            rows = perms.json().get("permissions", []) or []
            out["permissions_query"] = {"status": perms.status_code, "roster_size": len(rows),
                                        "sample_roles": sorted({str((r.get('role') or {}).get('id', '')) for r in rows})[:6]}
    return out


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
