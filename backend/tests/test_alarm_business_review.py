"""告警重开、并发认领、连续恢复和试运行边界的独立回归。"""
from datetime import timedelta
from html.parser import HTMLParser
from logging import WARNING
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1 import alarms, rules
from app.collectors.base import Observation
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.main import app as production_app
from app.models import AlarmEvent, AlarmEventLog, Resource
from app.models.base import utcnow
from app.notify.render import render_alarm_mail
from app.rules.evaluator import Decision, evaluate_rule
from app.rules.model import Match, Rule
from app.services import alarm_service


def alarm(db, **overrides):
    row = AlarmEvent(**{"fingerprint": "review-fp", "rule_id": "review-rule", "tenant_id": 1,
                       "service": "ecs", "resource_type": "ecs_server", "resource_name": "server",
                       "check_item": "cpu", "severity": "P2", "status": "FIRING", "title": "CPU 高",
                       **overrides})
    db.add(row)
    db.commit()
    return row


def threshold(**overrides):
    return Rule(**{"id": "review-rule", "name": "CPU", "resource_type": "ecs_server", "check_item": "cpu",
                   "kind": "threshold", "expr": {"value": "value", "levels": [
                       {"severity": "P2", "threshold": 80}]}, "recover": {"below": 60, "for_count": 2},
                   **overrides})


def test_recovery_requires_consecutive_samples():
    rule, state = threshold(), {}
    assert evaluate_rule(rule, {"value": 50}, state, "P2").action is None
    assert evaluate_rule(rule, {"value": 70}, state, "P2").action == "REFRESH"
    assert evaluate_rule(rule, {"value": 50}, state, "P2").action is None
    assert evaluate_rule(rule, {"value": 50}, state, "P2").action == "RESOLVE"


def test_missing_sample_breaks_confirmation_without_changing_alarm_status():
    rule, state = threshold(for_count=2), {}
    assert evaluate_rule(rule, {"value": 90}, state, None).action is None
    assert evaluate_rule(rule, {}, state, None).action is None
    assert evaluate_rule(rule, {"value": 90}, state, None).action is None
    assert evaluate_rule(rule, {"value": 90}, state, None).action == "FIRE"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), False, "unavailable"])
def test_invalid_threshold_samples_do_not_resolve_or_fire(value):
    result = evaluate_rule(threshold(), {"value": value}, {}, "P2")
    assert result.action is None and result.match is None


def test_reopen_clears_previous_ack_and_silence(mem_db, monkeypatch):
    now = utcnow()
    previous = alarm(mem_db, status="RESOLVED", resolved_at=now - timedelta(minutes=1),
                     ack_by="previous-owner", ack_at=now - timedelta(minutes=2),
                     silence_id=77, suppress_reason="old maintenance")
    resource = Resource(id=1, tenant_id=1, service="ecs", resource_type="ecs_server",
                        resource_id="server-id", name="server", attrs={})
    mem_db.add(resource)
    mem_db.commit()
    monkeypatch.setattr(alarm_service, "enqueue_notification", lambda *a, **kw: None)
    alarm_service.apply_decision(
        mem_db, rule=threshold(), decision=Decision("FIRE", Match("P2", title="CPU 高")),
        fp=previous.fingerprint, resource=resource,
        obs=Observation("ecs_server", "server-id", "server", "cpu", {"value": 90}),
        facts={"value": 90}, now=now, active=None)
    mem_db.commit()
    assert previous.status == "FIRING" and previous.reopen_count == 1
    assert previous.ack_at is None and previous.ack_by is None
    assert previous.silence_id is None and previous.suppress_reason is None


@pytest.mark.parametrize("batch", [False, True])
def test_stale_session_cannot_ack_a_concurrently_resolved_alarm(mem_db, monkeypatch, batch):
    row = alarm(mem_db)
    monkeypatch.setattr(alarm_service, "publish", lambda event: None)
    with Session(mem_db.bind) as other:
        current = other.get(AlarmEvent, row.id)
        current.status, current.resolved_at = "RESOLVED", utcnow()
        other.commit()
    if batch:
        assert alarm_service.batch_ack(mem_db, [row.id], "operator", tenant_ids=[1]) == []
    else:
        with pytest.raises(BizError) as exc:
            alarm_service.ack(mem_db, row.id, "operator")
        assert exc.value.http_status == 409
    mem_db.expire_all()
    assert mem_db.get(AlarmEvent, row.id).status == "RESOLVED"
    assert mem_db.scalar(select(AlarmEventLog)) is None


@pytest.fixture
def api(mem_db):
    app = FastAPI()
    app.include_router(alarms.mail_router, prefix="/api/v1")
    app.include_router(rules.router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="reviewer", display_name="reviewer", permissions=["rule:view"])
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("expression", ["min(())", "round(1e309)", "'x' * 4", "'x' + 'y'"])
def test_untargeted_dry_run_rejects_invalid_arithmetic_without_500(api, expression):
    response = api.post("/api/v1/rules/dry-run", json={"kind": "threshold", "facts": {},
                        "expr": {"value": expression, "levels": [{"severity": "P2", "threshold": 1}]}})
    assert response.status_code in (200, 400, 422)
    if response.status_code == 200:
        assert response.json()["data"]["matched"] is False
        assert response.json()["data"]["recovered"] is False


def test_rule_rendered_fields_fit_alarm_storage():
    rule = Rule(id="long", name="long", resource_type="ecs_server", check_item="state", kind="state",
                expr={"when": "status != 'ACTIVE'", "severity": "P1", "title": "x" * 500,
                      "threshold_text": "t" * 256})
    match = rule.match({"status": "s" * 128})
    assert match is not None
    assert len(match.title) <= 255 and len(match.current_value) <= 64 and len(match.threshold_text) <= 64


def test_old_mail_link_cannot_ack_reopened_generation(api, mem_db, monkeypatch):
    row = alarm(mem_db, reopen_count=1)
    monkeypatch.setattr(alarms, "verify_mail_action", lambda token: {
        "alarm_id": row.id, "action": "ack", "recipient": "review@example.invalid", "reopen_count": 0})
    response = api.post("/api/v1/mail-actions/signed-fixture")
    assert response.status_code in (401, 409)
    mem_db.refresh(row)
    assert row.status == "FIRING" and row.ack_by is None


def test_mail_ack_is_idempotent_for_same_generation(api, mem_db, monkeypatch):
    row = alarm(mem_db)
    monkeypatch.setattr(alarms, "verify_mail_action", lambda token: {
        "alarm_id": row.id, "action": "ack", "recipient": "review@example.invalid", "reopen_count": 0})
    monkeypatch.setattr(alarm_service, "publish", lambda event: None)
    assert api.post("/api/v1/mail-actions/signed-fixture").status_code == 200
    assert api.post("/api/v1/mail-actions/signed-fixture").status_code == 200
    assert len(list(mem_db.scalars(select(AlarmEventLog)))) == 1


def test_rendered_signed_mail_link_tracks_real_reopen_and_ack(api, mem_db, caplog):
    """真实模板 → 真实签名/验签 → HTTP 认领；不能用模拟载荷掩盖跨模块契约。"""
    caplog.set_level(WARNING, logger="httpx")  # 签名链接是 bearer 凭据，不写入测试请求日志。
    row = alarm(mem_db)
    resource = Resource(tenant_id=1, service="ecs", resource_type="ecs_server", resource_id="mail-resource",
                        name="server", attrs={})
    mem_db.add(resource)
    mem_db.commit()
    obs = Observation("ecs_server", resource.resource_id, resource.name, "cpu", {"value": 90})

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hrefs = []

        def handle_starttag(self, tag, attrs):
            if tag == "a" and (href := dict(attrs).get("href")) and "/m/action/" in href:
                self.hrefs.append(href)

    def ack_path():
        _, html, _ = render_alarm_mail(mem_db, row, "FIRING", "review@example.invalid")
        links = Links()
        links.feed(html)
        for href in links.hrefs:
            token = urlsplit(href).path.rsplit("/", 1)[-1]
            info = alarms.verify_mail_action(token)
            if info and info["action"] == "ack":
                assert info.get("reopen_count", 0) == row.reopen_count
                return f"/api/v1/mail-actions/{token}"
        pytest.fail("邮件未渲染出可验证的认领链接")

    old = ack_path()
    assert api.post(old).status_code == 200
    assert api.post(old).status_code == 200
    now = utcnow()
    alarm_service.apply_decision(mem_db, rule=threshold(), decision=Decision("RESOLVE"), fp=row.fingerprint,
                                  resource=resource, obs=obs, facts={"value": 50}, now=now, active=row)
    mem_db.commit()
    alarm_service.apply_decision(mem_db, rule=threshold(), decision=Decision("FIRE", Match("P2", title="CPU 高")),
                                  fp=row.fingerprint, resource=resource, obs=obs, facts=obs.facts,
                                  now=now + timedelta(seconds=1), active=None)
    mem_db.commit()
    assert row.reopen_count == 1 and row.ack_at is None
    assert api.get(old).status_code == 409
    assert api.post(old).status_code == 409
    assert row.status == "FIRING"
    current = ack_path()
    assert api.post(current).status_code == 200
    assert api.post(current).status_code == 200
    assert len(list(mem_db.scalars(select(AlarmEventLog).where(AlarmEventLog.action == "ACK")))) == 2
