"""bi_center linkage probe: connectivity, cache size, and org-match ratios.
Counts only — no names, no identifiers."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.config import get_settings
from app.db import Document, EmployeeMap, SessionLocal, init_db
from app.integrations import BiCenterClient


def main() -> None:
    settings = get_settings()
    init_db()
    out: dict = {"configured": BiCenterClient(settings).configured()}
    try:
        out["connectivity"] = asyncio.run(BiCenterClient(settings).check())
    except Exception as exc:
        out["connectivity"] = {"status": "error", "message": str(exc)[:120]}
    with SessionLocal() as db:
        out["employee_map_cached"] = db.scalar(select(func.count()).select_from(EmployeeMap)) or 0
        out["employee_map_matched"] = db.scalar(select(func.count()).select_from(EmployeeMap)
                                                .where(EmployeeMap.matched.is_(True))) or 0
        out["documents_org_matched"] = db.scalar(select(func.count()).select_from(Document)
                                                 .where(Document.org_matched.is_(True))) or 0
        out["documents_unmatched"] = db.scalar(select(func.count()).select_from(Document)
                                               .where(Document.org_matched.is_(False))) or 0
        out["distinct_departments_on_documents"] = db.scalar(
            select(func.count(func.distinct(Document.department_name)))
            .where(Document.org_matched.is_(True))) or 0
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
