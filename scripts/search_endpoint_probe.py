"""Find the live wiki-search endpoint: try documented-path candidates and
print HTTP status plus top-level response keys (never document contents).

Usage: python scripts/search_endpoint_probe.py <keyword>
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


async def probe(keyword: str) -> list[dict]:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    candidates = [
        ("POST", "https://api.dingtalk.com/v2.0/wiki/nodes/search", None,
         {"keyword": keyword, "operatorId": operator, "maxResults": 10}),
        ("GET", "https://api.dingtalk.com/v2.0/wiki/nodes/search",
         {"keyword": keyword, "operatorId": operator, "maxResults": 10}, None),
        ("POST", "https://api.dingtalk.com/v1.0/wiki/nodes/search", None,
         {"keyword": keyword, "operatorId": operator, "maxResults": 10}),
        ("POST", "https://api.dingtalk.com/v2.0/wiki/nodes/query", None,
         {"keyword": keyword, "operatorId": operator, "maxResults": 10}),
        ("GET", "https://api.dingtalk.com/v2.0/wiki/nodes",
         {"keyword": keyword, "operatorId": operator, "maxResults": 10}, None),
    ]
    results = []
    async with httpx.AsyncClient(timeout=20) as http:
        for method, url, params, body in candidates:
            try:
                response = await http.request(method, url, params=params, json=body, headers=headers)
                keys = []
                try:
                    payload = response.json()
                    keys = list(payload)[:6] if isinstance(payload, dict) else [type(payload).__name__]
                    count = len(payload.get("nodes", payload.get("items", payload.get("data", [])))) if isinstance(payload, dict) else 0
                except Exception:
                    count = -1
                results.append({"method": method, "url": url.split(".com")[1], "status": response.status_code,
                                "keys": keys, "count": count})
            except httpx.HTTPError as exc:
                results.append({"method": method, "url": url.split(".com")[1], "error": str(exc)[:80]})
    return results


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "测试"
    print(json.dumps(asyncio.run(probe(keyword)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
