"""系统设置 → 租户短信：各租户短信概况只返回开关与是否配置齐全，不返回网关、账号或密码；按租户范围过滤。"""
from app.services import sms_settings
from tests.test_sms_settings import SECRET, config
from tests.test_sms_settings import sms_api as sms_api


def test_summaries_show_switches_and_completeness_without_credentials(sms_api):
    client, db, _ = sms_api
    assert client.put("/api/v1/system/tenants/1/sms", json=config()).status_code == 200
    data = client.get("/api/v1/system/sms/tenants").json()["data"]
    assert data["global_enabled"] is False
    first, second = data["items"]
    assert (first["tenant_id"], first["enabled"], first["configured"], first["effective"]) == (1, True, True, False)
    assert first["severities"] == ["P1", "P2"] and first["tenant_name"] == "短信租户1"
    assert (second["tenant_id"], second["enabled"], second["configured"]) == (2, False, False)
    text = client.get("/api/v1/system/sms/tenants").text
    for secret in (SECRET, "tenant-only-user", "sms.example.invalid", "password_cipher"):
        assert secret not in text
    assert client.put("/api/v1/system/sms", json={"enabled": True}).status_code == 200
    data = client.get("/api/v1/system/sms/tenants").json()["data"]
    assert data["global_enabled"] is True and data["items"][0]["effective"] is sms_settings.effective(db, 1) is True


def test_scoped_manager_sees_only_own_tenants_and_viewer_is_denied(sms_api):
    client, _, actor = sms_api
    actor.tenant_ids = [2]
    assert [item["tenant_id"] for item in client.get("/api/v1/system/sms/tenants").json()["data"]["items"]] == [2]
    actor.permissions = ["notify:view", "setting:manage"]
    assert client.get("/api/v1/system/sms/tenants").status_code == 403
