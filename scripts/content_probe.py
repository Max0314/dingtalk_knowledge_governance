"""Diagnose the body-extraction chain for one node. Prints ONLY lengths,
sources and error codes — never document content.

Usage: python scripts/content_probe.py <node_id>
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.content import extract_text, fetch_document_content
from app.db import Document, SessionLocal, init_db
from app.integrations import DingtalkClient, IntegrationError


def main() -> None:
    node_id = sys.argv[1] if len(sys.argv) > 1 else ""
    settings = get_settings()
    init_db()
    out: dict = {"node_id": node_id, "space_id": settings.wiki_storage_space_id,
                 "extract_enabled": settings.content_extract_enabled}
    with SessionLocal() as db:
        doc = db.get(Document, node_id)
        if not doc:
            print(json.dumps({"error": "document_not_found"}))
            return
        out["extension"] = doc.extension
        out["size"] = doc.size
        # Uploaded files and native .adoc documents deliberately use different
        # APIs.  Probe the low-level storage route only where a numeric storage
        # key exists; fetch_document_content covers both paths below.
        if doc.extension != "adoc" and doc.storage_dentry_id:
            try:
                data = asyncio.run(DingtalkClient(settings).download_file_bytes(
                    settings.wiki_storage_space_id, doc.storage_dentry_id, settings.content_max_bytes
                ))
                out["downloaded_bytes"] = len(data)
                out["extracted_chars"] = len(extract_text(doc.extension, data))
            except IntegrationError as exc:
                out["download_error"] = {"code": exc.code, "status": exc.status_code}
        try:
            text, source = asyncio.run(fetch_document_content(settings, doc))
            out["fetch"] = {"chars": len(text), "source": source}
        except IntegrationError as exc:
            out["fetch_error"] = {"code": exc.code, "status": exc.status_code}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
