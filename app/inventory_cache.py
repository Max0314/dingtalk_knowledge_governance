"""Reuse the inventory difference list until its transactional inputs change."""
import hashlib
import json

from sqlalchemy import select

from .bi_models import FileInventoryVersion
from .db import HistoricalSnapshot, ReadCacheEntry, Workspace


def previous(db, snapshot, parameters):
    from .read_cache import _key
    versions = db.execute(select(FileInventoryVersion.workspace_id, FileInventoryVersion.revision)
                          .order_by(FileInventoryVersion.workspace_id)).all()
    spaces = db.execute(select(Workspace.workspace_id, Workspace.is_active).order_by(Workspace.workspace_id)).all()
    baseline = db.get(HistoricalSnapshot, snapshot)
    stamp = hashlib.sha256(json.dumps([snapshot, str(baseline.collected_at) if baseline else '',
            [list(r) for r in versions], [list(r) for r in spaces]], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    cached = db.get(ReadCacheEntry, _key('file_inventory', parameters))
    payload = cached.payload if cached else None
    if isinstance(payload, dict) and payload.get('snapshot') == snapshot and payload.get('inventory_stamp') == stamp:
        return payload, stamp
    return None, stamp
