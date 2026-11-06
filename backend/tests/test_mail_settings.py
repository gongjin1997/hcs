"""邮件设置安全与运行时生效回归；仅 mock SMTP / IMAP，不连接外部服务器。"""
import json
import smtplib
import ssl
from contextlib import nullcontext
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1 import notify
from app.core import crypto
from app.core.config import Settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.main import create_app
from app.models import AuditLog, Contact, Role, SysSetting, User
from app.notify import smtp
from app.schemas.mail import MailSettingsUpdate
from app.services import mail_settings_service as service

SECRET = "test-only-mail-password-012345"
IMAP_SECRET = "test-only-imap-password-67890"
API = "/api/v1/notify"


@pytest.fixture(autouse=True)
def mail_environment(monkeypatch):
    settings = Settings(_env_file=None, smtp_host="legacy.example.com", smtp_port=25,
                        smtp_sender="legacy@example.com", smtp_password=SECRET, smtp_user="legacy-user")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    return settings


def update(db, **data):
    changes = service.save(db, MailSettingsUpdate.model_validate(data), "测试操作人")
    db.commit()
    return changes


def smtp_config(**overrides):
    return service.SmtpRuntime(host="smtp.example.com", port=25, sender="sender@example.com",
                               username="smtp-user", password=SecretStr(SECRET), **overrides)


def imap_config(**overrides):
    return service.ImapRuntime(enabled=True, host="imap.example.com", port=993, username="imap-user",
                               password=SecretStr(IMAP_SECRET), **overrides)


def test_environment_fallback_then_complete_database_snapshot(mem_db, mail_environment):
    mail_environment.smtp_ca_file = "/test-only/legacy-ca.pem"
    value = service.get(mem_db)
    assert value.smtp.port == 25 and value.smtp.auth
    assert value.smtp.password.get_secret_value() == SECRET
    assert service.public(mem_db, editable=False)["source"] == "environment"
    update(mem_db, smtp={"sender": "new@example.com"})
    row = mem_db.get(SysSetting, service.KEY)
    assert row is not None
    assert set(row.value) == {"smtp", "imap"}
    stored = row.value["smtp"]
    assert stored["host"] == "legacy.example.com" and stored["port"] == 25
    assert stored["password_encrypted"].startswith(crypto.PREFIX)
    assert crypto.decrypt(stored["password_encrypted"]) == SECRET
    assert SECRET not in json.dumps(row.value)
    mail_environment.smtp_host = "changed-env.example.com"
    mail_environment.smtp_password = "changed-env-password"
    mail_environment.smtp_ca_file = "/test-only/changed-ca.pem"
    assert service.get(mem_db).smtp.host == "legacy.example.com"
    assert service.get(mem_db).smtp.password.get_secret_value() == SECRET
    assert service.get(mem_db).smtp.ca_file == "/test-only/legacy-ca.pem"
    assert SECRET not in repr(service.get(mem_db))
    assert SECRET not in json.dumps(service.public(mem_db, editable=True), default=str)
    assert "ca_file" not in json.dumps(service.public(mem_db, editable=True), default=str)


@pytest.mark.parametrize("protocol", ["smtp", "imap"])
def test_empty_password_preserves_and_explicit_clear_never_falls_back(mem_db, protocol):
    update(mem_db, **{protocol: {"password": SECRET}})
    for data in ({}, {"password": ""}, {"password": None}):
        update(mem_db, **{protocol: data})
        assert getattr(service.get(mem_db), protocol).password.get_secret_value() == SECRET
    diff = update(mem_db, **{protocol: {"clear_password": True}})
    assert diff == {}
    assert not getattr(service.get(mem_db), protocol).password.get_secret_value()
    assert not service.public(mem_db, editable=True)[protocol]["password_set"]
    update(mem_db, **{protocol: {"password": "replacement-test-only"}})
    assert getattr(service.get(mem_db), protocol).password.get_secret_value() == "replacement-test-only"


@pytest.mark.parametrize("mask", [
    "*", "**", "********", "abcd*wxyz", "abcd********wxyz", "••••••••", "[redacted]", "<redacted>",
])
def test_mask_cannot_replace_saved_password(mask):
    with pytest.raises(ValidationError):
        MailSettingsUpdate.model_validate({"smtp": {"password": mask}})


def test_conflicting_clear_is_atomic_and_audit_diff_excludes_secrets(mem_db):
    with pytest.raises(BizError):
        update(mem_db, smtp={"host": "new.example.com"}, imap={"password": SECRET, "clear_password": True})
    assert mem_db.get(SysSetting, service.KEY) is None
    diff = update(mem_db, smtp={"host": "new.example.com", "password": SECRET}, imap={"password": IMAP_SECRET})
    assert diff == {"smtp.host": ["legacy.example.com", "new.example.com"]}
    assert "password" not in json.dumps(diff)


def test_fresh_reads_with_existing_identity_map(mem_db):
    update(mem_db, smtp={"sender": "one@example.com"})
    first = service.get(mem_db)
    with Session(bind=mem_db.get_bind()) as other:
        update(other, smtp={"sender": "two@example.com", "port": 587, "security": "STARTTLS"})
    second = service.get(mem_db)
    assert first.smtp.sender == "one@example.com"
    assert second.smtp.sender == "two@example.com" and second.smtp.port == 587


@pytest.mark.parametrize("security", ["NONE", "STARTTLS", "SSL"])
@pytest.mark.parametrize("auth", [False, True])
def test_smtp_modes_optional_auth_and_noop_without_sending(monkeypatch, security, auth):
    plain, implicit = MagicMock(), MagicMock()
    monkeypatch.setattr(smtp.smtplib, "SMTP", plain)
    monkeypatch.setattr(smtp.smtplib, "SMTP_SSL", implicit)
    selected = implicit if security == "SSL" else plain
    conn = selected.return_value
    conn.__enter__.return_value = conn
    conn.noop.return_value = (250, b"OK")
    settings = smtp_config(security=security, auth=auth)
    smtp.check_smtp(settings)
    selected.assert_called_once()
    args, kwargs = selected.call_args
    assert args == (settings.host, 25)
    assert kwargs["timeout"] == 15
    if security == "SSL":
        context = kwargs["context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert conn.starttls.call_count == (1 if security == "STARTTLS" else 0)
    if security == "STARTTLS":
        context = conn.starttls.call_args.kwargs["context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    if auth:
        conn.login.assert_called_once_with("smtp-user", SECRET)
    else:
        conn.login.assert_not_called()
    conn.noop.assert_called_once()
    conn.send_message.assert_not_called()


@pytest.mark.parametrize("security", ["NONE", "STARTTLS", "SSL"])
def test_imap_modes_use_verified_tls_and_only_login_noop_logout(monkeypatch, security):
    plain, implicit = MagicMock(), MagicMock()
    monkeypatch.setattr(smtp.imaplib, "IMAP4_SSL", implicit)
    # 保留 imaplib.IMAP4.error 类，避免异常类型判断被 MagicMock 替换。
    monkeypatch.setattr(smtp.imaplib.IMAP4, "__new__", lambda *_a, **_k: plain.return_value)
    selected = implicit if security == "SSL" else plain
    conn = selected.return_value
    conn.login.return_value = ("OK", [b"authenticated"])
    conn.noop.return_value = ("OK", [b"OK"])
    smtp.check_imap(imap_config(security=security))
    if security == "SSL":
        context = implicit.call_args.kwargs["ssl_context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert conn.starttls.call_count == (1 if security == "STARTTLS" else 0)
    if security == "STARTTLS":
        context = conn.starttls.call_args.kwargs["ssl_context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    conn.login.assert_called_once_with("imap-user", IMAP_SECRET)
    conn.noop.assert_called_once()
    conn.logout.assert_called_once()
    assert [call[0] for call in conn.method_calls] == (["starttls"] if security == "STARTTLS" else []) + [
        "login", "noop", "logout"]


def test_runtime_build_and_send_change_without_restart(mem_db, monkeypatch):
    monkeypatch.setattr(service, "SessionLocal", lambda: nullcontext(mem_db))
    factory = MagicMock()
    monkeypatch.setattr(smtp.smtplib, "SMTP", factory)
    conn = factory.return_value
    conn.__enter__.return_value = conn
    conn.send_message.return_value = {}  # smtplib 成功返回空的拒收字典。
    update(mem_db, smtp={"auth": False, "host": "first.example.com", "sender": "first@example.com", "reply_to": ""})
    first = smtp.build_message(subject="test", text="text", html="<p>text</p>", recipients=["to@example.com"], message_id="<1>")
    assert "first@example.com" in first["From"] and first["Reply-To"] is None
    smtp.send(first)
    assert factory.call_args.args == ("first.example.com", 25)
    update(mem_db, smtp={"host": "second.example.com", "port": 2525, "sender": "second@example.com",
                         "reply_to": "reply@example.com"})
    second = smtp.build_message(subject="test", text="text", html="<p>text</p>", recipients=["to@example.com"],
                                message_id="<2>", thread_root="<root>", severity="P1", alarm_id=1, fingerprint="test-fp")
    smtp.send(second)
    assert factory.call_args.args == ("second.example.com", 2525)
    assert "second@example.com" in second["From"] and second["Reply-To"] == "reply@example.com"
    assert second["References"] == "<root>" and second["X-Priority"] == "1"
    assert conn.send_message.call_count == 2


def test_send_sanitizes_hostile_server_exception_for_notifier(monkeypatch):
    class HostileSmtp:
        def __init__(self, *_a, **_k):
            raise smtplib.SMTPAuthenticationError(535, f"smtp-user {SECRET}".encode())

    monkeypatch.setattr(smtp.smtplib, "SMTP", HostileSmtp)
    with pytest.raises(smtp.MailTransportError) as caught:
        smtp.send(EmailMessage(), smtp_config(auth=True))
    assert SECRET not in str(caught.value) and "smtp-user" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_smtp_auth_failure_closes_connection_and_noop_failure_never_sends(monkeypatch):
    factory = MagicMock()
    conn = factory.return_value
    conn.__enter__.return_value = conn
    monkeypatch.setattr(smtp.smtplib, "SMTP", factory)
    conn.login.side_effect = smtplib.SMTPAuthenticationError(535, SECRET.encode())
    with pytest.raises(smtp.MailTransportError) as error:
        smtp.check_smtp(smtp_config(auth=True))
    assert SECRET not in str(error.value)
    conn.close.assert_called_once()
    conn.login.side_effect = None
    conn.noop.return_value = (500, SECRET.encode())
    with pytest.raises(smtp.MailTransportError):
        smtp.check_smtp(smtp_config(auth=False))
    conn.send_message.assert_not_called()


@pytest.mark.parametrize("fail_at", ["login", "noop"])
def test_imap_failure_cleans_up_without_any_mailbox_mutation(monkeypatch, fail_at):
    factory = MagicMock()
    conn = factory.return_value
    monkeypatch.setattr(smtp.imaplib, "IMAP4_SSL", factory)
    conn.login.return_value = ("OK", [])
    conn.noop.return_value = ("OK", [])
    getattr(conn, fail_at).side_effect = smtp.imaplib.IMAP4.error(f"imap-user {IMAP_SECRET}")
    conn.logout.side_effect = RuntimeError(IMAP_SECRET)
    with pytest.raises(smtp.MailTransportError) as error:
        smtp.check_imap(imap_config())
    assert IMAP_SECRET not in str(error.value)
    conn.logout.assert_called_once()
    conn.shutdown.assert_called_once()
    assert {call[0] for call in conn.method_calls} <= {"login", "noop", "logout", "shutdown"}


@pytest.fixture()
def mail_client(mem_db, monkeypatch):
    permissions = ["notify:view", "notify:manage"]
    role = Role(code="mail_test_manager", name="邮件测试管理", permissions=permissions)
    mem_db.add(role)
    mem_db.flush()
    actor = User(username="mail-test", display_name="测试操作人", role_id=role.id,
                 password_hash="synthetic-unusable-test-hash", login_enabled=True,
                 enabled=True, notify_enabled=False, tenant_ids=None)
    recipient = User(username="smtp-test-recipient", display_name="合成测试收件人", email="test@example.com",
                     password_hash="!", login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=[])
    mem_db.add_all([actor, recipient])
    mem_db.flush()
    mem_db.add(Contact(user_id=recipient.id, name=recipient.display_name, email=recipient.email, enabled=True))
    mem_db.commit()
    app: FastAPI = create_app()
    user = CurrentUser(id=actor.id, username=actor.username, display_name=actor.display_name, permissions=permissions)
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(service, "SessionLocal", lambda: nullcontext(mem_db))
    client = TestClient(app)
    yield client, user
    client.close()


def test_mail_api_save_read_and_only_nonsecret_audit(mail_client, mem_db):
    client, _ = mail_client
    response = client.put(f"{API}/mail-settings", json={"smtp": {"port": 25, "password": SECRET},
                                                       "imap": {"password": IMAP_SECRET}})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "database" and data["editable"]
    assert data["smtp"]["password_set"] and data["imap"]["password_set"]
    for path in ("mail-settings", "smtp"):
        text = client.get(f"{API}/{path}").text
        assert SECRET not in text and IMAP_SECRET not in text and "password_encrypted" not in text
    logs = list(mem_db.scalars(select(AuditLog)))
    assert len(logs) == 1 and logs[0].action == "setting.update"
    assert "password" not in json.dumps(logs[0].detail)


@pytest.mark.parametrize("payload", [
    {"smtp": {"port": 0, "password": SECRET}},
    {"smtp": {"port": 65536, "password": SECRET}},
    {"smtp": {"host": "https://user:password@example.com", "password": SECRET}},
    {"smtp": {"sender": "test@example.com\r\nBcc:attacker@example.com", "password": SECRET}},
    {"smtp": {"password": "abcd********wxyz"}},
    {"imap": {"security": "UNKNOWN", "password": IMAP_SECRET}},
    {"unexpected": {"password": SECRET}},
])
def test_validation_errors_never_echo_passwords(mail_client, mem_db, payload):
    client, _ = mail_client
    response = client.put(f"{API}/mail-settings", json=payload)
    assert response.status_code == 400, response.text
    assert SECRET not in response.text and IMAP_SECRET not in response.text
    assert "input" not in response.text and "********" not in response.text
    assert mem_db.get(SysSetting, service.KEY) is None


@pytest.mark.parametrize("permissions,scope", [(["notify:view"], None), (["notify:view", "notify:manage"], [1])])
def test_manage_permission_and_full_scope_protect_all_writes(mail_client, permissions, scope):
    client, user = mail_client
    user.permissions, user.tenant_ids = permissions, scope
    result = client.get(f"{API}/mail-settings")
    assert result.status_code == 200 and not result.json()["data"]["editable"]
    for method, path, body in [("put", "mail-settings", {"smtp": {"port": 25}}),
                               ("post", "test-smtp", None), ("post", "test-imap", None),
                               ("post", "test-mail", {"to": ["test@example.com"]})]:
        assert getattr(client, method)(f"{API}/{path}", json=body).status_code == 403


def test_view_permission_required(mail_client):
    client, user = mail_client
    user.permissions = []
    assert client.get(f"{API}/mail-settings").status_code == 403
    assert client.get(f"{API}/smtp").status_code == 403


def test_connection_tests_never_send_and_test_mail_uses_saved_config(mail_client, mem_db, monkeypatch):
    client, _ = mail_client
    update(mem_db, smtp={"sender": "saved@example.com", "auth": False},
           imap={"enabled": True, "host": "imap.example.com", "username": "imap-user", "password": IMAP_SECRET})
    check_smtp, check_imap, send = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(notify, "check_smtp", check_smtp)
    monkeypatch.setattr(notify, "check_imap", check_imap)
    monkeypatch.setattr(notify, "send", send)
    assert client.post(f"{API}/test-smtp").status_code == 200
    assert client.post(f"{API}/test-imap").status_code == 200
    send.assert_not_called()
    assert check_imap.call_args.args[0].password.get_secret_value() == IMAP_SECRET
    recipient_id = mem_db.scalar(select(User.id).where(User.username == "smtp-test-recipient"))
    assert recipient_id is not None
    assert client.post(f"{API}/test-mail", json={"user_ids": [recipient_id]}).status_code == 200
    assert "saved@example.com" in send.call_args.args[0]["From"]


@pytest.mark.parametrize("path,target", [("test-smtp", "check_smtp"), ("test-imap", "check_imap"), ("test-mail", "send")])
def test_test_endpoints_sanitize_external_errors_in_api_and_audit(mail_client, mem_db, monkeypatch, caplog, path, target):
    client, _ = mail_client
    def boom(*_a, **_k):
        raise RuntimeError(f"server echoed smtp-user {SECRET} {IMAP_SECRET}")
    monkeypatch.setattr(notify, target, boom)
    response = client.post(f"{API}/{path}", json={"to": ["test@example.com"]} if path == "test-mail" else None)
    assert response.status_code == 502
    combined = response.text + caplog.text + json.dumps([log.detail for log in mem_db.scalars(select(AuditLog))])
    assert SECRET not in combined and IMAP_SECRET not in combined and "smtp-user" not in combined
    assert all(log.result == "FAILED" for log in mem_db.scalars(select(AuditLog)))
