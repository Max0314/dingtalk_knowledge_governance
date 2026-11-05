"""Instant nullable 64-bit byte columns; never rewrite or backfill live tables."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import inspect, text
from app.db import engine


def migrate(bind=engine):
    added = []
    with bind.connect() as conn:
        mysql = conn.dialect.name == 'mysql'
        if mysql:
            got = conn.scalar(text("SELECT GET_LOCK('kg_schema_migration',0)")) == 1
            conn.commit()
            if not got:
                raise RuntimeError('size_bytes_migration_busy')
        try:
            if mysql:
                conn.execute(text('SET SESSION lock_wait_timeout=3'))
                for table in ('documents', 'file_audit_events'):
                    legacy = next(c for c in inspect(conn).get_columns(table) if c['name']=='size')
                    if getattr(legacy['type'], 'unsigned', False):
                        raise RuntimeError('legacy_size_must_be_signed')
            for table in ('documents', 'file_audit_events'):
                if 'size_bytes' not in {c['name'] for c in inspect(conn).get_columns(table)}:
                    suffix = ', ALGORITHM=INSTANT' if mysql else ''
                    conn.execute(text('ALTER TABLE '+table+' ADD COLUMN size_bytes BIGINT NULL'+suffix))
                    conn.commit()
                    added.append(table)
            return {'added_size_bytes': added, 'no_backfill': True}
        finally:
            if mysql:
                conn.execute(text("SELECT RELEASE_LOCK('kg_schema_migration')"))
                conn.commit()


if __name__ == '__main__':
    print(json.dumps(migrate()))
