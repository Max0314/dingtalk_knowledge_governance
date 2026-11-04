from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import bi_projection, main, page_metrics, read_cache
from app.bi_models import FileInventoryVersion
from app.config import Settings
from app.db import Base, Document, HistoricalFileNode, HistoricalSnapshot, ReadCacheEntry, Workspace, utcnow


@pytest.fixture
def local(tmp_path,monkeypatch):
    bind=create_engine('sqlite:///'+str(tmp_path/'inventory.db'))
    Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind,autoflush=False)
    settings=Settings(_env_file=None,demo_mode=False,metadata_only=True,review_enabled=False,content_extract_enabled=False)
    monkeypatch.setattr(bi_projection,'get_settings',lambda:settings)
    monkeypatch.setattr(page_metrics,'get_settings',lambda:settings)
    bi_projection.install_change_hooks()
    with sessions.begin() as db:
        db.add(Workspace(workspace_id='synthetic',name='C-Synthetic'))
        db.add(HistoricalSnapshot(snapshot_id='baseline',source='test'))
        db.flush()
        db.add(HistoricalFileNode(snapshot_id='baseline',workspace_id='synthetic',node_id='base-only',node_type='file'))
        db.add(Document(workspace_id='synthetic',node_id='live',name='Synthetic'))
    with sessions.begin() as db:
        payload=main.file_inventory(db,snapshot='baseline')
        db.add(ReadCacheEntry(cache_key=read_cache._key('file_inventory',{'snapshot':'baseline'}),
                              endpoint='file_inventory',parameters={'snapshot':'baseline'},payload=payload,generated_at=utcnow()))
    yield sessions,bind
    bind.dispose()


def test_unchanged_inventory_rechecks_only_small_version_tables(local):
    sessions,bind=local;sql=[]
    def record(conn,cursor,statement,*args):sql.append(statement.lower())
    event.listen(bind,'before_cursor_execute',record)
    with sessions() as db:
        result=main.file_inventory(db,snapshot='baseline')
        assert result['live_total']==1 and result['baseline_ids']==['base-only']
    event.remove(bind,'before_cursor_execute',record)
    assert not any('from documents' in q or 'from historical_file_nodes' in q for q in sql)


def test_late_scan_timestamp_and_equal_total_cannot_hide_topology_change(local):
    sessions,_=local
    with sessions.begin() as db:
        db.add(Document(workspace_id='synthetic',node_id='base-only',name='Synthetic',metadata_applied_at=utcnow()-timedelta(days=1)))
    with sessions.begin() as db:
        result=main.file_inventory(db,snapshot='baseline')
        assert result['live_total']==2 and result['baseline_ids']==[]
        db.get(ReadCacheEntry,read_cache._key('file_inventory',{'snapshot':'baseline'})).payload=result
    with sessions.begin() as db:db.delete(db.get(Document,'base-only'))
    with sessions() as db:
        result=main.file_inventory(db,snapshot='baseline')
        assert result['live_total']==1 and result['baseline_ids']==['base-only']


def test_rollback_does_not_invalidate_inventory_and_workspace_hide_does(local):
    sessions,_=local
    with sessions() as db:
        revision=db.get(FileInventoryVersion,'synthetic').revision
        db.get(Document,'live').is_deleted=True
        db.flush();db.rollback()
        assert db.get(FileInventoryVersion,'synthetic').revision==revision
        assert main.file_inventory(db,snapshot='baseline')['live_total']==1
    with sessions.begin() as db:db.get(Workspace,'synthetic').is_active=False
    with sessions() as db:
        result=main.file_inventory(db,snapshot='baseline')
        assert result['live_total']==0 and result['baseline_ids']==[]


def test_non_inventory_metadata_does_not_write_version(local):
    sessions,_=local
    with sessions.begin() as db:
        revision=db.get(FileInventoryVersion,'synthetic').revision
        db.get(Document,'live').name='Renamed metadata'
    with sessions() as db:
        assert db.get(FileInventoryVersion,'synthetic').revision==revision
