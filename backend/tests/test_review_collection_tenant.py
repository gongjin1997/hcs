"""租户连通性检查的 HTTP 所有权与当前首启用项目契约。"""
from unittest.mock import MagicMock

import pytest

from app.hcs import HcsError
from app.hcs.endpoints import EndpointResolver
from app.models import HcsAccount, HcsProject
from app.services import tenant_check


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("auth_ok", [False, True])
def test_tenant_check_closes_only_owned_http_clients(mem_db, monkeypatch, external, auth_ok):
    tenant = HcsAccount(code="review", name="review", domain_name="review", iam_endpoint="https://example.invalid",
                        auth_type="AKSK")
    mem_db.add(tenant)
    mem_db.flush()
    for number in (1, 2):
        mem_db.add(HcsProject(account_id=tenant.id, region_id=f"r{number}", project_id=f"p{number}",
                              external_domain="example.invalid", enabled=True, workspaces=[]))
    mem_db.commit()
    client = MagicMock()
    responses = {"iam": {"projects": []}, "ecs": {"servers": [], "count": 0}, "cce": {"items": []},
                 "dcs": {"instances": []}, "gaussdb": {"instances": []}, "mrs": {"clusters": [], "clusterTotal": "0"}}
    client.get.side_effect = lambda service, *args, **kwargs: responses[service]
    client.resolver = EndpointResolver({service: "https://example.invalid" for service in responses}, "r1", "example.invalid")
    if not auth_ok:
        client.get.side_effect = HcsError(401, "AUTH", "authentication failed")
    client.mock_url = None
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(tenant_check, "client_for", factory)
    external_http = client.http if external else None
    result = tenant_check.check_tenant(mem_db, tenant, http=external_http)
    assert result["ok"] is auth_ok
    project = factory.call_args.args[1]
    assert project.project_id == "p1"
    assert factory.call_count == 1
    if external:
        client.http.close.assert_not_called()
    else:
        client.http.close.assert_called_once()
