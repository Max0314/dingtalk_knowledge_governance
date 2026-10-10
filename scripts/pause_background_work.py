"""Apply the quota repair's paused state without printing any .env secrets.

Run on the application host after checkout and before recreating API:
  python3 scripts/pause_background_work.py --apply
The exact old .env is backed up with mode 0600. No document data is deleted.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil

PAUSED = {
    "KG_AUDIT_PULL_ENABLED": "false", "KG_BRIDGE_ENABLED": "false",
    "KG_FULL_SCAN_ENABLED": "false", "KG_REVIEW_ENABLED": "false",
    "KG_NOTIFY_ENABLED": "false", "KG_STREAM_ENABLED": "false",
    "KG_BRIDGE_WALKS_ENABLED": "false", "KG_SCAN_DAYS": "10",
    "KG_SCAN_MAX_ATTEMPTS": "3", "KG_SCAN_RETRY_SECONDS": "900",
    "KG_SCAN_MAX_API_CALLS": "60000", "KG_DINGTALK_DAILY_LIMIT": "65000",
    "KG_DINGTALK_MONTHLY_LIMIT": "100000", "KG_DINGTALK_NODES_MONTHLY_LIMIT": "60000",
    "KG_DINGTALK_SEARCH_DAILY_LIMIT": "500", "KG_DINGTALK_SEARCH_MONTHLY_LIMIT": "10000",
    "KG_DINGTALK_AUDIT_DAILY_LIMIT": "1000", "KG_BRIDGE_LOCATOR_MAX_ATTEMPTS": "3",
    "KG_BRIDGE_LOCATOR_RETRY_SECONDS": "600",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    if not env.is_file() or env.is_symlink():
        raise SystemExit("Expected regular project .env")
    original = env.read_text(encoding="utf-8")
    updated = original
    for key, value in PAUSED.items():
        line = f"{key}={value}"
        pattern = rf"(?m)^\s*(?:export\s+)?{re.escape(key)}\s*=.*$"
        updated = re.sub(pattern, line, updated) if re.search(pattern, updated) else updated.rstrip() + "\n" + line + "\n"
    print("Background switches: OFF; monthly day=10; quota limits configured.")
    if not args.apply:
        print("DRY RUN; add --apply")
        return
    if updated == original:
        print("Configuration already applied.")
        return
    backup = root / (".env.quota-backup-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
    # Create with restrictive permissions before copying credential content.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as dest, env.open("rb") as source:
        shutil.copyfileobj(source, dest)
    env.write_text(updated, encoding="utf-8")
    env.chmod(0o600)
    print("Configuration applied; previous .env preserved in a mode-0600 .env.quota-backup-* file.")


if __name__ == "__main__":
    main()
