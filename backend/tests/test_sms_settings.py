"""短信配置：默认双关闭、租户隔离、凭据不返回，所有测试均不发送短信。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.sms import router
from app.core.config import get_settings
from app.core.crypto import PREFIX
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.main import app as production_app
from app.models import AuditLog, HcsAccount, Role, SysSetting, User
from app.services import sms_settings

SECRET = "synthetic-sms-password-never-return"


@pytest.fixture()
def sms_api(mem_db):
    role = Role(code="admin", name="管理员", permissions=["*"])
    mem_db.add(role)
    mem_db.flush()
    user = User(username="sms-admin", display_name="测试管理员", password_hash="!", role_id=role.id)
    mem_db.add(user)
    tenants = [HcsAccount(id=i, code=f"sms-{i}", name=f"短信租户{i}", iam_endpoint="https://example.invalid",
                           domain_name="synthetic", secret_cipher=f"unchanged-hcs-{i}") for i in (1, 2)]
    mem_db.add_all(tenants)
    mem_db.commit()
    actor = CurrentUser(id=user.id, username=user.username, display_name=user.display_name,
                        role_code="admin", permissions=["setting:manage", "tenant:manage", "notify:view"])
    app = FastAPI()
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: actor
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client, mem_db, actor


def config(**extra):
    return {"enabled": True, "endpoint": "https://sms.example.invalid/HttpSmsBatchSend", "username": "tenant-only-user",
            "password": SECRET, "expand_port": "001", "verify_tls": True, "severities": ["P1", "P2"], **extra}


def test_defaults_are_off_and_reads_never_create_settings(sms_api):
    client, db, _ = sms_api
    assert client.get("/api/v1/system/sms").json()["data"]["enabled"] is False
    for tid in (1, 2):
        result = client.get(f"/api/v1/system/tenants/{tid}/sms")
        assert result.status_code == 200
        data = result.json()["data"]
        assert data["enabled"] is False and data["verify_tls"] is True and not data["password_set"]
        assert data["revision"] == 0 and data["endpoint"] == data["username"] == ""
        assert not sms_settings.effective(db, tid)
    assert db.scalars(select(SysSetting)).all() == []


def test_tenant_credentials_are_encrypted_independent_and_not_audited(sms_api):
    client, db, _ = sms_api
    response = client.put("/api/v1/system/tenants/1/sms", json=config())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["enabled"] and data["password_set"] and data["revision"] == 1
    assert SECRET not in response.text and "password_cipher" not in response.text
    cipher = db.get(SysSetting, "sms_tenant:1").value["password_cipher"]
    assert cipher.startswith(PREFIX) and cipher != SECRET
    assert sms_settings.password(sms_settings.load_tenant(db, 1)) == SECRET
    assert client.get("/api/v1/system/tenants/2/sms").json()["data"]["password_set"] is False
    assert db.get(HcsAccount, 1).secret_cipher == "unchanged-hcs-1"
    assert db.get(HcsAccount, 2).secret_cipher == "unchanged-hcs-2"
    logs = [row.detail for row in db.scalars(select(AuditLog))]
    assert SECRET not in json.dumps(logs) and cipher not in json.dumps(logs)
    assert db.scalar(select(AuditLog.action)) == "sms.tenant.update"


def test_both_switches_and_account_must_be_enabled(sms_api):
    client, db, _ = sms_api
    assert client.put("/api/v1/system/tenants/1/sms", json=config()).status_code == 200
    assert not sms_settings.effective(db, 1)
    assert client.put("/api/v1/system/sms", json={"enabled": True}).status_code == 200
    assert sms_settings.effective(db, 1) and not sms_settings.effective(db, 2)
    cutoff = sms_settings.activation_time(db, 1)
    assert cutoff is not None and cutoff.tzinfo is not None
    cipher = sms_settings.load_tenant(db, 1).password_cipher
    assert client.put("/api/v1/system/sms", json={"enabled": False}).status_code == 200
    assert not sms_settings.effective(db, 1)
    assert sms_settings.load_tenant(db, 1).password_cipher == cipher
    assert client.put("/api/v1/system/sms", json={"enabled": True}).status_code == 200
    db.get(HcsAccount, 1).enabled = False
    db.commit()
    assert not sms_settings.effective(db, 1)


def test_blank_password_retains_secret_clear_requires_disabled(sms_api):
    client, db, _ = sms_api
    client.put("/api/v1/system/tenants/1/sms", json=config())
    before = sms_settings.load_tenant(db, 1)
    assert client.put("/api/v1/system/tenants/1/sms", json={"password": ""}).status_code == 200
    after = sms_settings.load_tenant(db, 1)
    assert after.password_cipher == before.password_cipher and after.revision == before.revision
    denied = client.put("/api/v1/system/tenants/1/sms", json={"clear_password": True})
    assert denied.status_code == 400 and sms_settings.load_tenant(db, 1).password_cipher == before.password_cipher
    done = client.put("/api/v1/system/tenants/1/sms", json={"enabled": False, "clear_password": True})
    assert done.status_code == 200 and not done.json()["data"]["password_set"]
    assert sms_settings.load_tenant(db, 1).password_cipher == ""


@pytest.mark.parametrize("body", [{"enabled": True}, config(endpoint=""), config(username=""),
                                  config(password=""), config(severities=[]), config(severities=["P4"]),
                                  config(expand_port="x"), config(expand_port="123456789012345"),
                                  config(password="********"), config(clear_password=True),
                                  config(endpoint="http://user:secret@example.invalid/api"),
                                  config(endpoint="https://example.invalid/api?secret=value"),
                                  config(endpoint="/HttpSmsBatchSend"), config(endpoint="https://{gateway}/api"),
                                  config(enabled="true")])
def test_invalid_configs_never_persist_or_return_password(sms_api, body):
    client, db, _ = sms_api
    response = client.put("/api/v1/system/tenants/1/sms", json=body)
    assert response.status_code == 400, response.text
    assert SECRET not in response.text and db.get(SysSetting, "sms_tenant:1") is None


def test_validation_error_does_not_echo_secret_input(sms_api):
    client, _, _ = sms_api
    response = client.put("/api/v1/system/tenants/1/sms", json={"password": [SECRET]})
    assert response.status_code == 400 and SECRET not in response.text


def test_sms_tls_is_independent_of_hcs_verify(sms_api, monkeypatch):
    client, _, _ = sms_api
    monkeypatch.setattr(get_settings(), "hcs_verify", False)
    assert client.get("/api/v1/system/tenants/1/sms").json()["data"]["verify_tls"] is True


def test_scoped_manager_cannot_change_global_or_another_tenant(sms_api):
    client, _, actor = sms_api
    actor.tenant_ids = [1]
    assert client.get("/api/v1/system/sms").status_code == 200
    assert client.put("/api/v1/system/sms", json={"enabled": True}).status_code == 403
    assert client.put("/api/v1/system/tenants/1/sms", json=config(enabled=False)).status_code == 200
    assert client.get("/api/v1/system/tenants/2/sms").status_code == 404
    assert client.put("/api/v1/system/tenants/2/sms", json=config()).status_code == 404


def test_viewer_cannot_change_sms_or_read_credentials(sms_api):
    client, _, actor = sms_api
    actor.permissions = ["notify:view"]
    assert client.get("/api/v1/system/sms").status_code == 200
    assert client.get("/api/v1/system/tenants/1/sms").status_code == 403
    assert client.put("/api/v1/system/sms", json={"enabled": True}).status_code == 403
    assert client.put("/api/v1/system/tenants/1/sms", json=config()).status_code == 403


@pytest.mark.parametrize("value", ["13812345678", "+86 13812345678", "8613812345678", "138-1234-5678"])
def test_mobile_normalization(value):
    assert sms_settings.normalize_phone(value) == "13812345678"


@pytest.mark.parametrize("value", [None, "", "01012345678", "13812345678,13912345678", "1381234567", "phone"])
def test_non_mobile_and_multiple_numbers_do_not_receive_sms(value):
    assert sms_settings.normalize_phone(value) is None


def test_corrupt_saved_configuration_fails_closed(sms_api):
    _, db, _ = sms_api
    db.add(SysSetting(key="sms", value={"enabled": "true"}))
    db.add(SysSetting(key="sms_tenant:1", value={"enabled": True, "enabled_since": "not-a-date"}))
    db.commit()
    assert not sms_settings.load_global(db).enabled and not sms_settings.load_tenant(db, 1).enabled
