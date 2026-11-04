"""Add only small organization/checkpoint schema; no document table rebuild."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import inspect, select, text
from app.db import EmployeeOrgMonth, engine
from app import scan_models
from app.bi_models import BIUploadDay, BIDirtyScope, BIIdentityMonth, PageIdentityState
from app.bi_projection import enqueue
from sqlalchemy.orm import Session


def migrate(bind=engine):
    with bind.connect() as conn:
        locked = bind.dialect.name != 'mysql' or conn.scalar(text("SELECT GET_LOCK('kg_schema_migration',0)")) == 1
        if not locked:
            raise RuntimeError('page_scan_migration_busy')
        conn.commit()
        try:
            for model in (scan_models.ScanProgress, scan_models.ScanDirectory, scan_models.ScanSeen, scan_models.ScanPageToken):
                model.__table__.create(conn, checkfirst=True)
                conn.commit()
            PageIdentityState.__table__.create(conn, checkfirst=True)
            conn.commit()
            indexes = inspect(conn).get_indexes(BIIdentityMonth.__tablename__)
            if not any(i['column_names'][:1] == ['employee_key'] for i in indexes):
                if bind.dialect.name == 'mysql':
                    conn.execute(text('SET SESSION lock_wait_timeout=3'))
                    conn.execute(text('ALTER TABLE kg_bi_identity_month ADD INDEX ix_kg_bi_identity_employee (employee_key,month), ALGORITHM=INPLACE, LOCK=NONE'))
                else:
                    conn.execute(text('CREATE INDEX ix_kg_bi_identity_employee ON kg_bi_identity_month(employee_key,month)'))
                conn.commit()
            existing = {c['name'] for c in inspect(conn).get_columns(EmployeeOrgMonth.__tablename__)}
            for name, kind in [('employee_name', "VARCHAR(128) NOT NULL DEFAULT ''"), ('details_ready', 'BOOLEAN NOT NULL DEFAULT 0')]:
                if name not in existing:
                    if bind.dialect.name == 'mysql':
                        conn.execute(text('SET SESSION lock_wait_timeout=3'))
                    suffix = ', ALGORITHM=INSTANT' if bind.dialect.name == 'mysql' else ''
                    conn.execute(text('ALTER TABLE employee_org_month_cache ADD COLUMN '+name+' '+kind+suffix))
                    conn.commit()
            conn.execute(text('CREATE TABLE IF NOT EXISTS kg_schema_migrations (version VARCHAR(64) PRIMARY KEY, applied_at DATETIME NOT NULL)'))
            applied = conn.scalar(text("SELECT version FROM kg_schema_migrations WHERE version='page-scan-v1-20261104'"))
            conn.commit()
            if not applied:
                # Once, only from preaggregates. No source reseed or file scan.
                with Session(bind=conn) as db:
                    months = list(db.scalars(select(BIUploadDay.month).distinct()))
                    enqueue(db, BIDirtyScope, ['page_month/'+m for m in months if m])
                    db.execute(text("INSERT INTO kg_schema_migrations VALUES ('page-scan-v1-20261104',CURRENT_TIMESTAMP)"))
                    db.commit()
            return {'migration': 'page-scan-v1-20261104', 'ok': True}
        finally:
            if bind.dialect.name == 'mysql':
                conn.execute(text("SELECT RELEASE_LOCK('kg_schema_migration')"))
                conn.commit()


if __name__ == '__main__':
    print(json.dumps(migrate()))
