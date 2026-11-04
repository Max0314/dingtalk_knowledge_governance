"""Small page checkpoints, independent of document rows and payloads."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, id_string, utcnow


class ScanProgress(Base):
    __tablename__ = 'scan_progress'
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(id_string(), index=True)
    cycle_month: Mapped[str] = mapped_column(String(7), default='')
    phase: Mapped[str] = mapped_column(String(16), default='walk')
    seeding: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_cursor: Mapped[str] = mapped_column(id_string(), default='')


class ScanDirectory(Base):
    __tablename__ = 'scan_directories'
    __table_args__ = (Index('ix_scan_directory_pending', 'run_id', 'done', 'parent_id'),)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    next_token: Mapped[str] = mapped_column(String(2048), default='')
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class ScanSeen(Base):
    __tablename__ = 'scan_seen'
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(id_string(), primary_key=True)


class ScanPageToken(Base):
    __tablename__ = 'scan_page_tokens'
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str] = mapped_column(id_string(), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
