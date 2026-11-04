import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import bi_projection, metrics, page_metrics
from app.bi_models import BIIdentityMonth, BIDirtyScope
from app.bi_projection import seed_slice, project_dirty
from app.config import Settings
from app.db import Base, Document, EmployeeOrgMonth, HistoricalFileNode, HistoricalSnapshot, Workspace
from app.org_month_cache import prepare_month
from app.page_identity import prepare_slice


@pytest.fixture
def local(tmp_path, monkeypatch):
    bind = create_engine('sqlite:///' + str(tmp_path/'metrics.db'))
    Base.metadata.create_all(bind)
    sessions = sessionmaker(bind=bind, autoflush=False)
    s = Settings(_env_file=None, metadata_only=True, content_extract_enabled=False)
    for module in (metrics, page_metrics, bi_projection):
        monkeypatch.setattr(module, 'get_settings', lambda: s)
    bi_projection.install_change_hooks()
    with sessions.begin() as db:
        db.add_all([Workspace(workspace_id='good', name='C-Synthetic', owner_department_name='New', metadata_confirmed_at=datetime.now(timezone.utc)),
                    Workspace(workspace_id='personal', name='I-Synthetic', metadata_confirmed_at=datetime.now(timezone.utc)),
                    HistoricalSnapshot(snapshot_id='synthetic-baseline', source='test', definition={'is_primary_baseline': True, 'creator_identity_kind': 'union_id'})])
        db.flush()
        db.add_all([HistoricalFileNode(snapshot_id='synthetic-baseline', node_id='base', workspace_id='good', node_type='file', creator_user_id='union-a', source_created_at='2026-10-31T18:00:00Z'),
                    Document(node_id='base', workspace_id='good', name='Synthetic', source_created_at='2026-01-01'),
                    Document(node_id='nov', workspace_id='good', name='Synthetic', source_created_at='2026-11-02', source_creator_kind='user_id', source_creator_key='user-x'),
                    Document(node_id='oct', workspace_id='good', name='Synthetic', source_created_at='2026-10-02', source_creator_kind='user_id', source_creator_key='user-x'),
                    Document(node_id='own', workspace_id='personal', name='Synthetic', source_created_at='2026-11-03', source_creator_kind='user_id', source_creator_key='user-personal')])
        for m, source, kind, employee, dept in [('2026-11','union-a','union_id','employee-a','New'),
            ('2026-11','user-x','user_id','employee-x','New'),('2026-10','user-x','user_id','employee-x','Old'),
            ('2026-11','user-personal','user_id','employee-personal','New')]:
            db.add(BIIdentityMonth(month=m, source_key=source, identity_kind=kind, employee_key=employee,
                   resolve_status='matched', include_official=True, directory_version='v', policy_version='p'))
            db.add(EmployeeOrgMonth(month=m, employee_key=employee, employee_name='Synthetic '+employee, department_name=dept,
                   biz_group_name='Synthetic group', directory_version='v', policy_version='p', details_ready=True, is_rd_system=True))
    while True:
        with sessions.begin() as db:
            if seed_slice(db)['complete']: break
    with sessions.begin() as db:
        project_dirty(db)
    metrics.invalidate_cache()
    yield sessions, bind
    metrics.invalidate_cache()
    bind.dispose()


def test_production_statistics_use_preaggregates_and_keep_cst_dedup(local):
    sessions, bind = local
    statements = []
    def record(conn, cursor, sql, *args): statements.append(sql.lower())
    event.listen(bind, 'before_cursor_execute', record)
    with sessions() as db:
        d = metrics.collected(db)
        assert d['total_files'] == 4 and d['monthly'] == {'2026-10': 1, '2026-11': 3}
        assert page_metrics.counts(db) == {'good': 3, 'personal': 1}
        org = metrics.org_rollup(db, month='2026-10')
        assert next(r for r in org['items'] if r['department_name']=='Old')['delta'] == 1
        person = metrics.uploader_breakdown(db, 'employee-x', month='2026-10')
        assert person['department_name'] == 'Old' and person['period_total'] == 1 and person['matched']
        assert metrics.uploaders(db, month='2026-11')['total_files'] == 3
        assert metrics.increments_tree(db, month='2026-11', scope='rd_system')['rows'][0]['key'] == '2026-11-01'
    event.remove(bind, 'before_cursor_execute', record)
    assert not any('from documents' in q or 'from historical_file_nodes' in q for q in statements)


def test_personal_only_and_old_month_are_prepared_without_expanding_bi_scope(local):
    sessions, _ = local
    with sessions.begin() as db:
        from app.bi_models import BIUploadDay
        db.add(BIUploadDay(workspace_id='personal', month='2020-01', day='2020-01-01', identity_kind='user_id', source_key='old-personal', file_count=1))
        db.add(BIDirtyScope(scope_key='page_month/2020-01'))
        # Clear newer page-cache work to select the older personal-only month.
        db.query(BIDirtyScope).filter(BIDirtyScope.scope_key.like('page_month/2026%')).delete(synchronize_session=False)
    calls=[]
    version={'month':'2020-01','directoryVersion':'old-v','policyVersion':'p','resolvedSnapshotMonth':'2020-01'}
    class Remote:
        async def master_status(self): return {'policyVersion':'p','monthlyDirectoryVersions':{'2020-01':'old-v'}}
        async def directory_version(self, month): return version
        async def employee(self, path, params):
            calls.append(params)
            return {**version,'total':1,'items':[{'employeeKey':'old-employee','name':'Synthetic','dept':'Historical department','biz':'Historical group','isRdSystem':True,'isActive':False}]}
        async def resolve(self, keys):
            return [{**version,'status':'matched','employeeKey':'old-employee','matched':True,'includeInOfficialStats':True} for _ in keys]
    assert asyncio.run(prepare_slice(sessions, Remote()))
    with sessions() as db:
        assert db.get(BIIdentityMonth, ('2020-01','user_id','old-personal')).include_official
        assert db.scalar(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month=='2020-01')).department_name == 'Historical department'
        assert db.get(BIDirtyScope, 'page_month/2020-01') is None
    assert calls and calls[0]['includeInactive'] == 'true'


def test_bad_org_pagination_preserves_previous_month_and_blank_names_do_not_refetch(local):
    sessions, _ = local
    version={'directoryVersion':'new-v','policyVersion':'p','resolvedSnapshotMonth':'2026-11'}
    class Invalid:
        async def employee(self,*args,**kwargs):
            return {**version,'total':2,'items':[{'employeeKey':'duplicate'},{'employeeKey':'duplicate'}]}
    with pytest.raises(RuntimeError, match='duplicate'):
        asyncio.run(prepare_month(sessions,'2026-11',version,Invalid(),{}))
    with sessions() as db:
        assert db.scalar(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month=='2026-11')).directory_version == 'v'
    with sessions.begin() as db:
        rows=db.scalars(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month=='2026-11')).all()
        for r in rows: r.employee_name=''
    class Forbidden:
        async def employee(self,*args,**kwargs): pytest.fail('ready month must not be rewritten for a blank display name')
    asyncio.run(prepare_month(sessions,'2026-11',{'directoryVersion':'v','policyVersion':'p','resolvedSnapshotMonth':''},Forbidden(),{}))


def test_org_page_total_change_does_not_replace_old_snapshot(local):
    sessions,_=local
    version={'directoryVersion':'new-v','policyVersion':'p','resolvedSnapshotMonth':'2026-11'}
    class Changing:
        async def employee(self,path,params):
            offset=params['offset']
            return {**version,'total':3 if not offset else 2,'offset':offset,
                    'items':[{'employeeKey':str(offset),'name':'Synthetic'}]}
    with pytest.raises(RuntimeError,match='inconsistent'):
        asyncio.run(prepare_month(sessions,'2026-11',version,Changing(),{}))
    with sessions() as db:
        assert db.scalar(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month=='2026-11')).directory_version=='v'


def test_identity_preparation_has_one_shared_nonblocking_writer(local):
    from app.bi_push import identity_lock
    sessions,_=local
    with identity_lock(sessions):
        with pytest.raises(RuntimeError,match='identity_prepare_busy'):
            with identity_lock(sessions): pytest.fail('second writer entered')
    with identity_lock(sessions): pass


def test_failed_month_is_delayed_and_other_month_can_progress(local,monkeypatch):
    from app import bi_push
    from app.db import utcnow
    sessions,_=local
    with sessions.begin() as db:
        db.query(BIDirtyScope).delete(synchronize_session=False)
        db.add_all([BIDirtyScope(scope_key='page_month/2026-11',requested_at=utcnow()),
                    BIDirtyScope(scope_key='page_month/2026-10',requested_at=utcnow())])
    calls=[]
    async def prepare(sessions,inputs,remote):
        month=next(iter(inputs));calls.append(month)
        if month=='2026-11': raise RuntimeError('synthetic bad month')
    monkeypatch.setattr(bi_push,'prepare_identities',prepare)
    with pytest.raises(RuntimeError):asyncio.run(prepare_slice(sessions,object()))
    assert asyncio.run(prepare_slice(sessions,object()))
    assert calls==['2026-11','2026-10']


def test_page_scan_migration_is_idempotent_and_only_queues_aggregate_months(local):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import inspect, text
    sessions,bind=local
    script=Path(__file__).resolve().parents[1]/'scripts/migrate_page_scan.py'
    spec=importlib.util.spec_from_file_location('synthetic_page_migration',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with bind.begin() as conn:
        conn.execute(text('ALTER TABLE employee_org_month_cache DROP COLUMN employee_name'))
        conn.execute(text('ALTER TABLE employee_org_month_cache DROP COLUMN details_ready'))
    assert module.migrate(bind)['ok']
    with sessions() as db:
        generations={r.scope_key:r.generation for r in db.scalars(select(BIDirtyScope))}
    assert module.migrate(bind)['ok']
    with sessions() as db:
        assert {r.scope_key:r.generation for r in db.scalars(select(BIDirtyScope))}==generations
    columns={r['name'] for r in inspect(bind).get_columns('employee_org_month_cache')}
    assert {'employee_name','details_ready'}<=columns
