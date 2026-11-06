"""通知路由、预览权限、发信及升级状态回归；外部发送始终为 stub。"""

import re
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1 import notify, users
from app.core.config import Settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.security import verify_mail_action
from app.main import app as production_app
from app.models import AlarmEvent, AlarmEventLog, Contact, ContactGroup, HcsAccount, Notification, NotifyRoute, Silence, User
from app.models.base import utcnow
from app.notify import dispatcher, smtp
from app.notify.escalation import escalate_unacked, escalation_plan
from app.services import mail_settings_service


@pytest.fixture()
def notify_api(mem_db, monkeypatch):
    app = FastAPI()
    app.include_router(notify.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    user = CurrentUser(id=1, username="review-user", display_name="审查用户", permissions=["notify:view", "notify:manage"])
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(mail_settings_service, "SessionLocal", lambda: nullcontext(mem_db))
    settings = Settings(_env_file=None, smtp_host="example.invalid", smtp_sender="sender@example.invalid", smtp_user="")
    monkeypatch.setattr(mail_settings_service, "get_settings", lambda: settings)
    for tid in (1, 2):
        mem_db.add(
            HcsAccount(
                id=tid, code=f"tenant-{tid}", name=f"租户{tid}", iam_endpoint="https://example.invalid", domain_name="test-only"
            )
        )
    for cid, name, email in ((1, "成员一", "first@example.invalid"), (2, "成员二", "second@example.invalid")):
        person = User(username=f"notify-member-{cid}", display_name=name, email=email, password_hash="!", login_enabled=False)
        mem_db.add(person)
        mem_db.flush()
        mem_db.add(Contact(id=cid, user_id=person.id, name=name, email=email))
    mem_db.add(ContactGroup(code="ops", name="值班组", member_ids=[1, 2], leader_ids=[2]))
    mem_db.add(NotifyRoute(name="值班路由", priority=10, group_code="ops", escalation=[{"after_min": 15, "to": "leader"}]))
    mem_db.commit()
    with TestClient(app) as client:
        yield client, user


def alarm(db, **extra):
    row = AlarmEvent(
        **{
            "tenant_id": 1,
            "service": "dcs",
            "resource_type": "dcs_instance",
            "resource_name": "测试实例",
            "fingerprint": "review-fingerprint",
            "rule_id": "review-rule",
            "severity": "P2",
            "status": "FIRING",
            "check_item": "memory",
            "title": "测试内存告警",
            **extra,
        }
    )
    db.add(row)
    db.commit()
    return row


def test_readonly_preview_does_not_grant_a_bearer_ack_link(mem_db, notify_api):
    client, user = notify_api
    user.permissions = ["notify:view"]
    row = alarm(mem_db)
    response = client.get(f"/api/v1/notify/preview/{row.id}")
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert "/m/action/" not in preview["html"] and "/m/action/" not in preview["text"]
    user.tenant_ids = [2]
    assert client.get(f"/api/v1/notify/preview/{row.id}").status_code == 404


def test_simulation_cannot_inspect_silence_of_another_tenant(mem_db, notify_api):
    client, user = notify_api
    now = utcnow()
    mem_db.add(
        Silence(
            name="外租户维护窗口",
            kind="SILENCE",
            matchers={"tenant_ids": [2]},
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(hours=1),
        )
    )
    mem_db.commit()
    user.tenant_ids = [1]
    for tid in (2, None):
        response = client.post("/api/v1/notify/simulate", json={"tenant_id": tid, "service": "dcs"})
        assert response.status_code == 404 and "外租户维护窗口" not in response.text
    assert client.post("/api/v1/notify/simulate", json={"tenant_id": 1, "service": "dcs"}).status_code == 200


@pytest.mark.parametrize(
    "match",
    [
        {"service": ["dcs"]},
        {"tenant_ids": ["not-an-id"]},
        {"tenant_ids": [True]},
        {"tenant_ids": [1.5]},
        {"services": [1]},
        {"resource_types": [{}]},
        {"severities": [None]},
        {"services": False},
    ],
)
def test_invalid_route_match_is_400_not_catchall_or_internal_error(mem_db, notify_api, match):
    response = notify_api[0].post("/api/v1/notify/routes", json={"name": "无效条件", "group_code": "ops", "match": match})
    assert response.status_code == 400, response.text
    assert mem_db.scalar(select(NotifyRoute.id).where(NotifyRoute.name == "无效条件")) is None


def test_notification_limit_is_bounded(notify_api):
    for limit in (0, -1, 201):
        response = notify_api[0].get("/api/v1/notify/notifications", params={"limit": limit})
        assert response.status_code == 400


def test_each_recipient_gets_own_action_link_and_retry_does_not_resend_success(mem_db, notify_api, monkeypatch):
    row = alarm(mem_db)
    notification = Notification(alarm_id=row.id, kind="FIRING", recipients=["first@example.invalid", "second@example.invalid"])
    mem_db.add(notification)
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    sent = []

    def sender(message):
        if message["To"] == "second@example.invalid":
            raise RuntimeError("synthetic transport failure")
        sent.append(message)

    dispatcher.dispatch_pending(mem_db, sender=sender)
    assert len(sent) == 1 and sent[0]["To"] == "first@example.invalid"
    pending = list(mem_db.scalars(select(Notification).order_by(Notification.id)))
    assert [n.status for n in pending] == ["SENT", "RETRYING"]
    assert pending[1].recipients == ["second@example.invalid"]
    dispatcher.dispatch_pending(mem_db, sender=sent.append, now=utcnow() + timedelta(hours=1))
    assert [message["To"] for message in sent] == ["first@example.invalid", "second@example.invalid"]
    for message in sent:
        html = message.get_body(preferencelist=("html",)).get_content()
        token = re.search(r"/m/action/([^\"<>\s]+)", html).group(1)
        assert verify_mail_action(token)["recipient"] == message["To"]
    assert len({message["Message-ID"] for message in sent}) == 2


def test_partial_smtp_refusal_is_failure_not_false_success(monkeypatch):
    config = mail_settings_service.SmtpRuntime(host="example.invalid", sender="sender@example.invalid")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.send_message.return_value = {"refused@example.invalid": (550, b"rejected")}
    monkeypatch.setattr(smtp, "_connect_smtp", lambda _settings: connection)
    from email.message import EmailMessage

    with pytest.raises(smtp.MailTransportError):
        smtp.send(EmailMessage(), config)


def test_disabling_contact_prevents_already_queued_delivery(mem_db, notify_api, monkeypatch):
    client, actor = notify_api
    actor.permissions.append("user:manage")
    row = alarm(mem_db)
    mem_db.add(Notification(alarm_id=row.id, kind="FIRING", recipients=["first@example.invalid", "second@example.invalid"]))
    mem_db.commit()
    contact = mem_db.get(Contact, 2)
    response = client.patch(f"/api/v1/system/users/{contact.user_id}", json={"enabled": False})
    assert response.status_code == 200 and response.json()["data"]["contact_id"] == contact.id
    mem_db.refresh(contact)
    assert contact.enabled is False and mem_db.get(User, contact.user_id).enabled is False
    assert mem_db.scalar(select(ContactGroup).where(ContactGroup.code == "ops")).member_ids == [1, 2]
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    messages = []
    assert dispatcher.dispatch_pending(mem_db, sender=messages.append) == 1
    assert [message["To"] for message in messages] == ["first@example.invalid"]
    blocked = mem_db.scalar(select(Notification).where(Notification.status == "FAILED"))
    assert blocked.recipients == ["second@example.invalid"] and "停用" in blocked.error


def test_reopen_restarts_escalation_clock_and_deduplication(mem_db, notify_api, monkeypatch):
    now = utcnow()
    row = alarm(mem_db, first_seen=now - timedelta(hours=2), reopen_count=1)
    mem_db.add_all(
        [
            AlarmEventLog(alarm_id=row.id, action="ESCALATE", operator="system", at=now - timedelta(hours=1)),
            AlarmEventLog(alarm_id=row.id, action="REOPEN", operator="system", at=now),
        ]
    )
    mem_db.commit()
    assert escalate_unacked(mem_db, now=now + timedelta(minutes=1)) == 0
    assert escalate_unacked(mem_db, now=now + timedelta(minutes=16)) == 1
    assert escalate_unacked(mem_db, now=now + timedelta(hours=1)) == 0


def test_escalation_tie_break_matches_primary_routing(mem_db, notify_api):
    row = alarm(mem_db)
    first = mem_db.scalar(select(NotifyRoute))
    mem_db.add(ContactGroup(code="other", name="另一组", member_ids=[1], leader_ids=[1]))
    mem_db.add(
        NotifyRoute(
            name="另一条路由", priority=first.priority, group_code="other", escalation=[{"after_min": 5, "to": "leader"}]
        )
    )
    mem_db.commit()
    assert escalation_plan(mem_db, row)[0]["route"] == first.name


@pytest.mark.parametrize("kind", ["ESCALATE", "REPEAT"])
@pytest.mark.parametrize("status", ["ACKED", "SUPPRESSED", "RESOLVED", "CLOSED"])
def test_pending_unacked_reminder_is_skipped_after_state_change(mem_db, notify_api, monkeypatch, kind, status):
    now = utcnow()
    row = alarm(mem_db, status=status)
    notice = Notification(
        alarm_id=row.id,
        kind=kind,
        status="RETRYING",
        recipients=["first@example.invalid"],
        created_at=now - timedelta(minutes=10),
    )
    mem_db.add(notice)
    mem_db.commit()
    hooks = MagicMock()
    monkeypatch.setattr(dispatcher, "_platform_hooks", hooks)
    sender = MagicMock()
    assert dispatcher.dispatch_pending(mem_db, sender=sender, now=now) == 0
    sender.assert_not_called()
    assert notice.status == "MERGED" and "过时" in notice.error
    assert notice.next_retry_at is None and notice.sent_at is None
    hooks.assert_called_once_with(mem_db, 0, [])


@pytest.mark.parametrize("kind", ["ESCALATE", "REPEAT"])
def test_pending_reminder_respects_active_silence_before_alarm_is_suppressed(mem_db, notify_api, monkeypatch, kind):
    now = utcnow()
    row = alarm(mem_db, status="FIRING")
    notice = Notification(alarm_id=row.id, kind=kind, recipients=["first@example.invalid"])
    mem_db.add(notice)
    mem_db.add(
        Silence(
            name="新生效的维护窗口",
            kind="SILENCE",
            matchers={"tenant_ids": [1]},
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(hours=1),
        )
    )
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    sender = MagicMock()
    assert dispatcher.dispatch_pending(mem_db, sender=sender, now=now) == 0
    sender.assert_not_called()
    assert notice.status == "MERGED"


@pytest.mark.parametrize("kind", ["ESCALATE", "REPEAT", "RESOLVED"])
def test_previous_cycle_reminder_and_recovery_do_not_describe_reopened_alarm(mem_db, notify_api, monkeypatch, kind):
    now = utcnow()
    row = alarm(mem_db, status="FIRING", reopen_count=1)
    notice = Notification(
        alarm_id=row.id, kind=kind, recipients=["first@example.invalid"], created_at=now - timedelta(minutes=1)
    )
    mem_db.add(notice)
    mem_db.add(AlarmEventLog(alarm_id=row.id, action="REOPEN", operator="system", at=now))
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    sender = MagicMock()
    assert dispatcher.dispatch_pending(mem_db, sender=sender, now=now + timedelta(minutes=1)) == 0
    sender.assert_not_called()
    assert notice.status == "MERGED" and "轮次" in notice.error


@pytest.mark.parametrize("kind,status", [("FIRING", "RESOLVED"), ("RESOLVED", "RESOLVED"), ("REPEAT", "FIRING")])
def test_current_valid_notification_and_historical_firing_are_preserved(mem_db, notify_api, monkeypatch, kind, status):
    now = utcnow()
    row = alarm(mem_db, status=status, reopen_count=1, resolved_at=now if status == "RESOLVED" else None)
    mem_db.add(AlarmEventLog(alarm_id=row.id, action="REOPEN", operator="system", at=now - timedelta(minutes=1)))
    notice = Notification(alarm_id=row.id, kind=kind, recipients=["first@example.invalid"], created_at=now)
    mem_db.add(notice)
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_args: None)
    messages = []
    assert dispatcher.dispatch_pending(mem_db, sender=messages.append, now=now + timedelta(minutes=1)) == 1
    assert notice.status == "SENT" and len(messages) == 1
