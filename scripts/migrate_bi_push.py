"""Idempotent additive migration. Run before starting the new API/workers."""
import json
from sqlalchemy import inspect, text
from app.db import engine
from app import bi_models

ADDITIONS = {
    "sync_runs": {"collection_started_at": "DATETIME(6) NULL"},
    "file_audit_events": {"metadata_applied_at": "DATETIME(6) NULL"},
    "workspaces": {"metadata_confirmed_at": "DATETIME(6) NULL"},
    "documents": {"source_creator_key": "VARCHAR(128) COLLATE utf8mb4_bin NULL", "source_creator_kind": "VARCHAR(16) NULL", "metadata_applied_at": "DATETIME(6) NULL"},
    "scan_cycles": {"recovery_id": "VARCHAR(36) NULL", "target_workspace_ids_json": "TEXT NULL"},
    "scan_tasks": {"recovery_id": "VARCHAR(36) NULL"},
    "audit_state": {**{x: "BIGINT NULL" for x in ("window_start_ms", "window_end_ms", "next_gmt_create", "completed_end_ms", "gap_since_ms")}, "next_biz_id": "VARCHAR(128) NULL", "gap_reason": "VARCHAR(64) NULL", "inventory_reconciled_at": "DATETIME(6) NULL"},
}


def migrate(bind=engine):
    for value in vars(bi_models).values():
        if isinstance(value, type) and value.__module__ == bi_models.__name__ and hasattr(value, "__table__"):
            value.__table__.create(bind, checkfirst=True)
    with bind.begin() as c:
        if bind.dialect.name == "mysql":
            c.execute(text("SET SESSION lock_wait_timeout=3"))
        for table, columns in ADDITIONS.items():
            existing = {x["name"] for x in inspect(c).get_columns(table)}
            for column, kind in columns.items():
                if column not in existing:
                    sql_type = kind if bind.dialect.name == "mysql" else kind.replace("(6)", "").replace(" COLLATE utf8mb4_bin", "")
                    c.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        indexes = {x["name"] for x in inspect(c).get_indexes("documents")}
        if "ix_documents_metadata_applied_at" not in indexes:
            c.execute(text("CREATE INDEX ix_documents_metadata_applied_at ON documents(metadata_applied_at)"))
        c.execute(text("CREATE TABLE IF NOT EXISTS kg_schema_migrations (version VARCHAR(64) PRIMARY KEY, applied_at DATETIME NOT NULL)"))
        query = "INSERT IGNORE" if bind.dialect.name == "mysql" else "INSERT OR IGNORE"
        c.execute(text(query + " INTO kg_schema_migrations VALUES ('bi-push-v1-20261104',CURRENT_TIMESTAMP)"))
    return {"migration": "bi-push-v1-20261104", "ok": True}


if __name__ == "__main__":
    print(json.dumps(migrate()))
