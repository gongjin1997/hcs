"""单实例定位仅使用本地台账，复用 DCS 原有 report:view 与租户范围。"""
import pytest

from app.models import HcsAccount, HcsProject
from tests.test_dcs_history_api import dcs_api as dcs_api
from tests.test_dcs_history_api import resource


def test_exact_local_instance_does_not_depend_on_listing_page(dcs_api, mem_db):
    client, _user, _app, calls = dcs_api
    for number in range(55):
        resource(mem_db, id=number + 1, name=f"A-{number:02}")
    row = resource(mem_db, id=901, name="Z-同名实例", resource_id="cloud-uuid-not-a-local-key")
    resource(mem_db, id=902, name=row.name, tenant_id=2, project_ref=21)
    first_page = client.get('/api/v1/dcs/instances', params={'page_size': 50}).json()['data']
    assert 901 not in [item['id'] for item in first_page['items']]
    response = client.get('/api/v1/dcs/instances/901', headers={'X-Tenant-Id': '1'})
    assert response.status_code == 200, response.text
    detail = response.json()['data']
    assert detail['id'] == 901 and detail['tenant_id'] == 1 and detail['resource_id'] == row.resource_id
    assert detail['project_name'] == '项目11'
    assert calls == []


@pytest.mark.parametrize('permissions', [[], ['ledger:view'], ['task:run'], ['ledger:view', 'ledger:export']])
def test_detail_keeps_report_permission_contract(dcs_api, mem_db, permissions):
    client, user, _app, calls = dcs_api
    row = resource(mem_db)
    user.permissions = permissions
    assert client.get(f'/api/v1/dcs/instances/{row.id}').status_code == 403
    assert calls == []


@pytest.mark.parametrize('change', ['foreign', 'deleted', 'wrong_type', 'missing', 'wrong_header'])
def test_detail_never_crosses_resource_type_lifecycle_or_scope(dcs_api, mem_db, change):
    client, user, _app, calls = dcs_api
    row = resource(mem_db)
    if change == 'foreign':
        row.tenant_id, row.project_ref = 2, 21
    elif change == 'deleted':
        row.lifecycle = 'DELETED'
    elif change == 'wrong_type':
        row.resource_type = 'ecs_server'
    elif change == 'wrong_header':
        user.tenant_ids = None
    mem_db.commit()
    ident = 999999 if change == 'missing' else row.id
    headers = {'X-Tenant-Id': '2'} if change == 'wrong_header' else {'X-Tenant-Id': '1'}
    assert client.get(f'/api/v1/dcs/instances/{ident}', headers=headers).status_code == 404
    assert calls == []


def test_disabled_tenant_and_project_still_allow_local_history_identity(dcs_api, mem_db):
    client, _user, _app, calls = dcs_api
    row = resource(mem_db)
    mem_db.get(HcsAccount, 1).enabled = False
    mem_db.get(HcsProject, 11).enabled = False
    mem_db.commit()
    response = client.get(f'/api/v1/dcs/instances/{row.id}')
    assert response.status_code == 200, response.text
    assert calls == []


def test_wrong_project_association_does_not_leak_other_tenant_project_name(dcs_api, mem_db):
    client, _user, _app, calls = dcs_api
    row = resource(mem_db, project_ref=21)
    response = client.get(f'/api/v1/dcs/instances/{row.id}')
    assert response.status_code == 200, response.text
    assert response.json()['data']['project_name'] is None
    assert calls == []
