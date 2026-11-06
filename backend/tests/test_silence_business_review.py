"""静默输入边界及空租户范围不能扩张为全局静默。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1 import silences
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.main import app as production_app
from app.models import AlarmEvent, Silence
from app.models.base import utcnow
from app.services import alarm_service, silence_service


@pytest.fixture
def api(mem_db):
    app = FastAPI()
    app.include_router(silences.router, prefix="/api/v1")
    app.include_router(silences.quick_router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    user = CurrentUser(id=1, username="reviewer", display_name="reviewer", tenant_ids=[],
                       permissions=["alarm:view", "silence:manage"])
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    mem_db.add(AlarmEvent(tenant_id=1, fingerprint="silence-review", rule_id="rule", service="ecs",
                          resource_type="ecs_server", resource_name="server", severity="P2", status="FIRING",
                          title="CPU 高", check_item="cpu"))
    mem_db.commit()
    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), db=mem_db, user=user)


def payload(**overrides):
    return {"name": "maintenance", "matchers": {"services": ["ecs"]},
            "ends_at": (utcnow() + timedelta(hours=1)).isoformat(), **overrides}


def test_no_tenants_cannot_create_a_global_silence(api):
    response = api.client.post("/api/v1/silences", json=payload())
    assert response.status_code == 403
    assert api.db.scalar(select(Silence)) is None
    assert api.db.scalar(select(AlarmEvent)).status == "FIRING"


def test_selected_tenant_is_preserved_in_saved_matchers(api):
    api.user.tenant_ids = [1]
    response = api.client.post("/api/v1/silences", json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["data"]["matchers"]["tenant_ids"] == [1]


@pytest.mark.parametrize("overrides", [
    {"matchers": {"tenant_ids": ["invalid"]}},
    {"recurrence": {"weekdays": ["invalid"], "start": "22:00", "end": "23:00"}},
    {"ends_at": "2026-10-28T10:00:00"},
    {"name": "   "},
])
def test_invalid_silence_input_returns_client_error_without_mutation(api, overrides):
    api.user.tenant_ids = None
    response = api.client.post("/api/v1/silences", json=payload(**overrides))
    assert response.status_code in (400, 422)
    assert api.db.scalar(select(Silence)) is None
    assert api.db.scalar(select(AlarmEvent)).status == "FIRING"


def test_failed_silence_audit_does_not_commit_suppression(api, monkeypatch):
    api.user.tenant_ids = [1]

    def fail_names(db):
        raise RuntimeError("simulated audit preparation failure")

    monkeypatch.setattr(silences, "_names", fail_names)
    response = api.client.post("/api/v1/silences", json=payload())
    assert response.status_code == 500
    api.db.rollback()
    assert api.db.scalar(select(Silence)) is None
    assert api.db.scalar(select(AlarmEvent)).status == "FIRING"


def test_severity_upgrade_stops_matching_a_lower_severity_silence(api, monkeypatch):
    now = utcnow()
    silence = Silence(name="only P3", matchers={"severities": ["P3"]}, recurrence={},
                      starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    api.db.add(silence)
    api.db.flush()
    row = api.db.scalar(select(AlarmEvent))
    row.status, row.silence_id, row.severity = "SUPPRESSED", silence.id, "P1"
    api.db.commit()
    notices = []
    monkeypatch.setattr(alarm_service, "enqueue_notification", lambda *args, **kw: notices.append(args[2]))
    result = silence_service.sweep(api.db, now)
    assert result["released"] == 1 and row.status == "FIRING"
    assert notices == ["FIRING"]


def test_overlapping_silence_counts_transferred_suppression(api):
    now = utcnow()
    old = Silence(name="expired", matchers={"services": ["ecs"]}, recurrence={},
                  starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1), suppressed_total=1)
    new = Silence(name="active", matchers={"services": ["ecs"]}, recurrence={},
                  starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1), suppressed_total=0)
    api.db.add_all([old, new])
    api.db.flush()
    row = api.db.scalar(select(AlarmEvent))
    row.status, row.silence_id = "SUPPRESSED", old.id
    api.db.commit()
    result = silence_service.sweep(api.db, now)
    assert result["released"] == 0 and row.silence_id == new.id
    assert new.suppressed_total == 1


def test_quick_silence_suppresses_and_counts_each_alarm_only_once(api):
    api.user.tenant_ids = [1]
    row = api.db.scalar(select(AlarmEvent))
    response = api.client.post(f"/api/v1/alarms/{row.id}/silence", json={"minutes": 60})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["suppressed"] == 1
    assert data["silence"]["suppressed_total"] == data["silence"]["suppressed_now"] == 1


@pytest.mark.parametrize("matchers", [
    {"tenant_ids": [True]}, {"tenant_ids": [1.5]},
    {"services": ["ecs"], "severity": ["P1"]},
    {"services": ["ecs"], "name_contains": 123},
])
def test_invalid_matcher_types_cannot_broaden_the_selected_scope(api, matchers):
    api.user.tenant_ids = [1]
    response = api.client.post("/api/v1/silences", json=payload(matchers=matchers))
    assert response.status_code in (400, 422)
    assert api.db.scalar(select(Silence)) is None
