"""Raw download-info probe: prints HTTP status and the ERROR body (never file
content) for one dentry, to diagnose 4xx causes precisely.

Usage: python scripts/download_probe.py <dentry_uuid> [space_id]
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


async def probe(dentry_uuid: str, space_id: str) -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    out: dict = {"dentry_uuid": dentry_uuid, "space_id": space_id}
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            f"https://api.dingtalk.com/v1.0/storage/spaces/{space_id}/dentries/{dentry_uuid}/downloadInfos/query",
            params={"unionId": settings.dingtalk_sync_operator_id},
            headers={"x-acs-dingtalk-access-token": token},
            json={"option": {}},
        )
    out["status"] = response.status_code
    if response.is_error:
        out["error_body"] = response.text[:400]
    else:
        info = response.json().get("headerSignatureInfo") or {}
        out["ok"] = {"urls": len(info.get("resourceUrls") or []), "expire": info.get("expirationSeconds")}
    return out


def main() -> None:
    dentry_uuid = sys.argv[1] if len(sys.argv) > 1 else ""
    space_id = sys.argv[2] if len(sys.argv) > 2 else get_settings().wiki_storage_space_id
    print(json.dumps(asyncio.run(probe(dentry_uuid, space_id)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
