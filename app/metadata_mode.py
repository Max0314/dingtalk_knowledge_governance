"""Shared metadata-write clock and side-effect gate."""
from datetime import timezone
from sqlalchemy import text
from .db import utcnow


def metadata_only(settings):
    return bool(getattr(settings, "metadata_only", False))


def database_now(db):
    if db.bind.dialect.name == "mysql":
        return db.scalar(text("SELECT UTC_TIMESTAMP(6)"))
    return utcnow().replace(tzinfo=None)


def naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
