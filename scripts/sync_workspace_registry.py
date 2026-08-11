"""Populate the company knowledge-base registry.

Fills the existing `workspaces` table with every workspace the digital
employee can see (name, workspaceId, url, creator, source timestamps) and
refreshes `workspace_roles` administrator rows from the live member roster
(OWNER/MANAGER). Reviewer rows and manually-set governance fields are left
untouched. Idempotent; re-run any time (suggested cadence: with the monthly
reconciliation).

Usage: python scripts/sync_workspace_registry.py [--no-members]
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionLocal, Workspace, WorkspaceRole, init_db, utcnow
from app.integrations import DingtalkClient, IntegrationError

ADMIN_ROLES = {"OWNER", "MANAGER"}


async def collect(settings, with_members: bool) -> dict:
    client = DingtalkClient(settings)
    operator = settings.dingtalk_sync_operator_id
    spaces: list[dict] = []
    next_token = ""
    while True:
        page = await client.list_workspaces(operator, next_token)
        spaces.extend(page["items"])
        next_token = page.get("next_token", "")
        if not next_token:
            break
    summary = {"workspaces_seen": len(spaces), "upserted": 0, "admin_rows": 0, "member_errors": 0}
    with SessionLocal() as db:
        for space in spaces:
            workspace_id = space["workspace_id"]
            if not workspace_id:
                continue
            row = db.get(Workspace, workspace_id)
            if not row:
                row = Workspace(workspace_id=workspace_id, name=space.get("name", "") or workspace_id)
                db.add(row)
            for field in ("name", "description", "url"):
                if space.get(field):
                    setattr(row, field, space[field])
            row.source_created_at = space.get("created_at", "") or row.source_created_at
            row.source_updated_at = space.get("updated_at", "") or row.source_updated_at
            row.creator_key = space.get("creator_id", "") or row.creator_key
            row.synced_at = utcnow()
            summary["upserted"] += 1
            if not with_members:
                continue
            try:
                members: list[dict] = []
                token = ""
                while True:
                    page = await client.list_workspace_members(workspace_id, operator, token)
                    members.extend(page["items"])
                    token = page.get("next_token", "")
                    if not token:
                        break
                admins = [member for member in members
                          if member["role"] in ADMIN_ROLES and member["type"] == "USER" and member["user_id"]]
                db.execute(delete(WorkspaceRole).where(WorkspaceRole.workspace_id == workspace_id,
                                                       WorkspaceRole.role == "administrator"))
                for admin in admins:
                    db.add(WorkspaceRole(workspace_id=workspace_id, employee_key=admin["user_id"],
                                         role="administrator", display_name=admin["name"]))
                summary["admin_rows"] += len(admins)
            except IntegrationError as exc:
                summary["member_errors"] += 1
                summary.setdefault("member_error_sample", f"{exc.code}:{exc.status_code}")
        db.commit()
        summary["registry_total"] = db.scalar(select(func.count()).select_from(Workspace)) or 0
        summary["admin_total"] = db.scalar(select(func.count()).select_from(WorkspaceRole)
                                           .where(WorkspaceRole.role == "administrator")) or 0
    return summary


def main() -> None:
    with_members = "--no-members" not in sys.argv
    settings = get_settings()
    init_db()
    print(json.dumps(asyncio.run(collect(settings, with_members)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
