"""Dump the FIELD NAMES (and id-shaped values) of raw fileAuditLogs rows —
does the trail carry a numeric resource/dentry id we discarded?
Prints keys and id-like fields only; resource names truncated.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit_pull import _fetch_pages
from app.config import get_settings


def main() -> None:
    settings = get_settings()
    now = int(time.time() * 1000)
    rows = asyncio.run(_fetch_pages(settings, now - 30 * 60 * 1000, now))
    out: dict = {"rows": len(rows)}
    write_row = next((row for row in rows if "上传" in str(row.get("actionView", ""))), rows[0] if rows else None)
    if write_row:
        out["sample_keys"] = sorted(write_row.keys())
        out["id_like_fields"] = {key: str(value)[:44] for key, value in write_row.items()
                                 if any(tag in key.lower() for tag in ("id", "uuid", "resource", "space"))}
        out["id_like_fields"].pop("resource", None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
