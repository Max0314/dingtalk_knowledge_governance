"""Three-route download/content probe for one dentry. Prints statuses and
error bodies (or content LENGTH only) — never document text.

Routes: A) v1 storage downloadInfos (numeric-id family, expected 400 on uuid)
        B) v1 drive downloadInfos (string fileId family)
        C) v2 doc contents by dentryUuid (native-doc content read)

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
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    out: dict = {"dentry_uuid": dentry_uuid, "space_id": space_id}
    async with httpx.AsyncClient(timeout=30) as http:
        a = await http.post(
            f"https://api.dingtalk.com/v1.0/storage/spaces/{space_id}/dentries/{dentry_uuid}/downloadInfos/query",
            params={"unionId": operator}, headers=headers, json={"option": {}})
        out["A_storage_v1"] = {"status": a.status_code, "body": a.text[:200] if a.is_error else "OK"}
        b = await http.get(
            f"https://api.dingtalk.com/v1.0/drive/spaces/{space_id}/files/{dentry_uuid}/downloadInfos",
            params={"unionId": operator}, headers=headers)
        if b.is_error:
            out["B_drive_v1"] = {"status": b.status_code, "body": b.text[:200]}
        else:
            payload = b.json()
            info = payload.get("downloadInfo") or payload
            urls = (info.get("resourceUrls") or [info.get("resourceUrl")] if isinstance(info, dict) else []) or []
            out["B_drive_v1"] = {"status": b.status_code, "keys": list(payload)[:6], "urls": len([u for u in urls if u])}
        c = await http.get(
            f"https://api.dingtalk.com/v2.0/doc/dentries/{dentry_uuid}/contents",
            params={"operatorId": operator, "targetFormat": "markdown"}, headers=headers)
        if c.is_error:
            out["C_doc_contents"] = {"status": c.status_code, "body": c.text[:200]}
        else:
            payload = c.json()
            content = payload.get("content", "") if isinstance(payload, dict) else ""
            out["C_doc_contents"] = {"status": c.status_code, "keys": list(payload)[:6] if isinstance(payload, dict) else [],
                                     "content_chars": len(content or "")}
    return out


def main() -> None:
    dentry_uuid = sys.argv[1] if len(sys.argv) > 1 else ""
    space_id = sys.argv[2] if len(sys.argv) > 2 else get_settings().wiki_storage_space_id
    print(json.dumps(asyncio.run(probe(dentry_uuid, space_id)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
