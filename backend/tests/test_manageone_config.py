"""ManageOne 独立配置：默认关闭、权限隔离、凭据保护和显式选源；不访问云端。"""
import json
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.manageone import router
from app.core.config import get_settings
from app.core.crypto import PREFIX, decrypt
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.main import app as production_app
from app.models import AlarmEvent, AlarmEventLog, AuditLog, CollectSwitch, HcsAccount, InspectionTask, SysSetting
from app.services import collect_switch, manageone_config

PATH = "/api/v1/system/tenants/1/manageone"
SECRET = "synthetic-manageone-password-never-return"
SECOND_SECRET = "synthetic-manageone-replacement-password"
PUBLIC_FIELDS = {"tenant_id", "enabled", "configured", "endpoint", "username", "organization", "secret_set", "selected"}


@pytest.fixture
def manageone_api(mem_db):
    mem_db.add_all(HcsAccount(id=i, code=f"manageone-{i}", name=f"ManageOne 租户{i}",
                              iam_endpoint="https://iam.example.invalid", domain_name="iam-only-domain",
                              username=f"iam-only-user-{i}", secret_cipher=f"unchanged-iam-secret-{i}") for i in (1, 2))
    mem_db.commit()
    actor = CurrentUser(id=1, username="manageone-test", display_name="配置测试员",
                        permissions=["tenant:manage", "tenant:view", "ledger:view", "task:view"])
    app = FastAPI()
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: actor
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client, mem_db, actor


def config(**updates):
    return {"enabled": True, "endpoint": "https://manageone.example.invalid/base/path/",
            "username": "manageone-only-user", "organization": "org-a", "password": SECRET, **updates}


def test_defaults_are_disabled_independent_and_reads_do_not_write(manageone_api):
    client, db, _ = manageone_api
    for tenant_id in (1, 2):
        response = client.get(f"/api/v1/system/tenants/{tenant_id}/manageone")
        assert response.status_code == 200
        assert response.json()["data"] == {
            "tenant_id": tenant_id, "enabled": False, "configured": False, "endpoint": "", "username": "",
            "organization": None, "secret_set": False, "selected": False,
        }
        tenant = db.get(HcsAccount, tenant_id)
        assert tenant.manageone_config == {} and tenant.metrics_source == "AUTO"
        assert manageone_config.get_config(tenant) == {
            "enabled": False, "endpoint": "", "username": "", "organization": None, "secret_cipher": "",
        }
    assert db.scalars(select(AuditLog)).all() == []
    assert db.scalars(select(SysSetting)).all() == []
    assert db.scalars(select(CollectSwitch)).all() == []
    assert db.scalars(select(InspectionTask)).all() == []


def test_encrypted_credentials_are_independent_and_never_returned_or_audited(manageone_api, caplog):
    client, db, _ = manageone_api
    response = client.put(PATH, json=config())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == PUBLIC_FIELDS
    assert data["enabled"] and data["configured"] and data["secret_set"] and not data["selected"]
    tenant = db.get(HcsAccount, 1)
    internal = manageone_config.get_config(tenant)
    cipher = internal["secret_cipher"]
    assert cipher.startswith(PREFIX) and decrypt(cipher) == SECRET
    assert tenant.manageone_config == internal
    assert set(internal) == {"enabled", "endpoint", "username", "organization", "secret_cipher"}
    assert tenant.iam_endpoint == "https://iam.example.invalid" and tenant.username == "iam-only-user-1"
    assert tenant.secret_cipher == "unchanged-iam-secret-1"
    assert db.get(HcsAccount, 2).manageone_config == {}
    assert not client.get("/api/v1/system/tenants/2/manageone").json()["data"]["secret_set"]
    readback = client.get(PATH)
    assert readback.json()["data"] == data
    audit = db.scalar(select(AuditLog))
    assert (audit.action, audit.tenant_id, audit.target_id) == ("tenant.manageone.update", 1, "1")
    assert audit.detail["password_changed"] is True
    exposed = response.text + readback.text + json.dumps(audit.detail) + caplog.text
    assert all(value not in exposed for value in (SECRET, cipher, "secret_cipher"))
    second = client.put("/api/v1/system/tenants/2/manageone", json=config(password=SECOND_SECRET))
    assert second.status_code == 200
    assert decrypt(db.get(HcsAccount, 2).manageone_config["secret_cipher"]) == SECOND_SECRET
    assert tenant.manageone_config["secret_cipher"] == cipher


@pytest.mark.parametrize("payload", [{}, {"password": ""}, {"password": None}])
def test_blank_or_omitted_password_preserves_cipher(manageone_api, payload):
    client, db, _ = manageone_api
    assert client.put(PATH, json=config()).status_code == 200
    cipher = db.get(HcsAccount, 1).manageone_config["secret_cipher"]
    result = client.put(PATH, json=payload)
    assert result.status_code == 200, result.text
    assert db.get(HcsAccount, 1).manageone_config["secret_cipher"] == cipher
    assert result.json()["data"]["configured"]
    last = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()))
    assert last.detail == {"changes": {}, "password_changed": False, "closed_disk_alarms": 0}


@pytest.mark.parametrize("source", ["AUTO", "NONE", "PROMETHEUS", "AOM", "MANAGEONE"])
def test_save_never_changes_source_without_explicit_selection_or_other_gates(manageone_api, source):
    client, db, _ = manageone_api
    tenant = db.get(HcsAccount, 1)
    tenant.metrics_source, tenant.enabled = source, False
    collect_switch.initialize_disabled(db, 1, "test-operator")
    db.add(InspectionTask(code="ecs_metrics", name="ECS 指标", service="ecs", interval_sec=300,
                          enabled=False, params={"kept": "unchanged"}))
    db.commit()
    switches = [(row.tenant_id, row.resource_type, row.enabled, row.updated_by) for row in db.scalars(select(CollectSwitch))]
    settings = [(row.key, deepcopy(row.value), row.updated_by) for row in db.scalars(select(SysSetting))]
    for payload in (config(), {"select_source": False}, {"enabled": False}):
        response = client.put(PATH, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["selected"] == (source == "MANAGEONE")
        assert tenant.metrics_source == source and tenant.enabled is False
    assert [(row.tenant_id, row.resource_type, row.enabled, row.updated_by)
            for row in db.scalars(select(CollectSwitch))] == switches
    assert [(row.key, row.value, row.updated_by) for row in db.scalars(select(SysSetting))] == settings
    task = db.get(InspectionTask, "ecs_metrics")
    assert task.enabled is False and task.interval_sec == 300 and task.params == {"kept": "unchanged"}


def test_explicit_selection_is_atomic_and_disabling_keeps_selected_marker(manageone_api):
    client, db, _ = manageone_api
    tenant = db.get(HcsAccount, 1)
    response = client.put(PATH, json=config(select_source=True))
    assert response.status_code == 200 and response.json()["data"]["selected"]
    assert tenant.metrics_source == "MANAGEONE" and tenant.manageone_config["enabled"] is True
    db.expire_all()
    assert db.get(HcsAccount, 1).metrics_source == "MANAGEONE"
    assert db.scalar(select(AuditLog)).detail["changes"]["metrics_source"] == ["AUTO", "MANAGEONE"]
    disabled = client.put(PATH, json={"enabled": False, "select_source": False})
    assert disabled.status_code == 200
    assert disabled.json()["data"]["selected"] and not disabled.json()["data"]["enabled"]
    assert db.get(HcsAccount, 1).metrics_source == "MANAGEONE"
    assert db.scalars(select(CollectSwitch)).all() == []
    assert db.scalars(select(InspectionTask)).all() == []
    assert db.scalars(select(SysSetting)).all() == []


def test_selecting_cloud_volume_source_administratively_closes_filesystem_disk_alerts(manageone_api):
    client, db, _actor = manageone_api
    db.add(AlarmEvent(tenant_id=1, fingerprint="old-filesystem-disk-alarm", rule_id="R-ECS-006",
        service="ecs", resource_type="ecs_server", resource_name="synthetic-ecs", severity="P2",
        status="FIRING", title="旧文件系统分区告警", check_item="disk"))
    db.commit()
    response = client.put(PATH, json=config(select_source=True))
    assert response.status_code == 200, response.text
    alarm = db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-ECS-006"))
    assert alarm.status == "CLOSED" and alarm.resolved_at is not None
    log = db.scalar(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id))
    assert (log.action, log.from_status, log.to_status) == ("CLOSE", "FIRING", "CLOSED")
    assert log.operator == "配置测试员" and "非指标恢复" in log.note
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "tenant.manageone.update"))
    assert audit.detail["closed_disk_alarms"] == 1


@pytest.mark.parametrize("payload", [
    {"select_source": True}, config(enabled=False, select_source=True),
    config(endpoint="", select_source=True), config(username="", select_source=True),
    config(password="", select_source=True), {"enabled": True},
    config(endpoint=""), config(username=""), config(password=None),
])
def test_incomplete_enable_or_selection_never_changes_config_source_or_audit(manageone_api, payload):
    client, db, _ = manageone_api
    response = client.put(PATH, json=payload)
    assert response.status_code == 400 and response.json()["code"] == 40001
    assert "ManageOne" in response.json()["message"]
    assert SECRET not in response.text
    tenant = db.get(HcsAccount, 1)
    assert tenant.manageone_config == {} and tenant.metrics_source == "AUTO"
    assert db.scalars(select(AuditLog)).all() == []


def test_disabled_complete_configuration_can_be_saved_without_selection(manageone_api):
    client, db, _ = manageone_api
    response = client.put(PATH, json=config(enabled=False))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] and data["secret_set"] and not data["enabled"] and not data["selected"]
    before = deepcopy(db.get(HcsAccount, 1).manageone_config)
    rejected = client.put(PATH, json={"select_source": True})
    assert rejected.status_code == 400 and "先启用并完整配置" in rejected.json()["message"]
    assert db.get(HcsAccount, 1).manageone_config == before
    response = client.put(PATH, json={"enabled": True, "select_source": True})
    assert response.status_code == 200 and response.json()["data"]["selected"]


@pytest.mark.parametrize("field,value,label", [
    ("endpoint", "https://other.example.invalid/another/base", "地址"),
    ("username", "another-user", "用户名"), ("organization", "org-b", "组织"), ("organization", None, "组织"),
])
@pytest.mark.parametrize("password", [None, ""])
def test_changing_identity_requires_replacement_password_and_rejects_old_secret(manageone_api, field, value, label, password):
    client, db, _ = manageone_api
    assert client.put(PATH, json=config()).status_code == 200
    before = deepcopy(db.get(HcsAccount, 1).manageone_config)
    response = client.put(PATH, json={field: value, "password": password, "select_source": True})
    assert response.status_code == 400
    assert f"修改 ManageOne {label}后须重新填写密码" in response.json()["message"]
    assert db.get(HcsAccount, 1).manageone_config == before
    assert db.get(HcsAccount, 1).metrics_source == "AUTO"
    assert len(db.scalars(select(AuditLog)).all()) == 1
    changed = client.put(PATH, json={field: value, "password": SECOND_SECRET})
    assert changed.status_code == 200, changed.text
    assert db.get(HcsAccount, 1).manageone_config[field] == value
    assert decrypt(db.get(HcsAccount, 1).manageone_config["secret_cipher"]) == SECOND_SECRET


def test_identity_change_without_old_secret_is_allowed_while_disabled(manageone_api):
    client, db, _ = manageone_api
    result = client.put(PATH, json={"endpoint": "http://192.0.2.10:8080/base", "username": "read-only"})
    assert result.status_code == 200
    assert not result.json()["data"]["secret_set"] and not result.json()["data"]["enabled"]
    assert db.get(HcsAccount, 1).metrics_source == "AUTO"


def test_clear_requires_disabled_and_explicitly_discards_secret_on_identity_change(manageone_api):
    client, db, _ = manageone_api
    assert client.put(PATH, json=config(select_source=True)).status_code == 200
    before = deepcopy(db.get(HcsAccount, 1).manageone_config)
    for body, message in (({"clear_password": True}, "须先禁用"),
                          ({"password": SECOND_SECRET, "clear_password": True}, "不能同时")):
        response = client.put(PATH, json=body)
        assert response.status_code == 400 and message in response.json()["message"]
        assert db.get(HcsAccount, 1).manageone_config == before
    result = client.put(PATH, json={"enabled": False, "clear_password": True,
                                  "endpoint": "https://changed.example.invalid", "username": "changed", "organization": None})
    assert result.status_code == 200
    data = result.json()["data"]
    assert not data["enabled"] and not data["configured"] and not data["secret_set"] and data["selected"]
    assert db.get(HcsAccount, 1).manageone_config["secret_cipher"] == ""
    assert db.get(HcsAccount, 1).metrics_source == "MANAGEONE"
    assert db.scalar(select(AuditLog).order_by(AuditLog.id.desc())).detail["password_changed"] is True
    assert client.put(PATH, json={"enabled": True}).status_code == 400


@pytest.mark.parametrize("permission", ["ledger:view", "task:view", "tenant:view"])
def test_each_view_permission_allows_read_but_not_write_or_clear(manageone_api, permission):
    client, db, actor = manageone_api
    assert client.put(PATH, json=config()).status_code == 200
    before = deepcopy(db.get(HcsAccount, 1).manageone_config)
    actor.permissions = [permission]
    assert client.get(PATH).status_code == 200
    for body in (config(), {"enabled": False, "clear_password": True}, {"select_source": True},
                 {"endpoint": "https://other.example.invalid", "password": SECOND_SECRET}):
        assert client.put(PATH, json=body).status_code == 403
    assert db.get(HcsAccount, 1).manageone_config == before


def test_missing_permissions_reject_reads_and_writes(manageone_api):
    client, db, actor = manageone_api
    actor.permissions = []
    assert client.get(PATH).status_code == 403
    assert client.put(PATH, json=config()).status_code == 403
    actor.permissions = ["tenant:manage"]
    assert client.get(PATH).status_code == 403
    assert client.put(PATH, json=config()).status_code == 200
    assert db.get(HcsAccount, 1).manageone_config["enabled"] is True


@pytest.mark.parametrize("scope_kind", ["membership", "selected_tenant", "empty"])
def test_scope_rejects_cross_tenant_read_write_clear_and_selection(manageone_api, scope_kind):
    client, db, actor = manageone_api
    actor.tenant_ids = [1] if scope_kind == "membership" else ([] if scope_kind == "empty" else None)
    headers = {"X-Tenant-Id": "1"} if scope_kind == "selected_tenant" else {}
    path = "/api/v1/system/tenants/2/manageone"
    assert client.get(path, headers=headers).status_code == 404
    for body in (config(), {"enabled": False, "clear_password": True}, config(select_source=True)):
        assert client.put(path, headers=headers, json=body).status_code == 404
    assert db.get(HcsAccount, 2).manageone_config == {} and db.get(HcsAccount, 2).metrics_source == "AUTO"
    assert db.scalars(select(AuditLog)).all() == []


def test_header_cannot_expand_membership_and_missing_tenant_is_hidden(manageone_api):
    client, _, actor = manageone_api
    actor.tenant_ids = [1]
    assert client.get(PATH, headers={"X-Tenant-Id": "2"}).status_code == 403
    assert client.put(PATH, headers={"X-Tenant-Id": "2"}, json=config()).status_code == 403
    assert client.get("/api/v1/system/tenants/999/manageone").status_code == 404
    assert client.put("/api/v1/system/tenants/999/manageone", json=config()).status_code == 404


@pytest.mark.parametrize("endpoint", [
    "manageone.example.invalid", "//manageone.example.invalid", "/base", "ftp://manageone.example.invalid",
    "https://", "https:///base", "https://user:secret@example.invalid", "https://@example.invalid",
    "https://example.invalid?", "https://example.invalid?token=secret", "https://example.invalid#",
    "https://example.invalid#fragment", "https://{region}.example.invalid", "https://example.invalid/${tenant}",
    "https://example.invalid/with space", "https://example.invalid/\nbase", "https://example.invalid/\tbase",
    "https://example.invalid/\x00base", "https://example.invalid/\x7fbase", "https://example.invalid/%0abase",
    "https://example.invalid/%7Btenant%7D", "https://example.invalid\\@other.invalid",
    "https://user%40example.invalid", "https://example.invalid:", "https://example.invalid:0",
    "https://example.invalid:99999", "https://example.invalid:invalid", "https://[invalid]/base",
])
def test_unsafe_endpoint_is_rejected_without_leaking_input(manageone_api, endpoint):
    client, db, _ = manageone_api
    response = client.put(PATH, json=config(endpoint=endpoint))
    assert response.status_code == 400, response.text
    assert "HTTP/HTTPS 基础地址" in response.json()["message"]
    assert SECRET not in response.text and endpoint not in response.text
    assert db.get(HcsAccount, 1).manageone_config == {}
    assert db.scalars(select(AuditLog)).all() == []


@pytest.mark.parametrize("endpoint", [
    "http://192.0.2.10:8080", "http://localhost:8080/base/path/", "https://manageone.example.invalid:8443/base/path/",
    "http://[::1]:8080/base", "https://manageone.example.invalid",
])
def test_explicit_http_https_and_base_paths_are_preserved(manageone_api, endpoint, monkeypatch):
    client, db, _ = manageone_api
    settings = get_settings()
    monkeypatch.setattr(settings, "hcs_verify", False)
    result = client.put(PATH, json=config(endpoint=endpoint))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["endpoint"] == endpoint
    assert db.get(HcsAccount, 1).manageone_config["endpoint"] == endpoint
    assert "verify_tls" not in db.get(HcsAccount, 1).manageone_config and settings.hcs_verify is False


@pytest.mark.parametrize("updates", [
    {"enabled": "true"}, {"enabled": 1}, {"select_source": "false"}, {"clear_password": "true"},
    {"endpoint": "https://example.invalid/" + "x" * 512}, {"username": "u" * 129},
    {"organization": "o" * 129}, {"password": SECRET * 20}, {"password": [SECRET]},
    {"username": {"credential": SECRET}}, {"organization": [SECRET]}, {"endpoint": {"credential": SECRET}},
    {"username": "bad\nuser"}, {"organization": "bad\x00org"}, {"password": "********"},
    {"secret_cipher": SECRET}, {SECRET: SECRET}, {"verify_tls": False},
])
def test_invalid_fields_and_validation_errors_never_echo_secrets(manageone_api, updates, caplog):
    client, db, _ = manageone_api
    response = client.put(PATH, json=config(**updates))
    assert response.status_code == 400 and response.json()["code"] == 40001
    assert SECRET not in response.text + caplog.text and "secret_cipher" not in response.text
    assert db.get(HcsAccount, 1).manageone_config == {}
    assert db.scalars(select(AuditLog)).all() == []


def test_malformed_json_never_echoes_password(manageone_api):
    client, db, _ = manageone_api
    result = client.put(PATH, content='{"password": "' + SECRET + '", "enabled": ',
                        headers={"content-type": "application/json"})
    assert result.status_code == 400 and SECRET not in result.text
    assert db.get(HcsAccount, 1).manageone_config == {}


@pytest.mark.parametrize("raw", [None, [], {"enabled": "true"}, {"enabled": True, "endpoint": "//unsafe"}])
def test_corrupt_stored_config_fails_closed_without_mutating_it(manageone_api, raw):
    client, db, _ = manageone_api
    tenant = db.get(HcsAccount, 1)
    tenant.manageone_config = raw
    db.commit()
    result = client.get(PATH)
    assert result.status_code == 200
    assert not result.json()["data"]["enabled"] and not result.json()["data"]["secret_set"]
    assert not result.json()["data"]["configured"]
    assert tenant.manageone_config == raw


def test_unencrypted_secret_and_iam_credentials_are_never_reused(manageone_api):
    client, db, _ = manageone_api
    tenant = db.get(HcsAccount, 1)
    tenant.manageone_config = {"enabled": False, "endpoint": "https://example.invalid", "username": "read-only",
                              "secret_cipher": SECRET}
    db.commit()
    assert manageone_config.get_config(tenant)["secret_cipher"] == ""
    result = client.get(PATH)
    assert result.status_code == 200 and not result.json()["data"]["secret_set"] and SECRET not in result.text
    assert client.put(PATH, json={"enabled": True}).status_code == 400
    assert tenant.secret_cipher == "unchanged-iam-secret-1"


def test_registered_production_routes_have_safe_response_contract():
    paths = production_app.openapi()["paths"]
    path = "/api/v1/system/tenants/{tenant_id}/manageone"
    assert set(paths[path]) == {"get", "put"}
    schema = production_app.openapi()["components"]["schemas"]["ManageOneConfigOut"]
    assert set(schema["properties"]) == PUBLIC_FIELDS
