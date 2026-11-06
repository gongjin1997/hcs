"""新租户默认关闭采集，保存域名可空，认证正确传递账号 ID。"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.tenants import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.hcs.auth import TokenProvider
from app.main import app as production_app
from app.models import CollectSwitch, HcsAccount
from app.services import collect_switch


@pytest.fixture()
def tenant_api(mem_db):
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username='admin', display_name='管理员', permissions=['tenant:manage', 'tenant:view'], tenant_ids=None,
    )
    with TestClient(app) as client:
        yield client


def tenant_body():
    return {'code': 'new-tenant', 'name': '新租户', 'iam_endpoint': 'https://iam.example.invalid',
            'auth_type': 'AKSK', 'access_key': 'local-test-ak', 'secret': 'local-test-sk',
            'projects': [{'region_id': 'region-1', 'project_id': 'project-1', 'external_domain': 'example.invalid'}]}


def test_new_tenant_collection_is_off_and_legacy_tenants_are_unchanged(mem_db, tenant_api):
    legacy = HcsAccount(code='legacy', name='已有租户', domain_name='legacy', iam_endpoint='https://iam.example.invalid')
    mem_db.add(legacy)
    mem_db.add(CollectSwitch(tenant_id=0, resource_type='ecs_server', enabled=True))
    mem_db.commit()
    response = tenant_api.post('/api/v1/system/tenants', json=tenant_body())
    assert response.status_code == 200, response.text
    new = response.json()['data']
    assert new['domain_name'] == ''
    switches = collect_switch.load(mem_db)
    assert all(not switches.enabled(new['id'], kind) for kind in collect_switch.SWITCH_TYPES)
    assert all(not switches.task_enabled(new['id'], task) for task in collect_switch.TASK_TYPES)
    assert all(switches.enabled(legacy.id, kind) for kind in collect_switch.SWITCH_TYPES)
    collect_switch.update(mem_db, [{'resource_type': 'ecs_server', 'tenant_id': new['id'], 'enabled': True}], 'admin')
    mem_db.commit()
    assert tenant_api.put('/api/v1/system/tenants/'+str(new['id']), json={**tenant_body(), 'remark': '更新'}).status_code == 200
    assert collect_switch.load(mem_db).enabled(new['id'], 'ecs_server')
    assert not collect_switch.load(mem_db).enabled(new['id'], 'dcs_instance')
    assert tenant_api.delete('/api/v1/system/tenants/'+str(new['id'])).status_code == 200
    assert list(mem_db.scalars(select(CollectSwitch).where(CollectSwitch.tenant_id == new['id']))) == []


@pytest.mark.parametrize('domain', [None, '', '  '])
def test_optional_domain_accepts_null_or_empty(domain, tenant_api):
    body = {**tenant_body(), 'domain_name': domain}
    response = tenant_api.post('/api/v1/system/tenants', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['data']['domain_name'] == ''


@pytest.mark.parametrize(('name', 'domain_id', 'expected'), [('name', None, {'name': 'name'}),
                                                          ('', 'domain-id', {'id': 'domain-id'}),
                                                          ('', None, None)])
def test_token_provider_uses_domain_id_without_sending_blank_domain(name, domain_id, expected):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, headers={'X-Subject-Token': 'test-token'},
                              json={'token': {'expires_at': '2099-01-01T00:00:00Z'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        provider = TokenProvider(http, 'https://iam.example.invalid', name, 'user', 'password', 'project', domain_id=domain_id)
        assert provider.get() == 'test-token'
    user = requests[0]['auth']['identity']['password']['user']
    assert user.get('domain') == expected
