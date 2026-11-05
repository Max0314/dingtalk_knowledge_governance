import asyncio
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import diagnostics, integrations, main, performance
from app.config import Settings
from app.db import Base, ModelConfig


@pytest.fixture
def page(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'page.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    settings = Settings(_env_file=None, metadata_only=True, content_extract_enabled=False,
                        auth_enabled=False, DINGTALK_APP_KEY='synthetic', DINGTALK_APP_SECRET='synthetic',
                        DINGTALK_SYNC_OPERATOR_ID='synthetic')
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    monkeypatch.setattr(main, 'SessionLocal', sessions)
    def db_session():
        with sessions() as db:
            yield db
    main.app.dependency_overrides[main.db_session] = db_session
    with sessions.begin() as db:
        db.add(ModelConfig(name='Synthetic model', model_name='synthetic-model', enabled=True,
                           base_url='https://model.invalid/v1', api_key='synthetic-key'))
    yield TestClient(main.app), sessions, settings
    main.app.dependency_overrides.pop(main.db_session, None)
    engine.dispose()


def test_page_and_model_pause_do_not_call_remote(page, monkeypatch):
    client, _, _ = page
    def forbidden(*args, **kwargs):
        pytest.fail('Opening diagnostics must not call a remote service')
    monkeypatch.setattr(integrations, 'outbound_client', forbidden)
    result = client.get('/api/v1/diagnostics/connectivity')
    assert result.status_code == 200
    assert next(r for r in result.json()['items'] if r.get('target') == 'model')['status'] == 'paused'
    assert client.get('/api/v1/model-configs').json()['items'][0]['model_name'] == 'synthetic-model'
    assert client.post('/api/v1/diagnostics/connectivity/model').json()['status'] == 'paused'
    assert client.post('/api/v1/model-configs/1/connection-check').json()['status'] == 'paused'


def test_failed_external_probe_stays_in_its_section(page, monkeypatch):
    client, _, _ = page
    async def fail(*args, **kwargs):
        raise httpx.ConnectError('synthetic private upstream information')
    monkeypatch.setattr(integrations.DingtalkClient, 'list_workspaces', fail)
    result = client.post('/api/v1/diagnostics/connectivity/dingtalk')
    assert result.status_code == 200 and result.json()['status'] == 'failed'
    assert 'private' not in result.text
    assert client.get('/api/v1/diagnostics/connectivity').status_code == 200
    assert client.post('/api/v1/diagnostics/connectivity/unknown').status_code == 404


def test_admin_probe_preserves_budget_failure_code(page, monkeypatch):
    client, _, _ = page
    async def exhausted(*args, **kwargs):
        raise integrations.IntegrationError('dingtalk_budget_exhausted', 'synthetic', 429)
    monkeypatch.setattr(integrations.DingtalkClient, 'list_workspaces', exhausted)
    result = client.post('/api/v1/diagnostics/connectivity/dingtalk')
    assert result.json()['code'] == 'dingtalk_budget_exhausted'


def test_performance_reports_route_templates_without_ids(page):
    client, _, _ = page
    response = client.post('/api/v1/model-configs/987654/connection-check')
    assert response.status_code == 404
    assert 'app;dur=' in response.headers['server-timing']
    data = client.get('/api/v1/diagnostics/performance').json()
    assert any('{config_id}' in r['route'] for r in data['items'])
    assert '987654' not in str(data)


def test_runtime_status_is_local_and_available_to_non_admin_without_loading_overview(page,monkeypatch):
    client,_,_=page
    def forbidden(*a,**k):pytest.fail('runtime status must not call external services')
    monkeypatch.setattr(integrations,'outbound_client',forbidden)
    response=client.get('/api/v1/runtime/status')
    assert response.status_code==200
    data=response.json()
    assert isinstance(data['registered_workspaces'],int)
    assert data['review_enabled'] is False and data['notify_enabled'] is False
    assert data['scan']['total']==0


def test_runtime_progress_includes_pending_and_excludes_other_recovery(page):
    from datetime import datetime
    from app.api_usage import CST
    from app.db import ScanCycle,ScanTask
    client,sessions,_=page;month=datetime.now(CST).strftime('%Y-%m')
    with sessions.begin() as db:
        db.add(ScanCycle(month=month,recovery_id='current'))
        for workspace,status,recovery in [('pending','pending','current'),('failed','dead_letter','current'),('old','succeeded','prior')]:
            db.add(ScanTask(month=month,workspace_id=workspace,status=status,recovery_id=recovery))
    scan=client.get('/api/v1/runtime/status').json()['scan']
    assert scan['total']==2 and scan['failed']==1 and scan['remaining']==2 and scan['succeeded']==0
