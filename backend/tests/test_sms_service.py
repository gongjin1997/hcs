"""短信升级仅使用真实隔离数据库和 stub 网关；不访问任何短信服务。"""
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.crypto import encrypt
from app.core.response import BizError, ErrorCode
from app.models import AlarmEvent, AlarmEventLog, Contact, ContactGroup, HcsAccount, NotifyRoute, Silence, SysSetting, User
from app.models.sms import SmsNotification
from app.notify.sms_gateway import SmsBatchResult, SmsGatewayError, SmsRecipientResult
from app.services import sms_service, sms_settings

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
SINCE = NOW - timedelta(hours=1)


def _setting(db, key, value):
    row = db.get(SysSetting, key)
    if row is None:
        db.add(SysSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _enable(db, tenant=101, **changes):
    _setting(db, "sms", {"enabled": True, "enabled_since": SINCE.isoformat()})
    config = {"enabled": True, "enabled_since": SINCE.isoformat(), "revision": 1,
              "endpoint": f"https://sms-{tenant}.example.invalid/HttpSmsBatchSend", "username": f"synthetic-{tenant}",
              "password_cipher": encrypt(f"synthetic-secret-{tenant}"), "expand_port": "", "verify_tls": True,
              "severities": ["P1", "P2"], **changes}
    _setting(db, f"sms_tenant:{tenant}", config)


def _alarm(db, key="alarm", **changes):
    row = AlarmEvent(**{"fingerprint": f"sms-{key}", "tenant_id": 101, "rule_id": "SMS-TEST",
        "service": "ecs", "resource_type": "ecs_server", "resource_name": "https://private.invalid/13800000001",
        "severity": "P1", "status": "FIRING", "title": "原始告警标题不进入短信", "check_item": "status",
        "first_seen": NOW - timedelta(minutes=20), "last_seen": NOW, **changes})
    db.add(row)
    db.commit()
    return row


def _person(db, key, phone="13800000001", **changes):
    user = User(**{"username": key, "display_name": "电话人员", "email": None, "phone": phone,
                   "password_hash": "!", "login_enabled": False, **changes})
    db.add(user)
    db.flush()
    contact = Contact(user_id=user.id, name=user.display_name, email=user.email or "", phone=user.phone, enabled=False)
    db.add(contact)
    db.flush()
    return user, contact


def _accepted(endpoint, username, password, content, phones, **kwargs):
    return SmsBatchResult([SmsRecipientResult(phone, True, "0", "31415") for phone in phones])


@pytest.fixture()
def sms_world(mem_db):
    for tenant in (101, 202):
        mem_db.add(HcsAccount(id=tenant, code=f"sms-{tenant}", name=f"合成租户{tenant}",
                              iam_endpoint="https://example.invalid", domain_name="synthetic"))
    person, contact = _person(mem_db, "phone-only", "+86 138-0000-0001", tenant_ids=[101])
    group = ContactGroup(code="sms-group", name="短信电话组", member_ids=[contact.id], leader_ids=[contact.id])
    route = NotifyRoute(name="短信升级路由", priority=1, match={}, group_code=group.code,
                        escalation=[{"after_min": 15, "to": "leader", "severities": ["P1", "P2"]}])
    mem_db.add_all([group, route])
    mem_db.commit()
    return person, contact, group, route


def _jobs(db):
    db.expire_all()
    return list(db.scalars(select(SmsNotification).order_by(SmsNotification.id)))


def test_global_and_tenant_are_off_by_default(mem_db, sms_world):
    _alarm(mem_db)
    sender = Mock(side_effect=_accepted)
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0
    sender.assert_not_called()
    assert _jobs(mem_db) == [] and mem_db.scalars(select(SysSetting)).all() == []


@pytest.mark.parametrize("global_enabled,tenant_enabled", [(False, True), (True, False)])
def test_both_switches_are_required(mem_db, sms_world, global_enabled, tenant_enabled):
    _enable(mem_db, enabled=tenant_enabled)
    _setting(mem_db, "sms", {"enabled": global_enabled, "enabled_since": SINCE.isoformat()})
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 0


def test_phone_only_leader_queues_once_without_email_or_login(mem_db, sms_world):
    user, contact, _, route = sms_world
    _enable(mem_db)
    alarm = _alarm(mem_db)
    assert not user.email and not user.login_enabled and not contact.enabled
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    job, = _jobs(mem_db)
    expected = [alarm.id, 0, route.id, 15, user.id]
    assert job.dedup_key == hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    assert (job.tenant_id, job.user_id, job.route_id, job.config_revision, job.status) == (101, user.id, route.id, 1, "PENDING")
    assert job.phone_mask == "138****0001" and "登录巡检平台" in job.content
    serialized = json.dumps({column.name: str(getattr(job, column.name)) for column in job.__table__.columns})
    assert "13800000001" not in serialized and "https://" not in serialized and "private.invalid" not in serialized


@pytest.mark.parametrize("field,value", [("enabled", False), ("notify_enabled", False), ("tenant_ids", [202]),
                                        ("tenant_ids", []), ("phone", "bad"), ("phone", "+12025550123")])
def test_user_total_notify_scope_and_domestic_phone_are_required(mem_db, sms_world, field, value):
    person = sms_world[0]
    setattr(person, field, value)
    mem_db.commit()
    _enable(mem_db)
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 0


@pytest.mark.parametrize("changes", [{"status": "ACKED"}, {"status": "SUPPRESSED"}, {"status": "RESOLVED"},
                                     {"status": "CLOSED"}, {"ack_by": "operator"}, {"ack_at": NOW},
                                     {"severity": "P3"}, {"tenant_id": None}, {"first_seen": NOW}])
def test_only_due_unacked_firing_supported_tenant_and_severity_queue(mem_db, sms_world, changes):
    _enable(mem_db)
    _alarm(mem_db, **changes)
    assert sms_service.enqueue_due(mem_db, NOW) == 0


@pytest.mark.parametrize("switch", ["global", "tenant"])
def test_activation_cutoff_does_not_blast_old_history(mem_db, sms_world, switch):
    _enable(mem_db)
    if switch == "global":
        _setting(mem_db, "sms", {"enabled": True, "enabled_since": (NOW - timedelta(minutes=10)).isoformat()})
    else:
        config = mem_db.get(SysSetting, "sms_tenant:101").value
        _setting(mem_db, "sms_tenant:101", {**config, "enabled_since": (NOW - timedelta(minutes=10)).isoformat()})
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 0


def test_reopen_has_separate_generation_and_escalation_clock(mem_db, sms_world):
    _enable(mem_db)
    alarm = _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    alarm.reopen_count = 1
    mem_db.add(AlarmEventLog(alarm_id=alarm.id, action="REOPEN", at=NOW))
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW + timedelta(minutes=14)) == 0
    assert sms_service.enqueue_due(mem_db, NOW + timedelta(minutes=15)) == 1
    assert {job.alarm_generation for job in _jobs(mem_db)} == {0, 1}


def test_old_alarm_with_new_cycle_after_activation_can_queue(mem_db, sms_world):
    _enable(mem_db)
    alarm = _alarm(mem_db, first_seen=SINCE - timedelta(days=1), reopen_count=1)
    mem_db.add(AlarmEventLog(alarm_id=alarm.id, action="REOPEN", at=NOW - timedelta(minutes=20)))
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW) == 1


@pytest.mark.parametrize("kind", ["SILENCE", "MAINTENANCE"])
def test_silence_is_honored_at_queue_and_again_at_send(mem_db, sms_world, kind):
    _enable(mem_db)
    _alarm(mem_db)
    silence = Silence(name="合成窗口", kind=kind, matchers={"tenant_ids": [101]},
                      starts_at=NOW - timedelta(minutes=1), ends_at=NOW + timedelta(hours=1))
    mem_db.add(silence)
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    silence.cancelled_at = NOW
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    silence.cancelled_at = None
    mem_db.commit()
    sender = Mock(side_effect=_accepted)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0
    sender.assert_not_called()
    assert _jobs(mem_db)[0].status == "SKIPPED"


def test_routes_and_severity_filters_and_named_group_members(mem_db, sms_world):
    _, contact, group, route = sms_world
    other, other_contact = _person(mem_db, "member", "8613900000002", tenant_ids=[101])
    mem_db.add(ContactGroup(code="target", name="目标组", member_ids=[other_contact.id, other_contact.id], leader_ids=[]))
    route.match = {"tenant_ids": [101], "services": ["ecs"], "resource_types": ["ecs_server"], "severities": ["P1"]}
    route.escalation = [{"after_min": 15, "to": "target", "severities": ["P1"]},
                        {"after_min": 5, "to": "leader", "severities": ["P2"]}]
    mem_db.commit()
    _enable(mem_db)
    _alarm(mem_db)
    _alarm(mem_db, "wrong-service", service="dcs")
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    job, = _jobs(mem_db)
    assert job.user_id == other.id and job.user_id != contact.user_id


def test_non_continuing_first_match_blocks_later_route_even_without_email(mem_db, sms_world):
    route = sms_world[3]
    route.escalation = []
    mem_db.add(NotifyRoute(name="后续", priority=2, group_code="sms-group", match={},
                            escalation=[{"after_min": 1, "to": "leader"}]))
    mem_db.commit()
    _enable(mem_db)
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    route.continue_match = True
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW) == 1


def test_batch_bound_does_not_starve_later_alarm_after_dedup(mem_db, sms_world):
    _enable(mem_db)
    for i in range(4):
        _alarm(mem_db, str(i))
    assert [sms_service.enqueue_due(mem_db, NOW, batch=1) for _ in range(5)] == [1, 1, 1, 1, 0]


def test_claim_is_committed_before_send_and_uses_latest_phone(mem_db, sms_world):
    person = sms_world[0]
    _enable(mem_db)
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    person.phone = "86 139-0000-0002"
    mem_db.commit()
    seen = []
    def sender(endpoint, username, password, content, phones, **kwargs):
        with Session(mem_db.get_bind()) as other:
            assert other.scalar(select(SmsNotification.status)) == "PROCESSING"
        assert phones == ["13900000002"] and 0 < kwargs["timeout_seconds"] <= 5
        seen.append(phones)
        return _accepted(endpoint, username, password, content, phones, **kwargs)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 1
    job, = _jobs(mem_db)
    assert job.status == "ACCEPTED" and job.gateway_code == "0" and job.gateway_message_id == "31415"
    assert job.sent_at == NOW and job.phone_mask == "139****0002"
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0 and len(seen) == 1


@pytest.mark.parametrize("change", ["global_off", "tenant_off", "account_off", "person_off", "notify_off",
                                    "scope", "phone", "revision", "ack", "generation", "route", "group"])
def test_pending_jobs_are_revalidated_and_skipped(mem_db, sms_world, change):
    person, _, group, route = sms_world
    _enable(mem_db)
    alarm = _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    if change == "global_off":
        _setting(mem_db, "sms", {"enabled": False})
    elif change in ("tenant_off", "revision"):
        row = mem_db.get(SysSetting, "sms_tenant:101")
        row.value = {**row.value, **({"enabled": False} if change == "tenant_off" else {"revision": 2})}
    elif change == "account_off":
        mem_db.get(HcsAccount, 101).enabled = False
    elif change in ("person_off", "notify_off", "scope", "phone"):
        key, value = {"person_off": ("enabled", False), "notify_off": ("notify_enabled", False),
                      "scope": ("tenant_ids", [202]), "phone": ("phone", "invalid")}[change]
        setattr(person, key, value)
    elif change == "ack":
        alarm.status = "ACKED"
    elif change == "generation":
        alarm.reopen_count += 1
    elif change == "route":
        route.enabled = False
    else:
        group.leader_ids = []
    mem_db.commit()
    sender = Mock(side_effect=_accepted)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0
    sender.assert_not_called()
    assert _jobs(mem_db)[0].status == "SKIPPED"


def test_switch_change_after_claim_is_seen_before_external_call(mem_db, sms_world, monkeypatch):
    _enable(mem_db)
    _alarm(mem_db)
    sms_service.enqueue_due(mem_db, NOW)
    claim = sms_service._claim
    def disable_after_claim(db, job, now):
        result = claim(db, job, now)
        _setting(db, "sms", {"enabled": False})
        return result
    monkeypatch.setattr(sms_service, "_claim", disable_after_claim)
    sender = Mock(side_effect=_accepted)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0
    sender.assert_not_called()
    assert _jobs(mem_db)[0].status == "SKIPPED"


@pytest.mark.parametrize("failure,expected", [(SmsGatewayError("TIMEOUT", uncertain=True), "UNKNOWN"),
    (SmsGatewayError("INVALID_RESPONSE", uncertain=True), "UNKNOWN"),
    (SmsGatewayError("CONNECT", uncertain=False), "FAILED"),
    (RuntimeError("synthetic-secret-101 https://sms.invalid/13800000001"), "UNKNOWN")])
def test_uncertain_and_rejected_calls_never_auto_retry_or_store_raw_errors(mem_db, sms_world, failure, expected, caplog):
    _enable(mem_db)
    _alarm(mem_db)
    sms_service.enqueue_due(mem_db, NOW)
    sender = Mock(side_effect=failure)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 0
    assert sms_service.dispatch_pending(mem_db, NOW + timedelta(hours=1), sender) == 0
    job, = _jobs(mem_db)
    assert job.status == expected and sender.call_count == 1
    assert "synthetic-secret" not in job.error and "https://" not in job.error
    assert "13800000001" not in caplog.text and "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("code", ["2", "11", "4", "6", "7", "30", "44"])
def test_global_gateway_rejections_block_only_this_tenant_revision(mem_db, sms_world, code):
    person = sms_world[0]
    person.tenant_ids = None
    mem_db.commit()
    _enable(mem_db)
    _enable(mem_db, 202)
    _alarm(mem_db, "first")
    _alarm(mem_db, "second")
    _alarm(mem_db, "other-tenant", tenant_id=202)
    assert sms_service.enqueue_due(mem_db, NOW) == 3
    calls = []
    def sender(endpoint, username, password, content, phones, **kwargs):
        calls.append(endpoint)
        if "sms-101." in endpoint:
            return SmsBatchResult([SmsRecipientResult(phones[0], False, code)], code=code)
        return _accepted(endpoint, username, password, content, phones, **kwargs)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 1
    assert Counter(job.status for job in _jobs(mem_db)) == {"FAILED": 1, "SKIPPED": 1, "ACCEPTED": 1}
    assert len(calls) == 2 and mem_db.get(SysSetting, "sms_block:101").value["revision"] == 1
    assert mem_db.get(SysSetting, "sms_block:202") is None
    _alarm(mem_db, "blocked-new")
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    config = mem_db.get(SysSetting, "sms_tenant:101")
    config.value = {**config.value, "revision": 2}
    mem_db.commit()
    assert sms_service.enqueue_due(mem_db, NOW) == 1
    assert sms_service.dispatch_pending(mem_db, NOW, Mock(side_effect=_accepted)) == 1


def test_auth_exception_blocks_and_phone_rejection_does_not_poison_others(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db, "first")
    _alarm(mem_db, "second")
    sms_service.enqueue_due(mem_db, NOW)
    auth = Mock(side_effect=SmsGatewayError("AUTH", uncertain=False))
    assert sms_service.dispatch_pending(mem_db, NOW, auth) == 0 and auth.call_count == 1
    assert [job.status for job in _jobs(mem_db)] == ["FAILED", "SKIPPED"]


def test_phone_code32_is_failed_but_next_recipient_can_be_accepted(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db, "first")
    _alarm(mem_db, "second")
    sms_service.enqueue_due(mem_db, NOW)
    count = 0
    def sender(endpoint, username, password, content, phones, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return SmsBatchResult([SmsRecipientResult(phones[0], False, "32")])
        return _accepted(endpoint, username, password, content, phones, **kwargs)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 1
    assert [job.status for job in _jobs(mem_db)] == ["FAILED", "ACCEPTED"]
    assert mem_db.get(SysSetting, "sms_block:101") is None


def test_decryption_failure_is_safe_and_does_not_stop_other_tenants(mem_db, sms_world, monkeypatch):
    sms_world[0].tenant_ids = None
    mem_db.commit()
    _enable(mem_db)
    _enable(mem_db, 202)
    _alarm(mem_db, "first")
    _alarm(mem_db, "second", tenant_id=202)
    sms_service.enqueue_due(mem_db, NOW)
    password = sms_settings.password
    def decrypt(config):
        if config.username == "synthetic-101":
            raise BizError(ErrorCode.BAD_REQUEST, "synthetic-secret-do-not-store")
        return password(config)
    monkeypatch.setattr(sms_settings, "password", decrypt)
    assert sms_service.dispatch_pending(mem_db, NOW, Mock(side_effect=_accepted)) == 1
    assert [job.status for job in _jobs(mem_db)] == ["FAILED", "ACCEPTED"]
    assert "synthetic-secret" not in _jobs(mem_db)[0].error


def test_crash_left_processing_becomes_unknown_without_any_resend(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db)
    sms_service.enqueue_due(mem_db, NOW)
    sender = Mock(side_effect=SystemExit("synthetic worker crash"))
    with pytest.raises(SystemExit):
        sms_service.dispatch_pending(mem_db, NOW, sender)
    assert _jobs(mem_db)[0].status == "PROCESSING"
    assert sms_service.dispatch_pending(mem_db, NOW + timedelta(seconds=29), sender) == 0
    assert _jobs(mem_db)[0].status == "PROCESSING"
    assert sms_service.dispatch_pending(mem_db, NOW + timedelta(seconds=30), sender) == 0
    assert _jobs(mem_db)[0].status == "UNKNOWN" and sender.call_count == 1
    assert sms_service.enqueue_due(mem_db, NOW + timedelta(hours=1)) == 0


def test_tenant_inflight_mutex_prevents_second_claim(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db, "first")
    _alarm(mem_db, "second")
    sms_service.enqueue_due(mem_db, NOW)
    first, second = _jobs(mem_db)
    assert sms_service._claim(mem_db, first, NOW)
    assert not sms_service._claim(mem_db, second, NOW)
    assert not sms_service._claim(mem_db, first, NOW)
    assert [job.status for job in _jobs(mem_db)] == ["PROCESSING", "PENDING"]


def test_dispatch_has_tenant_fairness_and_ten_second_budget(mem_db, sms_world, monkeypatch):
    sms_world[0].tenant_ids = None
    mem_db.commit()
    _enable(mem_db)
    _enable(mem_db, 202)
    for i in range(5):
        _alarm(mem_db, str(i))
    _alarm(mem_db, "other", tenant_id=202)
    sms_service.enqueue_due(mem_db, NOW)
    clock = [0.0]
    monkeypatch.setattr(sms_service, "monotonic", lambda: clock[0])
    calls = []
    def sender(endpoint, username, password, content, phones, **kwargs):
        assert kwargs["timeout_seconds"] <= 5
        clock[0] += kwargs["timeout_seconds"]
        calls.append(endpoint)
        return _accepted(endpoint, username, password, content, phones, **kwargs)
    assert sms_service.dispatch_pending(mem_db, NOW, sender) == 2
    assert len(calls) == 2 and clock[0] <= 10 and "sms-202." in calls[1]
    assert Counter(job.status for job in _jobs(mem_db)) == {"ACCEPTED": 2, "PENDING": 4}


def test_large_old_tenant_queue_cannot_hide_healthy_tenant_before_batch_limit(mem_db, sms_world, monkeypatch):
    sms_world[0].tenant_ids = None
    mem_db.commit()
    _enable(mem_db)
    _enable(mem_db, 202)
    for index in range(60):
        _alarm(mem_db, f"busy-{index}")
    _alarm(mem_db, "healthy", tenant_id=202)
    assert sms_service.enqueue_due(mem_db, NOW) == 61
    clock = [0.0]
    monkeypatch.setattr(sms_service, "monotonic", lambda: clock[0])
    calls = []
    def sender(endpoint, username, password, content, phones, **kwargs):
        calls.append(endpoint)
        if "sms-101." in endpoint:
            clock[0] += 5
            raise SmsGatewayError("TIMEOUT", uncertain=True)
        return _accepted(endpoint, username, password, content, phones, **kwargs)
    assert sms_service.dispatch_pending(mem_db, NOW, sender, batch=2) == 1
    assert len(calls) == 2 and "sms-202." in calls[1] and clock[0] == 5
    assert Counter(job.status for job in _jobs(mem_db)) == {"UNKNOWN": 1, "ACCEPTED": 1, "PENDING": 59}


@pytest.mark.parametrize("missing", ["global", "tenant", "both"])
def test_missing_activation_timestamp_fails_closed(mem_db, sms_world, missing):
    _enable(mem_db)
    for key in ("sms", "sms_tenant:101"):
        if missing == "both" or key == ("sms" if missing == "global" else "sms_tenant:101"):
            row = mem_db.get(SysSetting, key)
            row.value = {name: value for name, value in row.value.items() if name != "enabled_since"}
    mem_db.commit()
    _alarm(mem_db)
    assert sms_service.enqueue_due(mem_db, NOW) == 0
    assert _jobs(mem_db) == []


def test_content_excludes_resource_names_urls_and_phone_shaped_tenant_names(mem_db, sms_world):
    tenant = mem_db.get(HcsAccount, 101)
    tenant.name = "测试租户202 https://private.invalid/secret +86 13800000001"
    mem_db.commit()
    _enable(mem_db)
    _alarm(mem_db)
    sms_service.enqueue_due(mem_db, NOW)
    content = _jobs(mem_db)[0].content
    assert "测试租户202" in content and "13800000001" not in content and "http" not in content and "secret" not in content


def test_postgresql_claim_sql_is_atomic_and_dedup_is_on_conflict(mem_db, sms_world):
    db = Mock()
    db.get_bind.return_value.dialect = postgresql.dialect()
    statement = sms_service._insert(db, SmsNotification).values(dedup_key="synthetic").on_conflict_do_nothing(
        index_elements=["dedup_key"])
    assert "ON CONFLICT (dedup_key) DO NOTHING" in str(statement.compile(dialect=postgresql.dialect()))
    locked = select(SmsNotification).where(SmsNotification.status == "PENDING").with_for_update(skip_locked=True)
    assert "FOR UPDATE SKIP LOCKED" in str(locked.compile(dialect=postgresql.dialect()))


@pytest.mark.pg
def test_pg_locked_tenant_is_skipped_without_claim(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db)
    sms_service.enqueue_due(mem_db, NOW)
    job, = _jobs(mem_db)
    with Session(mem_db.get_bind()) as other:
        other.scalar(select(HcsAccount).where(HcsAccount.id == 101).with_for_update())
        assert not sms_service._claim(mem_db, job, NOW)
    assert _jobs(mem_db)[0].status == "PENDING"


@pytest.mark.pg
def test_pg_concurrent_enqueue_keeps_one_job_per_generation(mem_db, sms_world):
    _enable(mem_db)
    _alarm(mem_db)
    barrier = Barrier(2)
    def enqueue(_):
        with Session(mem_db.get_bind(), expire_on_commit=False) as db:
            barrier.wait(timeout=5)
            return sms_service.enqueue_due(db, NOW)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(enqueue, range(2))) == 1
    assert len(_jobs(mem_db)) == 1
