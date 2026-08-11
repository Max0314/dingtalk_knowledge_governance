"""Run one review for one node and print the structured result (never the
body). For verifying the content-extraction upgrade on real documents.

Usage: python scripts/run_review_once.py <node_id> [trigger]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.service import run_review


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: run_review_once.py <node_id> [trigger]"}))
        return
    node_id = sys.argv[1]
    trigger = sys.argv[2] if len(sys.argv) > 2 else "manual"
    init_db()
    with SessionLocal() as db:
        instance = run_review(db, get_settings(), node_id, trigger)
        print(json.dumps({
            "review_instance_id": instance.review_instance_id,
            "node_id": instance.node_id,
            "ai_score": instance.ai_score,
            "verdict": instance.verdict,
            "review_scope": instance.review_scope,
            "rule_version": instance.rule_version,
            "model_config_version": instance.model_config_version,
            "fingerprint": (instance.content_fingerprint or "")[:16],
            "dimensions": {key: {"deduction": value.get("deduction"), "cap": value.get("cap")}
                           for key, value in (instance.dimensions or {}).items()},
            "findings": [finding.get("message") for finding in (instance.findings or [])][:8],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
