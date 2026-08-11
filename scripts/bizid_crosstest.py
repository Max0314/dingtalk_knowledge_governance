"""Cross-test: is the audit trail's numeric bizId actually the file's numeric
dentryId? Take recent wiki-upload events (name+size known), feed bizId into
the numeric download-info endpoint, then verify by downloading and comparing
BYTE LENGTH against the event's resourceSize. Sizes and statuses only —
content bytes are counted, never inspected or persisted.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import FileAuditEvent, SessionLocal, init_db
from app.integrations import DingtalkClient


async def run() -> dict:
    settings = get_settings()
    client = DingtalkClient(settings)
    token = await client._token_value()
    operator = settings.dingtalk_sync_operator_id
    headers = {"x-acs-dingtalk-access-token": token}
    sid = settings.wiki_storage_space_id
    init_db()
    with SessionLocal() as db:
        events = db.scalars(select(FileAuditEvent)
                            .where(FileAuditEvent.action_view == "知识库上传文件",
                                   FileAuditEvent.size > 0)
                            .order_by(FileAuditEvent.gmt_create.desc()).limit(3)).all()
        samples = [{"biz_id": event.biz_id, "resource": event.resource[:40],
                    "expected_size": event.size, "space": event.target_space_id} for event in events]
    out: dict = {"samples": len(samples), "results": []}
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as http:
        for sample in samples:
            entry: dict = {"resource": sample["resource"], "expected_size": sample["expected_size"],
                           "biz_id": sample["biz_id"]}
            resp = await http.post(
                f"https://api.dingtalk.com/v1.0/storage/spaces/{sample['space'] or sid}/dentries/{sample['biz_id']}/downloadInfos/query",
                params={"unionId": operator}, headers=headers, json={"option": {}})
            if resp.is_error:
                entry["download_info"] = {"status": resp.status_code, "body": resp.text[:160]}
            else:
                info = resp.json().get("headerSignatureInfo") or {}
                urls = info.get("resourceUrls") or []
                entry["download_info"] = {"status": 200, "urls": len(urls)}
                if urls:
                    blob = await http.get(urls[0], headers=info.get("headers") or {})
                    entry["downloaded_bytes"] = len(blob.content)
                    entry["size_match"] = (len(blob.content) == sample["expected_size"])
            out["results"].append(entry)
    return out


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
