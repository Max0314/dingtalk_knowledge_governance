"""Backfill workspaces.owner_department_name from the Yida knowledge-base
registry (知识库基本信息表, read via bi_center).

Default input is the slim mapping shipped alongside this script (2026-08-03
capture, 144 libs, 136 with department). A fresh raw dump from the Yida probe
can be passed with --input; both the slim format ({"items": [...]}) and the
raw capture format ({"knowledge_base_info": {"data": [...]}}) are accepted.

Dry-run by default — prints what would change. Apply with --apply.
Manually-set departments are kept unless --force is given.

Usage (inside the api container):
    python scripts/backfill_yida_departments.py            # preview
    python scripts/backfill_yida_departments.py --apply    # write
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal, Workspace, init_db

DEFAULT_INPUT = Path(__file__).resolve().parent / "yida_workspace_departments.json"
PLACEHOLDERS = {"", "-", "—", "未映射", "未知"}  # 视同空值，可被宜搭数据覆盖


def parse_raw(payload: dict) -> list[dict]:
    """Extract slim rows from a raw Yida capture (instanceValue field soup)."""
    rows = []
    for row in payload["knowledge_base_info"]["data"]:
        fields = {x["fieldId"]: (x.get("fieldData") or {}) for x in json.loads(row["instanceValue"])}
        url = fields.get("textField_mr8nshi6", {}).get("value") or ""
        match = re.search(r"/spaces/([A-Za-z0-9]+)", url)
        dept = ""
        dept_value = fields.get("departmentSelectField_mra0le5v", {}).get("value")
        if isinstance(dept_value, list) and dept_value and isinstance(dept_value[0], dict):
            text = dept_value[0].get("text")
            dept = (text.get("zh_CN") or text.get("en_US") or "") if isinstance(text, dict) else (text or "")
        biz = fields.get("selectField_mdgsbqwc", {}).get("value") or ""
        rows.append({"workspace_id": match.group(1) if match else "",
                     "space_uuid": fields.get("textField_mr8nshhu", {}).get("value") or "",
                     "name": fields.get("textField_mr8nshi3", {}).get("value") or "",
                     "department": dept,
                     "biz_domain": biz if isinstance(biz, str) else ""})
    return rows


def main() -> None:
    apply_changes = "--apply" in sys.argv
    force = "--force" in sys.argv
    input_path = DEFAULT_INPUT
    if "--input" in sys.argv:
        input_path = Path(sys.argv[sys.argv.index("--input") + 1])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    items = payload["items"] if "items" in payload else parse_raw(payload)

    init_db()
    updated, kept, missing_dept, unmatched = [], [], [], []
    match_by = {"workspace_id": 0, "space_uuid": 0, "url_slug": 0, "name": 0}
    with SessionLocal() as db:
        rows = db.scalars(select(Workspace)).all()
        registry = {ws.workspace_id: ws for ws in rows}
        # 注册表主键的命名空间随采集通道而异（官方 API id / alidocs URL slug），
        # 因此按 主键 → spaceUuid → 注册表 url 里的 slug → 唯一名称 依次匹配。
        by_url_slug = {}
        for ws in rows:
            slug = re.search(r"/spaces/([A-Za-z0-9]+)", ws.url or "")
            if slug:
                by_url_slug[slug.group(1)] = ws
        name_counts: dict[str, int] = {}
        for ws in rows:
            name_counts[ws.name] = name_counts.get(ws.name, 0) + 1
        by_name = {ws.name: ws for ws in rows if name_counts[ws.name] == 1}

        def resolve(item: dict):
            slug = item.get("workspace_id") or ""
            uuid_ = item.get("space_uuid") or ""
            if slug and slug in registry:
                return registry[slug], "workspace_id"
            if uuid_ and uuid_ in registry:
                return registry[uuid_], "space_uuid"
            if slug and slug in by_url_slug:
                return by_url_slug[slug], "url_slug"
            name = (item.get("name") or "").strip()
            if name and name in by_name:
                return by_name[name], "name"
            return None, ""

        for item in items:
            ws, how = resolve(item)
            if ws is None:
                unmatched.append(f'{item.get("name")}({item.get("workspace_id")})')
                continue
            match_by[how] += 1
            dept = (item.get("department") or "").strip()
            if not dept:
                missing_dept.append(ws.name)
                continue
            if ws.owner_department_name not in PLACEHOLDERS and ws.owner_department_name != dept and not force:
                kept.append(f"{ws.name}: 保留现值「{ws.owner_department_name}」（宜搭为「{dept}」）")
                continue
            if ws.owner_department_name != dept:
                updated.append(f"{ws.name}: 「{ws.owner_department_name}」→「{dept}」")
                if apply_changes:
                    ws.owner_department_name = dept
        if apply_changes:
            db.commit()

    print(json.dumps({
        "mode": "APPLIED" if apply_changes else "DRY-RUN（加 --apply 写入）",
        "yida_records": len(items),
        "updated": len(updated),
        "kept_manual_value": len(kept),
        "yida_missing_department": len(missing_dept),
        "not_in_registry": len(unmatched),
        "match_by": match_by,
    }, ensure_ascii=False, indent=1))
    if unmatched and len(unmatched) >= len(items) // 2:
        # 全线不匹配 ⇒ 十有八九是 id 命名空间对不上，给出注册表样本便于诊断
        with SessionLocal() as db:
            for ws in db.scalars(select(Workspace).limit(3)).all():
                print(f"[registry 样本] id={ws.workspace_id} name={ws.name} url={ws.url[:70]}")
    for label, bucket in (("更新", updated), ("保留人工值", kept), ("宜搭缺部门", missing_dept), ("registry 无此库", unmatched)):
        for line in bucket[:200]:
            print(f"[{label}] {line}")


if __name__ == "__main__":
    main()
