"""Latest review instance detail for one node (structured fields only).

Usage: python scripts/review_detail.py <node_id>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import Document, ReviewInstance, SessionLocal, init_db


def main() -> None:
    node_id = sys.argv[1] if len(sys.argv) > 1 else ""
    init_db()
    with SessionLocal() as db:
        doc = db.get(Document, node_id)
        instance = db.scalar(select(ReviewInstance).where(ReviewInstance.node_id == node_id)
                             .order_by(ReviewInstance.created_at.desc()).limit(1))
        if not instance:
            print(json.dumps({"error": "no_review"}))
            return
        model_block = (instance.dimensions or {}).get("model") or {}
        print(json.dumps({
            "name": doc.name if doc else "?", "file_class": doc.file_class if doc else "?",
            "storage_dentry_id_set": bool(doc.storage_dentry_id) if doc else False,
            "ai_score": instance.ai_score, "verdict": instance.verdict, "scope": instance.review_scope,
            "trigger": instance.trigger, "rule_version": instance.rule_version,
            "model_config_version": instance.model_config_version,
            "created_at": instance.created_at.isoformat(),
            "dual_track": {key: model_block.get(key) for key in
                           ("genre", "rule_score", "model_score", "composite", "model_dimensions")} if model_block else None,
            "advisory_dims": [key for key, value in (instance.dimensions or {}).items()
                              if isinstance(value, dict) and value.get("advisory")],
            "findings": [finding.get("message") for finding in (instance.findings or [])][:6],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
