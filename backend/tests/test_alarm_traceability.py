"""P0 合成闭环：巡检事实→静默/路由→SMTP/短信受理→认领→缺失不恢复→有效恢复。

仅使用隔离数据库、合成用户和注入传输；禁止默认 SMTP、短信和云连接。
"""
import json
import os
import smtplib
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1 import alarms, notify
from app.collectors.base import Observation, RunContext
from app.core.config import Settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.hcs import HcsClient
from app.main import app as production_app
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AlarmRule,
    Contact,
    ContactGroup,
    HcsAccount,
    HcsProject,
    Notification,
    NotifyRoute,
    Resource,
    Silence,
    SysSetting,
    User,
)
from app.models.sms import SmsNotification
from app.notify import dispatcher
from app.notify.escalation import escalate_unacked
from app.notify.sms_gateway import SmsGatewayError
from app.services import alarm_service, mail_settings_service, silence_service, sms_service
from app.services.pipeline import process_observations
from tests.test_sms_service import _accepted, _enable

NOW = datetime(2026, 11, 5, 1, 0, tzinfo=UTC)


@pytest.fixture()
def trace_world(mem_db, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("通知核验禁止任何真实网络、SMTP、短信或云连接")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(HcsClient, "__init__", forbidden)
    settings = Settings(_env_file=None, smtp_host="smtp.invalid", smtp_sender="sender@example.invalid",
                        smtp_user="", timezone="UTC")
    monkeypatch.setattr(mail_settings_service, "get_settings", lambda: settings)
    monkeypatch.setattr(mail_settings_service, "SessionLocal", lambda: nullcontext(mem_db))
    monkeypatch.setattr(alarm_service, "utcnow", lambda: NOW + timedelta(minutes=19))
    tenant = HcsAccount(id=101, code="synthetic-trace", name="合成租户", short_name="合成租户",
        iam_endpoint="https://iam.invalid", domain_name="synthetic")
    mem_db.add(tenant)
    mem_db.flush()
    project = HcsProject(id=1001, account_id=101, region_id="synthetic-region", project_id="synthetic-project",
                         project_name="合成项目", external_domain="invalid")
    person = User(username="trace-recipient", display_name="原始值班人员", email="before@example.invalid",
        phone="13800000001", tenant_ids=[101], password_hash="!", login_enabled=False)
    mem_db.add_all([project, person])
    mem_db.flush()
    contact = Contact(user_id=person.id, name=person.display_name, email=person.email, phone=person.phone)
    mem_db.add(contact)
    mem_db.flush()
    group = ContactGroup(code="trace-ops", name="原始值班组", member_ids=[contact.id], leader_ids=[contact.id])
    route = NotifyRoute(name="原始租户路由", group_code=group.code, priority=1,
        match={"tenant_ids": [101], "services": ["dcs"]}, escalation=[{"after_min": 15, "to": "leader"}])
    rule = AlarmRule(id="TRACE-MEMORY", name="原始内存阈值规则", resource_type="dcs_instance", check_item="memory",
        kind="threshold", expr={"value": "memory_usage", "unit": "%", "levels": [{"severity": "P2", "threshold": 80}],
            "title": "缓存内存 {value}（{threshold}）"}, recover={"below": 60, "for_count": 2},
        runbook="原始处置建议")
    mem_db.add_all([group, route, rule])
    mem_db.commit()
    ctx = RunContext(client=SimpleNamespace(), account=tenant.code, project_id=project.project_id,
        tenant_id=tenant.id, tenant_name=tenant.name, project_ref=project.id, region=project.region_id,
            tenant=tenant, params={"_task_code": "dcs_status"})

    def observe(value, at=NOW):
        obs = Observation("dcs_instance", "synthetic-cache", "原始缓存名称", "memory", {"memory_usage": value},
            sub_object="instance", status_raw="RUNNING", metrics={"memory_usage": value},
            attrs={"metrics_source": "DCS", "dcs_monitoring": {"source": "DCS", "observed_at": at.isoformat(),
                "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": 900}})
        return process_observations(mem_db, "dcs", [obs], ctx, at)

    return SimpleNamespace(db=mem_db, tenant=tenant, project=project, user=person, contact=contact, group=group,
                           route=route, rule=rule, observe=observe)


def notifications(db):
    return list(db.scalars(select(Notification).order_by(Notification.id)))


def logs(db, alarm):
    return list(db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id)
                          .order_by(AlarmEventLog.at, AlarmEventLog.id)))


def snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def save_evidence(name, data):
    """仅在核验命令显式指定目录时输出合成记录；不输出配置、口令、完整手机号或签名链接。"""
    destination = os.environ.get("HCS_NOTIFICATION_EVIDENCE_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def test_synthetic_trigger_silence_release_failure_retry_ack_missing_and_real_recovery(trace_world):
    w = trace_world
    silence = Silence(name="合成维护窗口", kind="MAINTENANCE", matchers={"tenant_ids": [101]},
        starts_at=NOW - timedelta(minutes=1), ends_at=NOW + timedelta(hours=1), created_by="synthetic-operator")
    w.db.add(silence)
    w.db.commit()
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    assert alarm.status == "SUPPRESSED" and alarm.silence_id == silence.id
    assert alarm.resource_ref == w.db.scalar(select(Resource.id))
    assert alarm.tenant_id == 101 and alarm.sub_object == "instance"
    assert alarm.current_value == "95%" and alarm.threshold_text == "≥80%"
    assert alarm.detail["facts"]["memory_usage"] == 95
    assert alarm.detail["project"] == "synthetic-project"
    assert [entry.action for entry in logs(w.db, alarm)] == ["FIRE", "SILENCE"]
    assert "不发通知" in logs(w.db, alarm)[-1].note and notifications(w.db) == []
    assert sms_service.enqueue_due(w.db, NOW) == 0
    assert w.db.scalars(select(SysSetting).where(SysSetting.key == "sms")).all() == []

    silence.cancelled_at = NOW + timedelta(minutes=1)
    assert silence_service.release(w.db, silence, silence.cancelled_at, "synthetic-operator") == 1
    w.db.commit()
    notice, = notifications(w.db)
    assert alarm.status == "FIRING" and notice.status == "PENDING"
    assert notice.recipients == ["before@example.invalid"]
    messages = []
    failure = Mock(side_effect=TimeoutError("never-persist-this-transport-body"))
    assert dispatcher.dispatch_pending(w.db, sender=failure, now=NOW + timedelta(minutes=2)) == 0
    assert notice.status == "RETRYING" and notice.retry == 1 and notice.sent_at is None
    assert notice.next_retry_at == NOW + timedelta(minutes=3)
    assert "never-persist" not in notice.error
    retry_entries = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_RETRY"]
    assert len(retry_entries) == 1 and f"#{notice.id}" in retry_entries[0].note
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(minutes=3)) == 1
    assert notice.status == "SENT" and notice.next_retry_at is None
    assert messages[0]["To"] == "before@example.invalid"
    sent_entries = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY"]
    assert "SMTP" in sent_entries[-1].note and "不代表" in sent_entries[-1].note
    assert "before@example.invalid" in sent_entries[-1].note
    assert "95%" in messages[0].get_body(preferencelist=("plain",)).get_content()

    alarm_service.ack(w.db, alarm.id, "synthetic-operator", "合成认领")
    assert alarm.status == "ACKED"
    w.observe(None, NOW + timedelta(minutes=20))
    assert alarm.status == "ACKED" and alarm.resolved_at is None
    assert not any(entry.action == "RESOLVE" for entry in logs(w.db, alarm))
    w.observe(50, NOW + timedelta(minutes=21))
    assert alarm.status == "ACKED"
    w.observe(50, NOW + timedelta(minutes=22))
    assert alarm.status == "RESOLVED" and alarm.resolved_at == NOW + timedelta(minutes=22)
    assert [entry.action for entry in logs(w.db, alarm)] == [
        "FIRE", "SILENCE", "UNSILENCE", "NOTIFY_RETRY", "NOTIFY", "ACK", "RESOLVE"]
    recovery = notifications(w.db)[-1]
    assert recovery.kind == "RESOLVED" and recovery.status == "PENDING"
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(minutes=23)) == 1
    assert "已恢复" in messages[-1]["Subject"]
    save_evidence("closed-loop.json", {"alarm": snapshot(alarm), "timeline": [snapshot(entry) for entry in logs(w.db, alarm)],
        "notifications": [snapshot(entry) for entry in notifications(w.db)], "smtp_stub_submissions": len(messages),
        "source_sample_time_claimed": False, "source_time_boundary": "本地求值及响应获取时间，不是源采样时间",
        "external_network_calls": 0})


@pytest.mark.parametrize("kind", ["FIRING", "UPGRADE", "REPEAT", "ESCALATE"])
@pytest.mark.parametrize("already_suppressed", [False, True])
def test_pending_notifications_cannot_bypass_a_new_silence(trace_world, kind, already_suppressed):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    notice, = notifications(w.db)
    notice.kind = kind
    w.db.add(Silence(name="发信前新增静默", kind="SILENCE", matchers={"tenant_ids": [101]},
        starts_at=NOW, ends_at=NOW + timedelta(hours=1)))
    if already_suppressed:
        alarm.status = "SUPPRESSED"
    w.db.commit()
    sender = Mock()
    assert dispatcher.dispatch_pending(w.db, sender=sender, now=NOW + timedelta(minutes=1)) == 0
    sender.assert_not_called()
    assert notice.status == "MERGED" and notice.sent_at is None
    assert "静默" in notice.error or "静默" in logs(w.db, alarm)[-1].note


def test_retry_and_terminal_failure_keep_each_attempt_and_remove_future_retry(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    notice, = notifications(w.db)
    sender = Mock(side_effect=TimeoutError("synthetic-transport-raw-error"))
    for minute in (1, 3, 9, 25, 56):
        dispatcher.dispatch_pending(w.db, sender=sender, now=NOW + timedelta(minutes=minute))
    assert sender.call_count == 5 and notice.status == "FAILED" and notice.retry == 5
    assert notice.next_retry_at is None and notice.sent_at is None
    attempts = [entry for entry in logs(w.db, alarm) if entry.action in ("NOTIFY_RETRY", "NOTIFY_FAIL")]
    assert [entry.action for entry in attempts] == ["NOTIFY_RETRY"] * 4 + ["NOTIFY_FAIL"]
    assert all(f"#{notice.id}" in entry.note and "synthetic-transport-raw-error" not in entry.note for entry in attempts)


def test_sent_mail_recipient_and_subject_history_survives_user_rule_route_and_resource_edits(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    messages = []
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(minutes=1)) == 1
    notice, = notifications(w.db)
    history = [snapshot(notice), *[snapshot(entry) for entry in logs(w.db, alarm)]]
    w.user.email, w.user.display_name = "after@example.invalid", "新值班人员"
    w.route.name, w.route.group_code = "新路由名称", "missing-new-group"
    w.rule.name, w.rule.expr, w.rule.runbook = "新阈值规则", {"value": "memory_usage", "levels": []}, "新建议"
    w.db.scalar(select(Resource)).name = "新资源名称"
    w.db.commit()
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(hours=1)) == 0
    assert [snapshot(notice), *[snapshot(entry) for entry in logs(w.db, alarm)]] == history
    assert notice.recipients == ["before@example.invalid"] and "原始缓存名称" in notice.subject


def test_sms_accepted_and_actual_masked_recipient_are_immutable_after_configuration_edits(trace_world):
    w = trace_world
    w.observe(95)
    _enable(w.db)
    assert sms_service.enqueue_due(w.db, NOW + timedelta(minutes=16)) == 1
    sender = Mock(side_effect=_accepted)
    assert sms_service.dispatch_pending(w.db, NOW + timedelta(minutes=16), sender=sender) == 1
    job = w.db.scalar(select(SmsNotification))
    assert job.status == "ACCEPTED" and job.phone_mask == "138****0001"
    assert job.gateway_code == "0" and job.gateway_message_id == "31415"
    assert "DELIVERED" not in job.status
    history = snapshot(job)
    w.user.phone, w.user.display_name = "13900000002", "新手机人员"
    w.route.name, w.route.enabled = "新路由", False
    w.db.commit()
    assert sms_service.dispatch_pending(w.db, NOW + timedelta(hours=1), sender=sender) == 0
    assert sender.call_count == 1 and snapshot(job) == history
    assert "13800000001" not in json.dumps(history, default=str, ensure_ascii=False)
    save_evidence("sms-accepted.json", {"notification": history, "gateway_stub_submissions": sender.call_count,
        "terminal_delivery_confirmed": False, "historical_record_unchanged_after_user_and_route_edit": True})


def test_new_contact_email_does_not_silently_retarget_already_queued_mail(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    notice, = notifications(w.db)
    w.user.email = "after@example.invalid"
    w.db.commit()
    sender = Mock()
    assert dispatcher.dispatch_pending(w.db, sender=sender, now=NOW + timedelta(minutes=1)) == 0
    sender.assert_not_called()
    assert notice.recipients == ["before@example.invalid"] and notice.status == "FAILED"
    rejected = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_FAIL"]
    assert len(rejected) == 1 and "未登记" in rejected[0].note


def test_escalation_history_records_selected_route_and_actual_recipients(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    assert escalate_unacked(w.db, NOW + timedelta(minutes=16)) == 1
    escalation = next(entry for entry in logs(w.db, alarm) if entry.action == "ESCALATE")
    assert "原始租户路由" in escalation.note
    assert "before@example.invalid" in escalation.note
    w.route.name, w.user.email = "新路由名称", "after@example.invalid"
    w.db.commit()
    assert "原始租户路由" in escalation.note and "before@example.invalid" in escalation.note


def test_reopen_cycle_updates_current_facts_without_rewriting_prior_notifications_or_logs(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    messages = []
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(seconds=30)) == 1
    sent_before_reopen = snapshot(notifications(w.db)[0])
    logs_before_reopen = [snapshot(entry) for entry in logs(w.db, alarm)]
    w.observe(50, NOW + timedelta(minutes=1))
    w.observe(50, NOW + timedelta(minutes=2))
    assert alarm.status == "RESOLVED"
    w.observe(99, NOW + timedelta(minutes=3))
    assert alarm.status == "FIRING" and alarm.reopen_count == 1 and alarm.current_value == "99%"
    assert alarm.detail["facts"]["memory_usage"] == 99
    assert alarm.detail["resource_id"] == "synthetic-cache" and alarm.detail["project"] == "synthetic-project"
    assert snapshot(notifications(w.db)[0]) == sent_before_reopen
    assert [snapshot(entry) for entry in logs(w.db, alarm)[:len(logs_before_reopen)]] == logs_before_reopen
    assert dispatcher.dispatch_pending(w.db, sender=messages.append, now=NOW + timedelta(minutes=4)) == 1
    notices = notifications(w.db)
    assert [entry.status for entry in notices] == ["SENT", "MERGED", "SENT"]
    assert "轮次" in notices[1].error
    assert snapshot(notices[0]) == sent_before_reopen
    assert "99%" in messages[-1].get_body(preferencelist=("plain",)).get_content()
    save_evidence("reopen-boundary.json", {
        "alarm": snapshot(alarm), "timeline": [snapshot(entry) for entry in logs(w.db, alarm)],
        "notifications": [snapshot(entry) for entry in notices], "latest_observation_memory_usage": 99,
        "detail_facts_match_reopened_observation": alarm.detail["facts"].get("memory_usage") == 99,
        "prior_sent_notification_unchanged": snapshot(notices[0]) == sent_before_reopen,
        "note": "重触发详情使用本轮 99%，旧投递和事件日志不改写"})


def test_unmatched_route_is_persisted_once_and_route_repair_can_enqueue(trace_world):
    w = trace_world
    w.route.match = {"tenant_ids": [202]}
    w.db.commit()
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    assert alarm.status == "FIRING" and notifications(w.db) == []
    misses = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_SKIP"]
    assert len(misses) == 1 and "未匹配通知路由" in misses[0].note
    assert alarm.notify_seq == 0
    first_skip = snapshot(misses[0])
    for minute in range(1, 6):
        assert alarm_service.enqueue_notification(w.db, alarm, "FIRING", NOW + timedelta(minutes=minute)) is None
        w.db.commit()
    assert [snapshot(entry) for entry in logs(w.db, alarm) if entry.action == "NOTIFY_SKIP"] == [first_skip]
    sender = Mock()
    assert dispatcher.dispatch_pending(w.db, sender=sender, now=NOW + timedelta(minutes=1)) == 0
    sender.assert_not_called()
    w.route.match = {"tenant_ids": [101]}
    w.db.commit()
    queued = alarm_service.enqueue_notification(w.db, alarm, "FIRING", NOW + timedelta(minutes=6))
    w.db.commit()
    assert queued is not None and queued.status == "PENDING" and alarm.notify_seq == 1
    assert queued.recipients == ["before@example.invalid"]
    save_evidence("unmatched-route-boundary.json", {
        "timeline": [snapshot(entry) for entry in logs(w.db, alarm)], "notifications": [],
        "persistent_no_recipient_reason_present": any(entry.action == "NOTIFY_SKIP" for entry in logs(w.db, alarm)),
        "skip_count_after_repeated_polls": 1, "queued_after_route_repair": snapshot(queued),
        "note": "跳过原因持久化且不逐轮重复，路由修复后正常入队"})


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_no_recipient_skip_is_deduplicated_before_flush_and_has_actual_reason(trace_world, explicit_empty):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    w.user.notify_enabled = False
    w.db.commit()
    arguments = {"recipients": []} if explicit_empty else {}
    for _ in range(3):
        assert alarm_service.enqueue_notification(w.db, alarm, "REPEAT", NOW + timedelta(minutes=1), **arguments) is None
    w.db.commit()
    skips = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_SKIP"]
    assert len(skips) == 1
    assert ("指定收件人为空" if explicit_empty else "命中路由无可用收件人（路由：原始租户路由）") in skips[0].note
    assert alarm.notify_seq == 1


def test_new_reopen_generation_gets_its_own_no_recipient_reason_without_rewriting_old_skip(trace_world):
    w = trace_world
    w.route.enabled = False
    w.db.commit()
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    first_skip = snapshot(next(entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_SKIP"))
    w.observe(50, NOW + timedelta(minutes=1))
    w.observe(50, NOW + timedelta(minutes=2))
    w.observe(99, NOW + timedelta(minutes=3))
    skips = [entry for entry in logs(w.db, alarm) if entry.action == "NOTIFY_SKIP" and "FIRING" in entry.note]
    assert len(skips) == 2 and "轮次 0" in skips[0].note and "轮次 1" in skips[1].note
    assert snapshot(skips[0]) == first_skip and alarm.detail["facts"]["memory_usage"] == 99


def test_existing_alarm_and_notification_apis_expose_retry_schedule_and_message_id(trace_world):
    w = trace_world
    w.observe(95)
    alarm = w.db.scalar(select(AlarmEvent))
    assert dispatcher.dispatch_pending(w.db, sender=Mock(side_effect=TimeoutError()), now=NOW + timedelta(minutes=1)) == 0
    notice, = notifications(w.db)
    app = FastAPI()
    app.include_router(alarms.router, prefix="/api/v1")
    app.include_router(notify.router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: w.db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=991, username="trace-reader", display_name="合成审查",
        permissions=["alarm:view", "notify:view"], tenant_ids=[101])
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/alarms/{alarm.id}")
        listing = client.get("/api/v1/notify/notifications")
        assert detail.status_code == listing.status_code == 200
        next_retry = (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        for entry in (detail.json()["data"]["notifications"][0], listing.json()["data"][0]):
            assert entry["retry"] == 1 and entry["next_retry_at"] == next_retry
            assert entry["message_id"] is None
        assert dispatcher.dispatch_pending(w.db, sender=Mock(), now=NOW + timedelta(minutes=2)) == 1
        entry = client.get(f"/api/v1/alarms/{alarm.id}").json()["data"]["notifications"][0]
        assert entry["retry"] == 1 and entry["next_retry_at"] is None and entry["message_id"] == notice.message_id


@pytest.mark.parametrize("change", ["global_off", "tenant_off", "ack", "route_off"])
def test_sms_revalidation_records_skip_reason_without_submitting(trace_world, change):
    w = trace_world
    w.observe(95)
    _enable(w.db)
    assert sms_service.enqueue_due(w.db, NOW + timedelta(minutes=16)) == 1
    alarm = w.db.scalar(select(AlarmEvent))
    if change == "global_off":
        w.db.get(SysSetting, "sms").value = {"enabled": False}
    elif change == "tenant_off":
        setting = w.db.get(SysSetting, "sms_tenant:101")
        setting.value = {**setting.value, "enabled": False}
    elif change == "ack":
        alarm_service.ack(w.db, alarm.id, "synthetic-operator")
    else:
        w.route.enabled = False
    w.db.commit()
    sender = Mock()
    assert sms_service.dispatch_pending(w.db, NOW + timedelta(minutes=20), sender=sender) == 0
    sender.assert_not_called()
    job = w.db.scalar(select(SmsNotification))
    assert job.status == "SKIPPED" and job.error and job.sent_at is None
    save_evidence(f"sms-skip-{change}.json", {"notification": snapshot(job), "gateway_stub_submissions": 0})


def test_sms_uncertain_submission_does_not_claim_delivery_or_automatically_retry(trace_world):
    w = trace_world
    w.observe(95)
    _enable(w.db)
    assert sms_service.enqueue_due(w.db, NOW + timedelta(minutes=16)) == 1
    sender = Mock(side_effect=SmsGatewayError("TIMEOUT", uncertain=True))
    assert sms_service.dispatch_pending(w.db, NOW + timedelta(minutes=16), sender=sender) == 0
    assert sms_service.dispatch_pending(w.db, NOW + timedelta(hours=1), sender=sender) == 0
    job = w.db.scalar(select(SmsNotification))
    assert sender.call_count == 1 and job.status == "UNKNOWN" and job.sent_at is None
    save_evidence("sms-unknown.json", {"notification": snapshot(job), "gateway_stub_submissions": 1,
        "terminal_delivery_confirmed": False, "automatically_retried": False})
