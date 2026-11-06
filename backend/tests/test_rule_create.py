"""规则创建的权限、目标、AST、持久化及下一轮巡检回归。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors.base import Observation, RunContext
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.main import app
from app.models import AlarmEvent, AlarmRule, AuditLog
from app.models.base import utcnow
from app.rules.loader import rules_by_target, sync_default_rules
from app.services.pipeline import process_observations
from app.services.rule_service import list_rule_targets


@pytest.fixture
def rule_api(mem_db):
    user = CurrentUser(id=1, username="rule-editor", display_name="规则编辑员",
                       permissions=["rule:view", "rule:edit"])
    old = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield SimpleNamespace(client=TestClient(app), db=mem_db, user=user)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


def body(**overrides):
    return {"id": "CUSTOM_ECS_CPU", "name": "自定义 CPU 阈值", "resource_type": "ecs_server",
            "check_item": "cpu", "kind": "threshold", "expr": {"value": "cpu_util", "unit": "%",
            "levels": [{"severity": "P2", "op": ">=", "threshold": 85}], "title": "CPU {value}（{threshold}）"},
            "for_count": 1, "recover": {"below": 75}, "enabled": True, **overrides}


def test_create_records_version_and_atomic_audit(rule_api):
    response = rule_api.client.post("/api/v1/rules", json=body())
    assert response.status_code == 201, response.text
    row = response.json()["data"]
    assert row["id"] == "CUSTOM_ECS_CPU" and row["version"] == 1
    assert row["updated_by"] == "规则编辑员" and row["enabled"] is True
    assert rule_api.db.get(AlarmRule, row["id"]).expr["value"] == "cpu_util"
    audit = rule_api.db.scalar(select(AuditLog))
    assert audit.action == "rule.create" and audit.target_id == row["id"]
    assert audit.detail["snapshot"]["version"] == 1
    assert audit.detail["snapshot"]["expr"]["levels"][0]["threshold"] == 85
    assert rule_api.client.get("/api/v1/rules").json()["data"][0]["id"] == row["id"]


def test_create_state_rule(rule_api):
    response = rule_api.client.post("/api/v1/rules", json=body(
        id="CUSTOM_ECS_STATE", kind="state", check_item="instance_status",
        expr={"when": "status == 'ERROR'", "severity": "P1", "title": "实例 {status}"},
        recover={"when": "status == 'ACTIVE'"}))
    assert response.status_code == 201, response.text
    assert response.json()["data"]["kind"] == "state"


def test_permissions_reuse_existing_rule_points(rule_api):
    rule_api.user.permissions = ["rule:view"]
    assert rule_api.client.get("/api/v1/rules/targets").status_code == 200
    assert rule_api.client.post("/api/v1/rules", json=body()).status_code == 403
    assert rule_api.db.scalar(select(AlarmRule)) is None
    rule_api.user.permissions = []
    assert rule_api.client.get("/api/v1/rules/targets").status_code == 403


def test_conflict_preserves_custom_and_builtin_rules(rule_api):
    sync_default_rules(rule_api.db)
    original = rule_api.db.get(AlarmRule, "R-DCS-005").expr.copy()
    assert rule_api.client.post("/api/v1/rules", json=body()).status_code == 201
    for rule_id in ["CUSTOM_ECS_CPU", "R-DCS-005"]:
        response = rule_api.client.post("/api/v1/rules", json=body(id=rule_id))
        assert response.status_code == 409 and response.json()["code"] == 40901
    assert rule_api.db.get(AlarmRule, "R-DCS-005").expr == original
    assert len(list(rule_api.db.scalars(select(AuditLog)))) == 1


@pytest.mark.parametrize("overrides", [
    {"id": "bad/id"}, {"id": "x" * 65}, {"name": " "}, {"name": "x" * 129},
    {"name": 123}, {"kind": "event"}, {"resource_type": "ecs_sever"}, {"resource_type": "evs_volume"},
    {"check_item": "cpu_util"}, {"check_item": "memory"}, {"enabled": "true"},
    {"for_count": 0}, {"for_count": True}, {"for_count": "2"}, {"repeat_interval_sec": -1},
    {"runbook": "x" * 10001}, {"service": "ecs"}, {"scope": {"tenant": "ignored"}},
    {"expr": {"value": "cpu_utils", "levels": [{"severity": "P2", "threshold": 85}]}},
    {"expr": {"value": "cpu_util", "levels": []}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P0", "threshold": 85}]}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P2", "threshold": "85"}]}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P2", "threshold": True}]}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P2", "op": "==", "threshold": 85}]}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P2", "threshold": 85}] * 2}},
    {"expr": {"value": "cpu_util", "levels": [{"severity": "P2", "threshold": 85}], "title": "{cpu_util.__class__}"}},
    {"recover": {"when": "mem_util < 70"}},
])
def test_invalid_inputs_leave_no_rule_or_audit(rule_api, overrides):
    response = rule_api.client.post("/api/v1/rules", json=body(**overrides))
    assert response.status_code in {400, 422}, response.text
    assert rule_api.db.scalar(select(AlarmRule)) is None
    assert rule_api.db.scalar(select(AuditLog)) is None


@pytest.mark.parametrize("expression", [
    "__import__('os').system('id')", "cpu_util.__class__", "[x for x in cpu_util]", "open('/tmp/no')",
    "cpu_util >", "cpu_util ** 100", "cpu_util " + "+ cpu_util " * 55, "cpu_util + " + "1" * 501,
    "cpu_util + 'x'", "cpu_util >= '85'", "cpu_util / 0", "cpu_util % 0", "1e999 + cpu_util",
    "round(cpu_util, 1.5)", "85", "len(cpu_util)", "max(cpu_util)",
])
def test_safe_ast_and_fact_type_validation(rule_api, expression):
    response = rule_api.client.post("/api/v1/rules", json=body(expr={
        "value": expression, "levels": [{"severity": "P2", "threshold": 85}]}))
    assert response.status_code in {400, 422}, response.text
    assert rule_api.db.scalar(select(AlarmRule)) is None


def test_targets_only_include_real_observation_checks(rule_api):
    response = rule_api.client.get("/api/v1/rules/targets")
    assert response.status_code == 200, response.text
    targets = response.json()["data"]
    cpu = next(item for item in targets if (item["resource_type"], item["check_item"]) == ("ecs_server", "cpu"))
    assert cpu["service"] == "ecs" and cpu["collection_tasks"]
    keys = {field["key"] for field in cpu["facts"]}
    assert "cpu_util" in keys and "mem_util" not in keys and "critical" in keys
    assert cpu["expression_examples"]
    assert not any(item["resource_type"] in {"evs_volume", "vpc", "elb"} for item in targets)
    assert len(list_rule_targets()) == len(targets)


def test_edit_toggle_versions_and_conflicts(rule_api):
    assert rule_api.client.post("/api/v1/rules", json=body()).status_code == 201
    update = {key: value for key, value in body().items() if key not in {"id", "resource_type", "check_item"}}
    response = rule_api.client.put("/api/v1/rules/CUSTOM_ECS_CPU", json={**update, "version": 1, "enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["version"] == 2 and response.json()["data"]["enabled"] is False
    response = rule_api.client.put("/api/v1/rules/CUSTOM_ECS_CPU", json={**update, "version": 1})
    assert response.status_code == 409
    assert rule_api.db.get(AlarmRule, "CUSTOM_ECS_CPU").enabled is False
    updates = list(rule_api.db.scalars(select(AuditLog).where(AuditLog.action == "rule.update")))
    assert len(updates) == 1
    assert updates[0].detail["before"]["version"] == 1 and updates[0].detail["snapshot"]["version"] == 2
    rule_api.user.permissions = ["rule:view"]
    assert rule_api.client.put("/api/v1/rules/CUSTOM_ECS_CPU", json=update).status_code == 403


def test_targeted_dry_run_uses_same_validation(rule_api):
    source = body()
    payload = {key: source[key] for key in ["resource_type", "check_item", "kind", "expr", "recover"]}
    payload["facts"] = {"cpu_util": 90}
    response = rule_api.client.post("/api/v1/rules/dry-run", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["matched"] is True
    assert rule_api.client.post("/api/v1/rules/dry-run", json={**payload, "check_item": "memory"}).status_code == 400
    assert rule_api.db.scalar(select(AlarmRule)) is None


def test_saved_rule_is_loaded_and_evaluated_next_cycle(rule_api):
    assert rule_api.client.post("/api/v1/rules", json=body()).status_code == 201
    assert rules_by_target(rule_api.db, {"ecs_server"})[("ecs_server", "cpu")][0].id == "CUSTOM_ECS_CPU"
    context = RunContext(client=None, account="test", project_id="test-project")
    observation = Observation(resource_type="ecs_server", resource_id="cpu-hot", name="CPU hot",
                              check_item="cpu", facts={"cpu_util": 90})
    now = utcnow()
    process_observations(rule_api.db, "ecs", [observation], context, now)
    alarm = rule_api.db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "CUSTOM_ECS_CPU"))
    assert alarm is not None and alarm.severity == "P2" and alarm.status == "FIRING"
    update = {key: value for key, value in body().items() if key not in {"id", "resource_type", "check_item"}}
    response = rule_api.client.put("/api/v1/rules/CUSTOM_ECS_CPU", json={**update, "enabled": False, "version": 1})
    assert response.status_code == 200
    assert not rules_by_target(rule_api.db, {"ecs_server"})
    process_observations(rule_api.db, "ecs", [Observation(resource_type="ecs_server", resource_id="second",
                         name="Second resource", check_item="cpu", facts={"cpu_util": 99})],
                         context, now + timedelta(minutes=1))
    assert len(list(rule_api.db.scalars(select(AlarmEvent)))) == 1


def test_existing_builtin_definitions_still_validate():
    from app.rules.loader import load_yaml_rules
    from app.schemas.rule import RuleIn
    from app.services.rule_service import validate_rule

    for raw in load_yaml_rules():
        definition = RuleIn.model_validate({key: value for key, value in raw.items()
                                           if key not in {"id", "resource_type", "check_item"}})
        # 历史未采集规则仍需通过语法检查，但不再允许启用或新建。
        validate_rule(definition, raw["resource_type"], raw["check_item"], allow_unavailable=True)


@pytest.mark.parametrize("resource_type,check_item,value", [
    ("gaussdb_instance", "cpu", "cpu_usage"), ("gaussdb_instance", "memory", "mem_usage"),
    ("dcs_instance", "cpu", "cpu_usage"), ("dcs_instance", "connections", "connected_clients"),
])
def test_uncollected_metrics_cannot_create_enabled_rules(rule_api, resource_type, check_item, value):
    payload = body(id="UNAVAILABLE_METRIC", resource_type=resource_type, check_item=check_item,
                   expr={"value": value, "levels": [{"severity": "P2", "threshold": 80}]})
    response = rule_api.client.post("/api/v1/rules", json=payload)
    assert response.status_code == 400
    assert "未采集" in response.json()["message"]
    target = next(t for t in rule_api.client.get("/api/v1/rules/targets").json()["data"]
                  if (t["resource_type"], t["check_item"]) == (resource_type, check_item))
    assert target["collection_available"] is False


def test_historical_uncollected_rule_can_be_disabled_but_not_enabled(rule_api):
    sync_default_rules(rule_api.db)
    rule = rule_api.db.get(AlarmRule, "R-GS-007")
    payload = {key: getattr(rule, key) for key in ("name", "kind", "expr", "recover", "for_count", "scope")}
    payload["enabled"] = False
    response = rule_api.client.put(f"/api/v1/rules/{rule.id}", json=payload)
    assert response.status_code == 200
    payload["enabled"] = True
    response = rule_api.client.put(f"/api/v1/rules/{rule.id}", json=payload)
    assert response.status_code == 400
    rule_api.db.refresh(rule)
    assert rule.enabled is False
