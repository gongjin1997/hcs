"""AOM 配置入口及旧配置兼容；只使用临时库与本地 Mock。"""

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import AuditLog, HcsAccount
from app.services.tenant_check import check_tenant

from .conftest import MOCK_URL
from .test_user_tenant_review import management_api as management_api


def tenant_body():
    return {"code": "tenant-1", "name": "租户1", "iam_endpoint": "https://iam.invalid",
            "domain_name": "test-only", "auth_type": "PASSWORD", "username": "reader",
            "projects": [{"id": 11, "region_id": "region-test", "project_id": "project-11",
                          "external_domain": "example.invalid"}]}


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("endpoints", [{}, {"aom": "https://legacy.invalid"}])
def test_explicit_aom_source_is_rejected_before_tenant_mutation(mem_db, management_api, operation, endpoints):
    client = management_api[0]
    tenant = mem_db.get(HcsAccount, 1)
    before = (tenant.name, tenant.metrics_source, tenant.endpoints, tenant.secret_cipher)
    body = {**tenant_body(), "metrics_source": "AOM", "endpoints": endpoints, "remark": "不得写入"}
    if operation == "create":
        body.update(code="new-tenant", name="新租户", secret="synthetic-only")
        body["projects"][0].pop("id")
        response = client.post("/api/v1/system/tenants", json=body)
        assert mem_db.scalar(select(HcsAccount).where(HcsAccount.code == "new-tenant")) is None
    else:
        response = client.put("/api/v1/system/tenants/1", json=body)
    assert response.status_code == 400 and "AOM 已全局停用" in response.text
    mem_db.refresh(tenant)
    assert (tenant.name, tenant.metrics_source, tenant.endpoints, tenant.secret_cipher) == before
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action.in_(["tenant.create", "tenant.update"]))) is None


def test_legacy_aom_source_allows_unrelated_edit_and_is_reported_disabled(mem_db, management_api):
    client = management_api[0]
    tenant = mem_db.get(HcsAccount, 1)
    old = {"available": True, "reason": "旧元数据成功", "at": "2026-10-01T00:00:00+00:00"}
    tenant.metrics_source, tenant.username = "AOM", "reader"
    tenant.capabilities = {"aom": old}
    tenant.endpoints = {"aom": "https://legacy.invalid"}
    mem_db.commit()
    response = client.put("/api/v1/system/tenants/1", json={**tenant_body(), "remark": "正常备注",
                                                         "endpoints": tenant.endpoints})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["metrics_source"] == "AOM" and data["metrics_effective"] == "NONE"
    assert data["metrics_source_disabled"] is True and "停用" in data["metrics_source_note"]
    assert data["capabilities"]["aom"]["available"] is False and data["capabilities"]["aom"]["disabled"] is True
    assert tenant.metrics_source == "AOM" and tenant.remark == "正常备注" and tenant.capabilities["aom"] == old
    listed = next(row for row in client.get("/api/v1/system/tenants").json()["data"] if row["id"] == 1)
    assert listed["metrics_effective"] == "NONE" and listed["metrics_source_disabled"] is True
    # 明确切换到 AUTO 才修改旧来源；旧能力记录仍只作历史保存。
    changed = client.put("/api/v1/system/tenants/1", json={**tenant_body(), "metrics_source": "AUTO"})
    assert changed.status_code == 200 and changed.json()["data"]["metrics_source_disabled"] is False
    assert tenant.metrics_source == "AUTO" and tenant.capabilities["aom"] == old


def test_system_info_has_no_active_aom_source_or_recommended_mapping(management_api):
    response = management_api[0].get("/api/v1/system/info")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "AOM" not in data["metrics_sources"] and "aom" not in data["capabilities"]
    assert "全局停用" in data["notes"]["aom"]
    assert "专用 AOM 映射" not in data["notes"]["prometheus"]


@pytest.mark.parametrize("legacy", ["tenant_endpoint", "global_endpoint", "source", "capability"])
def test_tenant_check_skips_legacy_aom_without_network_or_capability_success(
    world, mem_db, mock_http, monkeypatch, legacy,
):
    tenant = world["tenant-a"]
    tenant.endpoints, tenant.capabilities, tenant.metrics_source, tenant.prometheus_url = {}, {}, "AUTO", None
    monkeypatch.setattr(get_settings(), "hcs_endpoints", {})
    if legacy == "tenant_endpoint":
        tenant.endpoints = {"aom": "invalid-address-must-never-be-resolved"}
    elif legacy == "global_endpoint":
        monkeypatch.setattr(get_settings(), "hcs_endpoints", {"aom": "invalid-global-address"})
    elif legacy == "source":
        tenant.metrics_source = "AOM"
    else:
        tenant.capabilities = {"aom": {"available": True, "reason": "历史探测", "at": "2026-10-01"}}
    before = dict(tenant.capabilities)
    calls = []
    original = mock_http.send

    def send(request, *args, **kwargs):
        calls.append(request.url.path)
        assert "/ams/" not in request.url.path and "/aom/" not in request.url.path
        return original(request, *args, **kwargs)

    monkeypatch.setattr(mock_http, "send", send)
    result = check_tenant(mem_db, tenant, http=mock_http, mock_url=MOCK_URL)
    assert result["ok"] is True and result["metrics_source"] == "NONE"
    skipped = next(row for row in result["checks"] if row["name"] == "应用运维管理 AOM")
    assert skipped["ok"] and skipped["state"] == "skipped" and skipped["available"] is False
    assert skipped["disabled"] is True and calls
    assert tenant.capabilities.get("aom") == before.get("aom")
