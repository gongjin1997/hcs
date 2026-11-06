"""真实签名 -> 验签 -> 邮件 ACK；代次、旧链接兼容与幂等不能只靠 mock verify。"""

import base64
import re
import time
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.v1.alarms import mail_router
from app.core import security
from app.core.config import Settings
from app.core.db import get_db
from app.main import app as production_app
from app.models import AlarmEvent, AlarmEventLog, AuditLog, Contact, Notification, User
from app.notify import dispatcher
from app.notify.render import mail_context
from app.services import mail_settings_service


@pytest.fixture()
def mail_api(mem_db, monkeypatch):
    operator = User(username="mail-operator", display_name="邮件操作人", email="operator@example.invalid",
                    password_hash="!", login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=[1])
    mem_db.add(operator)
    mem_db.flush()
    mem_db.add(Contact(user_id=operator.id, name=operator.display_name, email=operator.email, enabled=True))
    mem_db.commit()
    app = FastAPI()
    app.include_router(mail_router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    settings = Settings(_env_file=None, smtp_host="example.invalid", smtp_sender="sender@example.invalid", smtp_user="")
    monkeypatch.setattr(mail_settings_service, "get_settings", lambda: settings)
    monkeypatch.setattr(mail_settings_service, "SessionLocal", lambda: nullcontext(mem_db))
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    with TestClient(app) as client:
        yield client


def alarm(db, generation=0):
    row = AlarmEvent(
        tenant_id=1,
        service="dcs",
        resource_type="dcs_instance",
        resource_name="测试缓存",
        fingerprint="mail-link-integration",
        rule_id="synthetic-rule",
        severity="P2",
        status="FIRING",
        title="内存异常",
        check_item="memory",
        reopen_count=generation,
    )
    db.add(row)
    db.commit()
    return row


def context_token(db, row):
    url = mail_context(db, row, "FIRING", "operator@example.invalid")["links"]["ack"]
    return url.rsplit("/", 1)[1]


def legacy_token(row):
    payload = f"{row.id}|ack|operator@example.invalid|{int(time.time()) + 600}"
    return base64.urlsafe_b64encode(f"{payload}|{security._mail_sig(payload)}".encode()).decode()


@pytest.mark.parametrize("generation", [0, 1, 4])
def test_context_signs_actual_generation_and_real_ack_is_idempotent(mem_db, mail_api, generation):
    row = alarm(mem_db, generation)
    token = context_token(mem_db, row)
    info = security.verify_mail_action(token)
    assert info == {"alarm_id": row.id, "action": "ack", "recipient": "operator@example.invalid", "reopen_count": generation}
    path = f"/api/v1/mail-actions/{token}"
    assert mail_api.get(path).status_code == 200
    for _ in range(2):
        response = mail_api.post(path)
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "ACKED"
        assert response.json()["data"]["ack_by"] == "operator@example.invalid"
    assert mem_db.scalar(select(func.count()).select_from(AlarmEventLog).where(AlarmEventLog.action == "ACK")) == 1
    assert mem_db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "alarm.mail_ack")) == 1


def test_legacy_link_works_only_in_generation_zero_and_new_link_can_ack_reopen(mem_db, mail_api):
    row = alarm(mem_db)
    old = legacy_token(row)
    assert security.verify_mail_action(old)["reopen_count"] == 0
    assert mail_api.post(f"/api/v1/mail-actions/{old}").status_code == 200
    row.status, row.reopen_count, row.ack_by, row.ack_at = "FIRING", 1, None, None
    mem_db.commit()
    for method in (mail_api.get, mail_api.post):
        assert method(f"/api/v1/mail-actions/{old}").status_code == 409
    assert row.status == "FIRING" and row.ack_by is None
    current = context_token(mem_db, row)
    assert security.verify_mail_action(current)["reopen_count"] == 1
    assert mail_api.post(f"/api/v1/mail-actions/{current}").status_code == 200


def test_generation_cannot_be_modified_or_stripped_without_invalidating_signature(mem_db, mail_api):
    row = alarm(mem_db, 2)
    token = context_token(mem_db, row)
    parts = base64.urlsafe_b64decode(token).decode().split("|")
    changed = parts.copy()
    changed[-2] = "3"
    stripped = parts[:-2] + parts[-1:]
    for payload in (changed, stripped):
        altered = base64.urlsafe_b64encode("|".join(payload).encode()).decode()
        assert security.verify_mail_action(altered) is None
        assert mail_api.post(f"/api/v1/mail-actions/{altered}").status_code == 401
    assert row.status == "FIRING"


def test_real_dispatched_reopened_mail_contains_current_link_that_can_ack(mem_db, mail_api):
    row = alarm(mem_db, 3)
    mem_db.add(Notification(alarm_id=row.id, kind="FIRING", recipients=["operator@example.invalid"]))
    mem_db.commit()
    messages = []
    assert dispatcher.dispatch_pending(mem_db, sender=messages.append) == 1
    html = messages[0].get_body(preferencelist=("html",)).get_content()
    token = re.search(r"/m/action/([^\"<>\s]+)", html).group(1)
    assert security.verify_mail_action(token)["reopen_count"] == 3
    response = mail_api.post(f"/api/v1/mail-actions/{token}")
    assert response.status_code == 200 and response.json()["data"]["status"] == "ACKED"


@pytest.mark.parametrize("payload", ["invalid", "1|ack|recipient|future|bad", "1|ack|recipient|9999999999|中文"])
def test_malformed_mail_signature_is_invalid_not_server_error(payload):
    token = base64.urlsafe_b64encode(payload.encode()).decode()
    assert security.verify_mail_action(token) is None
