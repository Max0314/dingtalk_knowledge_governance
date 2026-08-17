"""Run one real review for one node and print the structured result (never
the body).

Usage: python scripts/run_review_once.py <node_id> [trigger]

This command writes an immutable review instance and may enqueue a notification.
Use ``content_probe.py`` instead when only the body-fetch path should be checked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.service import ContentUnavailableError, run_review


ALLOWED_TRIGGERS = {"manual_rerun", "audit", "modify_merged", "demo_seed"}


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    if len(sys.argv) < 2:
        _print({"status": "failed", "error_code": "usage",
                "message": "usage: run_review_once.py <node_id> [trigger]"})
        return 2
    node_id = sys.argv[1]
    trigger = sys.argv[2] if len(sys.argv) > 2 else "manual_rerun"
    if len(sys.argv) > 3 or trigger not in ALLOWED_TRIGGERS:
        _print({"status": "failed", "error_code": "invalid_trigger",
                "allowed_triggers": sorted(ALLOWED_TRIGGERS)})
        return 2
    try:
        init_db()
        with SessionLocal() as db:
            instance = run_review(db, get_settings(), node_id, trigger)
    except ContentUnavailableError as exc:
        reason = str(exc)
        if not reason or len(reason) > 48 or any(not (c.islower() or c.isdigit() or c in "_:.-") for c in reason):
            reason = "unknown"
        _print({"status": "skipped", "node_id": node_id,
                "error_code": f"content_unavailable:{reason}"})
        return 0
    except KeyError:
        _print({"status": "failed", "node_id": node_id, "error_code": "document_not_found"})
        return 1
    except Exception:
        # Never leak integration messages, signed URLs or document content from
        # a maintenance command. Detailed diagnostics remain in sanitized logs.
        _print({"status": "failed", "node_id": node_id, "error_code": "review_execution_failed"})
        return 1
    if instance is None:
        _print({"status": "skipped", "node_id": node_id, "error_code": "content_unchanged"})
        return 0
    _print({
        "status": "succeeded",
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
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
