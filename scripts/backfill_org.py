"""Re-resolve organization attribution for unmatched mirrored documents.

The 08-07 seed ran minutes before the userId-vs-unionId fix landed, so the
seeded rows carry stale 未映射. Resolves uploaders through bi_center in
batches of 50 (userId/unionId chosen by shape) and writes back. Idempotent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import get_settings
from app.db import Document, SessionLocal, init_db
from app.integrations import BiCenterClient


async def run() -> dict:
    settings = get_settings()
    client = BiCenterClient(settings)
    summary = {"scanned": 0, "matched": 0, "still_unmatched": 0, "no_uploader": 0}
    with SessionLocal() as db:
        docs = db.scalars(select(Document).where(Document.org_matched.is_(False))).all()
        summary["scanned"] = len(docs)
        for start in range(0, len(docs), 50):
            chunk = [doc for doc in docs[start:start + 50]]
            payload = []
            for doc in chunk:
                key = doc.uploader_key or ""
                if not key:
                    summary["no_uploader"] += 1
                payload.append({"userId": key} if key.isdigit() else {"unionId": key})
            results = await client.resolve_batch(payload, "")
            for doc, identity in zip(chunk, results if isinstance(results, list) else []):
                identity = identity or {}
                if identity.get("matched") and identity.get("includeInOfficialStats"):
                    doc.uploader_key = identity.get("employeeKey", doc.uploader_key)
                    doc.uploader_name = identity.get("employeeName", "")
                    doc.department_name = identity.get("departmentName", "")
                    doc.biz_group_name = identity.get("bizGroupName", "")
                    doc.org_matched = True
                    summary["matched"] += 1
                else:
                    summary["still_unmatched"] += 1
            db.commit()
    return summary


def main() -> None:
    init_db()
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
