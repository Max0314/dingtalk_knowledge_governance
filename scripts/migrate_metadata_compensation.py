"""Create small compensation/source-watermark tables; no inventory ALTER/backfill."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import MetadataCompensationItem, MetadataCompensationRun, MetadataEventWatermark, engine


def migrate(bind=engine):
    for table in (MetadataCompensationRun.__table__, MetadataCompensationItem.__table__, MetadataEventWatermark.__table__):
        table.create(bind, checkfirst=True)
    return {'compensation_tables_ready': True, 'existing_tables_unchanged': True}


if __name__ == '__main__':
    print(json.dumps(migrate()))
