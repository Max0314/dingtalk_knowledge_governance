"""Add the bounded delete/restore lookup index without stopping collection."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text
from app.db import engine


def migrate(bind=engine):
    with bind.begin() as c:
        locked = False
        if bind.dialect.name == 'mysql':
            c.execute(text('SET SESSION lock_wait_timeout=3'))
            locked = bool(c.execute(text("SELECT GET_LOCK('kg_schema_migration',0)")).scalar())
            if not locked:
                raise RuntimeError('collector_index_migration_busy')
        try:
            indexes = inspect(c).get_indexes('documents')
            if any(i['column_names'] and i['column_names'][0] == 'storage_dentry_id' for i in indexes):
                return {'ok': True, 'created': False}
            if bind.dialect.name == 'mysql':
                c.execute(text('ALTER TABLE documents ADD INDEX ix_documents_storage_dentry_id (storage_dentry_id), ALGORITHM=INPLACE, LOCK=NONE'))
            else:
                c.execute(text('CREATE INDEX ix_documents_storage_dentry_id ON documents(storage_dentry_id)'))
            return {'ok': True, 'created': True}
        finally:
            if locked:
                c.execute(text("SELECT RELEASE_LOCK('kg_schema_migration')"))


if __name__ == '__main__':
    print(json.dumps(migrate()))
