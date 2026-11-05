from datetime import datetime
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import watcher
from app.db import Base, ScanCycle


@pytest.mark.parametrize('status',[None,'completed','completed_with_errors','budget_exhausted'])
def test_bounded_watcher_never_creates_or_reopens_cycle(tmp_path,monkeypatch,status):
    bind=create_engine('sqlite:///'+str(tmp_path/'watch.db'));Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind)
    month=datetime.now(watcher.CST).strftime('%Y-%m')
    if status:
        with sessions.begin() as db:db.add(ScanCycle(month=month,status=status))
    monkeypatch.setattr(watcher,'SessionLocal',sessions)
    monkeypatch.setattr(watcher,'init_db',lambda:None)
    monkeypatch.setattr(watcher,'get_settings',lambda:SimpleNamespace(full_scan_enabled=True))
    def forbidden(*a,**k):raise AssertionError('unapproved cycle must not scan or sweep')
    monkeypatch.setattr(watcher,'run_scan_slice',forbidden)
    monkeypatch.setattr(watcher,'sweep_stale_runs',forbidden)
    watcher.main(['--cycle-month',month])
    with sessions() as db:
        row=db.get(ScanCycle,month)
        assert (row.status if row else None)==status
    bind.dispose()


def test_bounded_watcher_exits_after_its_cycle_and_refuses_other_month(tmp_path,monkeypatch):
    bind=create_engine('sqlite:///'+str(tmp_path/'watch.db'));Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind)
    month=datetime.now(watcher.CST).strftime('%Y-%m')
    with sessions.begin() as db:db.add(ScanCycle(month=month,status='running'))
    monkeypatch.setattr(watcher,'SessionLocal',sessions)
    monkeypatch.setattr(watcher,'init_db',lambda:None)
    monkeypatch.setattr(watcher,'get_settings',lambda:SimpleNamespace(full_scan_enabled=True))
    monkeypatch.setattr(watcher,'sweep_stale_runs',lambda *a,**k:0)
    @contextmanager
    def unlocked(*a):yield True
    monkeypatch.setattr(watcher,'scan_lock',unlocked)
    monkeypatch.setattr(watcher.time,'sleep',lambda *a:None)
    calls=[]
    def advance(db,s,*,expected_month=None):
        calls.append(1)
        assert expected_month==month
        assert not db.in_transaction()
        db.get(ScanCycle,month).status='completed';db.commit()
        return {'walked':[{'status':'succeeded'}]}
    monkeypatch.setattr(watcher,'run_scan_slice',advance)
    watcher.main(['--cycle-month',month])
    assert calls==[1]
    with sessions() as db:
        assert not watcher.cycle_is_runnable(db,'2000-01')
        assert watcher.cycle_is_runnable(db,None)
    bind.dispose()


def test_scheduler_rechecks_bound_month_inside_lock(tmp_path,monkeypatch):
    from app import scan_scheduler
    from app.config import Settings
    bind=create_engine('sqlite:///'+str(tmp_path/'boundary.db'));Base.metadata.create_all(bind)
    sessions=sessionmaker(bind=bind)
    with sessions.begin() as db:db.add(ScanCycle(month='2026-11',status='running'))
    class December(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,12,1,tzinfo=tz)
    monkeypatch.setattr(scan_scheduler,'datetime',December)
    async def forbidden(*a,**k):raise AssertionError('must not resolve or open a new month')
    monkeypatch.setattr(scan_scheduler,'resolve_watch_targets',forbidden)
    s=Settings(_env_file=None,full_scan_enabled=True,watch_workspaces='*',scan_days='1')
    with sessions() as db:
        assert scan_scheduler.run_scan_slice(db,s,expected_month='2026-11')['status']=='cycle_outside_month'
        assert db.get(ScanCycle,'2026-12') is None
    bind.dispose()
