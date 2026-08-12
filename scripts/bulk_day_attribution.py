"""Who caused each bulk-import day? Joins the detected bulk days against the
primary snapshot's creator ids, names them via the employee cache, and marks
robot accounts. Counts and names only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app import metrics
from app.config import get_settings
from app.db import EmployeeMap, HistoricalFileNode, SessionLocal, init_db
from app.service import robot_keys


def main() -> None:
    settings = get_settings()
    robots = robot_keys(settings)
    init_db()
    with SessionLocal() as db:
        data = metrics.collected(db)
        snapshot = metrics.primary_snapshot_id(db)
        names = {row.user_id: row.name for row in db.scalars(select(EmployeeMap)).all()}
        days = sorted(data["bulk_days"], key=lambda item: -item["files"])[:10]
        out = []
        for day in days:
            creators = db.execute(
                select(HistoricalFileNode.creator_user_id, func.count())
                .where(HistoricalFileNode.snapshot_id == snapshot,
                       HistoricalFileNode.node_type != "folder",
                       HistoricalFileNode.source_created_at.like(day["day"] + "%"))
                .group_by(HistoricalFileNode.creator_user_id)
                .order_by(func.count().desc()).limit(4)).all()
            out.append({"day": day["day"], "files": day["files"],
                        "top_uploaders": [
                            {"name": names.get(user_id, "") or (user_id[:12] + "…"),
                             "count": count,
                             "robot": user_id in robots}
                            for user_id, count in creators]})
        robot_total = db.scalar(select(func.count()).select_from(HistoricalFileNode)
                                .where(HistoricalFileNode.snapshot_id == snapshot,
                                       HistoricalFileNode.node_type != "folder",
                                       HistoricalFileNode.creator_user_id.in_(robots))) or 0
        print(json.dumps({"bulk_days_total": len(data["bulk_days"]),
                          "robot_created_files_in_baseline": robot_total,
                          "top_bulk_days": out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
