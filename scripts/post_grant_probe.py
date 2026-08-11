"""Post-grant probe: raw field names from newly-unlocked endpoints (keys and
statuses only — never document content).

1. dentries/search raw item keys (does the wire carry a numeric id?)
2. permission roster live sample (registry admins)
3. doc contents retry (was 503)
4. candidate uuid download variants under the granted scope
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

ADOC = "dQPGYqjpJYmkXYbEhBjv4Qz3Jakx1Z5N"      # 监控链路验证报告_V1.0
DOCX = "YndMj49yWjmOEjoZhDwxAGP483pmz5aA"      # 77886_002.docx
ROOT = "NZQYprEoWoblEwoRHr6yz3yB81waOeDk"      # pilot KB root


async def probe() -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    sid = settings.wiki_storage_space_id
    out: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        search = await http.post(
            "https://api.dingtalk.com/v2.0/storage/dentries/search",
            params={"operatorId": operator}, headers=headers,
            json={"keyword": "77886_002", "option": {"maxResults": 3, "spaceIds": [sid]}})
        if search.is_error:
            out["search"] = {"status": search.status_code, "body": search.text[:200]}
        else:
            items = search.json().get("items", []) or []
            out["search"] = {"status": 200, "count": len(items),
                             "item_keys": sorted(items[0].keys()) if items else [],
                             "nested": {key: sorted(value.keys()) for key, value in (items[0].items() if items else [])
                                        if isinstance(value, dict)}}
        perms = await http.post(
            f"https://api.dingtalk.com/v2.0/storage/spaces/dentries/{ROOT}/permissions/query",
            params={"unionId": operator}, headers=headers, json={"option": {"maxResults": 50}})
        if perms.is_error:
            out["permissions"] = {"status": perms.status_code, "body": perms.text[:200]}
        else:
            rows = perms.json().get("permissions", []) or []
            out["permissions"] = {"status": 200, "roster": [
                {"name": (row.get("member") or {}).get("name"), "type": (row.get("member") or {}).get("type"),
                 "role": (row.get("role") or {}).get("id") or (row.get("role") or {}).get("name")} for row in rows[:6]]}
        doc = await http.get(f"https://api.dingtalk.com/v2.0/doc/dentries/{ADOC}/contents",
                             params={"operatorId": operator, "targetFormat": "markdown"}, headers=headers)
        out["doc_contents"] = ({"status": doc.status_code, "body": doc.text[:160]} if doc.is_error else
                               {"status": 200, "keys": sorted(doc.json().keys()),
                                "content_chars": len(doc.json().get("content") or "")})
        for label, method, url in (
                ("dl_v2_files", "POST", f"https://api.dingtalk.com/v2.0/storage/spaces/files/{DOCX}/downloadInfos/query"),
                ("dl_v1_uuid_again", "POST", f"https://api.dingtalk.com/v1.0/storage/spaces/{sid}/dentries/{DOCX}/downloadInfos/query")):
            resp = await http.request(method, url, params={"unionId": operator, "operatorId": operator},
                                      headers=headers, json={"option": {}})
            out[label] = {"status": resp.status_code, "body": resp.text[:160] if resp.is_error else "OK:" + ",".join(list(resp.json())[:4])}
    return out


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
