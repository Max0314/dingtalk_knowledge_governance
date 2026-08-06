"""Creator-aware full wiki scan under the service identity (new id namespace).

Run detached inside the api container:
    docker compose exec -d api python scripts/scan_uploader_baseline.py
Progress:  /app/uploader_scan_state.json  (poll with exec cat)

Behaviour, shaped by earlier incidents:
  * every DingTalk call retries 4x with backoff — one stray 503 must not skip
    a workspace (the 08-04 scan lost half its coverage to exactly that);
  * a paced sleep between calls keeps the API and the shared DB calm;
  * rows batch-insert per workspace inside one short transaction, then the
    (uploader x month) aggregate for that workspace is rebuilt from memory —
    dashboards never scan raw rows;
  * resumable: finished workspaces are recorded in the state file and skipped.

Excluded by name: knowledge bases the operator marked as test residue.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import delete

from app.config import get_settings
from app.db import HistoricalFileNode, HistoricalSnapshot, SessionLocal, UploaderMonthStat, init_db, utcnow
from app.integrations import DingtalkClient, normalize_node

SNAPSHOT_ID = "wiki-uploader-2026-08-06"
STATE_PATH = Path("/app/uploader_scan_state.json")
EXCLUDED_NAMES = {"CD_P-06-通信_光猫"}
CALL_PAUSE = 0.12
RETRIES = 4


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"status": "running", "done": {}, "failures": {}, "calls": 0, "files": 0, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


async def fetch_nodes(client: DingtalkClient, operator: str, workspace_id: str, parent: str, next_token: str) -> dict:
    token = await client._token_value()
    last = ""
    for attempt in range(RETRIES):
        try:
            params = {"workspaceId": workspace_id, "operatorId": operator, "parentNodeId": parent, "maxResults": 30}
            if next_token:
                params["nextToken"] = next_token
            async with httpx.AsyncClient(timeout=25) as http:
                response = await http.get("https://api.dingtalk.com/v2.0/wiki/nodes", params=params,
                                          headers={"x-acs-dingtalk-access-token": token})
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}"
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


async def scan_workspace(client: DingtalkClient, operator: str, space: dict, state: dict) -> None:
    workspace_id, name = space["workspace_id"], space["name"]
    nodes: dict[str, dict] = {}
    stack = [space.get("root_node_id", "")]
    while stack:
        parent = stack.pop()
        if not parent:
            continue
        next_token = ""
        while True:
            payload = await fetch_nodes(client, operator, workspace_id, parent, next_token)
            state["calls"] += 1
            for raw in payload.get("nodes", []):
                node = normalize_node(raw)
                if node["has_children"]:
                    stack.append(node["node_id"])
                if node["type"] == "FILE" and node["node_id"]:
                    nodes[node["node_id"]] = {**node, "parent": parent}
            next_token = payload.get("nextToken", "")
            await asyncio.sleep(CALL_PAUSE)
            if not next_token:
                break

    with SessionLocal() as db:
        db.execute(delete(HistoricalFileNode).where(HistoricalFileNode.snapshot_id == SNAPSHOT_ID,
                                                    HistoricalFileNode.workspace_id == workspace_id))
        db.execute(delete(UploaderMonthStat).where(UploaderMonthStat.snapshot_id == SNAPSHOT_ID,
                                                   UploaderMonthStat.workspace_id == workspace_id))
        items = list(nodes.values())
        for offset in range(0, len(items), 500):
            db.add_all(HistoricalFileNode(
                snapshot_id=SNAPSHOT_ID, workspace_id=workspace_id, node_id=item["node_id"],
                parent_node_id=item["parent"], name=item["name"], node_type="file",
                extension=item["extension"], creator_user_id=item["creator_id"],
                source_created_at=item["created_at"], source_updated_at=item["updated_at"],
            ) for item in items[offset:offset + 500])
            db.flush()
        aggregate: dict[tuple[str, str], int] = {}
        for item in items:
            month = (item["created_at"] or "")[:7]
            if len(month) == 7:
                key = (item["creator_id"], month)
                aggregate[key] = aggregate.get(key, 0) + 1
        db.add_all(UploaderMonthStat(snapshot_id=SNAPSHOT_ID, workspace_id=workspace_id, workspace_name=name,
                                     creator_user_id=creator, month=month, file_count=count)
                   for (creator, month), count in aggregate.items())
        snapshot = db.get(HistoricalSnapshot, SNAPSHOT_ID)
        definition = dict(snapshot.definition or {})
        definition.setdefault("workspaces", {})[workspace_id] = name
        snapshot.definition = definition
        snapshot.total_file_nodes = (snapshot.total_file_nodes or 0) + len(items)
        db.commit()
    state["done"][workspace_id] = {"name": name, "files": len(items)}
    state["files"] += len(items)


async def main() -> None:
    settings = get_settings()
    client = DingtalkClient(settings)
    operator = settings.dingtalk_sync_operator_id
    init_db()
    with SessionLocal() as db:
        if not db.get(HistoricalSnapshot, SNAPSHOT_ID):
            db.add(HistoricalSnapshot(snapshot_id=SNAPSHOT_ID, scope="service_identity_visible_spaces",
                                      status="running", collected_at=utcnow(),
                                      definition={"purpose": "uploader_attribution", "id_namespace": "app-token-v2",
                                                  "excluded_names": sorted(EXCLUDED_NAMES), "workspaces": {}}))
            db.commit()

    state = load_state()
    spaces = []
    token = ""
    while True:
        page = await client.list_workspaces(operator, next_token=token, max_results=30)
        spaces += page["items"]
        token = page.get("next_token", "")
        if not token:
            break
    todo = [s for s in spaces if s["workspace_id"] not in state["done"] and s["name"] not in EXCLUDED_NAMES]
    print(f"spaces total={len(spaces)} todo={len(todo)} excluded={sum(1 for s in spaces if s['name'] in EXCLUDED_NAMES)}", flush=True)

    for index, space in enumerate(todo):
        try:
            await scan_workspace(client, operator, space, state)
            state["failures"].pop(space["workspace_id"], None)
        except Exception as exc:
            state["failures"][space["workspace_id"]] = {"name": space["name"], "error": str(exc)[:200]}
        save_state(state)
        print(f"[{len(state['done'])}/{len(spaces)}] {space['name']} files={state['done'].get(space['workspace_id'], {}).get('files', 'FAIL')} calls={state['calls']}", flush=True)

    state["status"] = "completed" if not state["failures"] else "completed_with_failures"
    save_state(state)
    with SessionLocal() as db:
        snapshot = db.get(HistoricalSnapshot, SNAPSHOT_ID)
        snapshot.status = state["status"]
        db.commit()
    print(json.dumps({"status": state["status"], "done": len(state["done"]), "files": state["files"],
                      "calls": state["calls"], "failures": len(state["failures"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
