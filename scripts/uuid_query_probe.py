"""Last-mile converter probes: do any dentry-INFO endpoints accept the uuid
and return the numeric id? Statuses, key names and id-shaped values only.
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

DOCX = "YndMj49yWjmOEjoZhDwxAGP483pmz5aA"


def id_fields(payload: dict) -> dict:
    flat: dict = {}

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    walk(value, prefix + key + ".")
                elif "id" in key.lower() or "uuid" in key.lower():
                    flat[prefix + key] = str(value)[:40]
        elif isinstance(obj, list):
            for index, item in enumerate(obj[:2]):
                walk(item, prefix + f"{index}.")

    walk(payload)
    return flat


async def probe() -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    sid = settings.wiki_storage_space_id
    candidates = [
        ("q1_post_query", "POST", f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries/{DOCX}/query",
         {"unionId": operator}, {"option": {}}),
        ("q2_get_dentry", "GET", f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries/{DOCX}",
         {"unionId": operator}, None),
        ("q3_batch_uuid", "POST", f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries/query",
         {"unionId": operator}, {"dentryIds": [DOCX], "option": {}}),
        ("q4_v2_get", "GET", f"https://api.dingtalk.com/v2.0/storage/spaces/{sid}/dentries/{DOCX}",
         {"operatorId": operator, "unionId": operator}, None),
    ]
    out: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for label, method, url, params, body in candidates:
            try:
                resp = await http.request(method, url, params=params, headers=headers, json=body)
                if resp.is_error:
                    out[label] = {"status": resp.status_code, "body": resp.text[:170]}
                else:
                    payload = resp.json()
                    out[label] = {"status": resp.status_code, "keys": list(payload)[:8], "id_fields": id_fields(payload)}
            except httpx.HTTPError as exc:
                out[label] = {"error": str(exc)[:100]}
    return out


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
