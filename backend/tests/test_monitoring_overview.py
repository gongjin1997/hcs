"""监控覆盖必须来自本地有效巡检证据，并服从类型权限和租户范围。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.api.v1.monitoring import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.hcs import HcsClient
from app.main import app as production_app
from app.metrics import effective
from app.models import CollectSwitch, HcsAccount, HcsProject, InspectionTask, Resource, SysSetting
from app.schemas.collection_policy import CollectionPolicy
from app.schemas.monitoring import CoverageBlocker
from app.services import collect_switch, collection_policy, monitoring_overview, tenancy
from app.services.resource_guides import get_resource_guide

NOW = datetime(2026, 11, 5, 1, 2, 3, tzinfo=UTC)
BASE = "/api/v1/monitoring/coverage"
TYPES = ("ecs_server", "dcs_instance", "gaussdb_instance")


@pytest.fixture()
def monitor_api(mem_db, monkeypatch):
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"coverage-{tid}", name=f"租户{tid}", enabled=True,
            iam_endpoint="https://iam.invalid", domain_name="synthetic", secret_cipher="never-read-this"))
    mem_db.flush()
    for pid, tid in ((11, 1), (12, 1), (21, 2)):
        mem_db.add(HcsProject(id=pid, account_id=tid, project_id=f"project-{pid}", region_id="synthetic",
                              external_domain="invalid", enabled=True))
    for code in ("ledger_sync", "ecs_status", "ecs_metrics", "ecs_quota", "dcs_status", "gaussdb_status"):
        mem_db.add(InspectionTask(code=code, name=code, service=code, interval_sec=300, enabled=True))
    mem_db.commit()
    user = CurrentUser(id=991, username="coverage-reader", display_name="覆盖测试",
                       permissions=["ledger:view", "report:view"], tenant_ids=[1])

    def no_cloud(*_args, **_kwargs):
        pytest.fail("本地覆盖接口不得构造云客户端、读取环境兜底或访问网络")

    monkeypatch.setattr(HcsClient, "__init__", no_cloud)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_cloud)
    monkeypatch.setattr(tenancy, "legacy_target", no_cloud)
    monkeypatch.setattr(monitoring_overview, "utcnow", lambda: NOW)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user

    def add_resource(rtype="ecs_server", *, tid=1, pid=11, state="FRESH", age=0, health="OK", monitored=True,
                     checked=1, missing=0, **extra):
        attrs = {"inspection_monitoring": {"evaluated_at": (NOW - timedelta(seconds=age)).isoformat(),
                    "checked_rules": checked, "unavailable_rules": 1 if state == "PARTIAL" else missing,
                    "stale_after_sec": 900}, "irrelevant_private_attribute": "not-for-the-overview"}
        if state == "NO_DATA":
            attrs.pop("inspection_monitoring")
        if state == "INVALID":
            attrs["inspection_monitoring"] = {"evaluated_at": NOW.isoformat()}
        resource = Resource(tenant_id=tid, project_ref=pid, resource_type=rtype, service=rtype.split("_")[0],
            resource_id=f"synthetic-{tid}-{pid}-{len(mem_db.new)}-{mem_db.query(Resource).count()}",
            name="synthetic", health=health, monitored=monitored, attrs=attrs, last_seen=NOW, **extra)
        mem_db.add(resource)
        mem_db.commit()
        return resource

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, user=user, db=mem_db, app=app, add=add_resource)


def rows(api, **kwargs):
    response = api.client.get(BASE, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["evaluated_at"] == NOW.isoformat().replace("+00:00", "Z")
    return response.json()["data"]["rows"]


def one(api, rtype="ecs_server", **kwargs):
    return next(row for row in rows(api, **kwargs) if row["resource_type"] == rtype)


@pytest.mark.parametrize("permissions,expected", [
    (["ledger:view"], ["ecs_server", "gaussdb_instance"]),
    (["report:view"], ["dcs_instance"]), (["ledger:view", "report:view"], list(TYPES)),
])
def test_any_view_permission_only_exposes_its_authorized_types(monitor_api, permissions, expected):
    m = monitor_api
    m.user.permissions = permissions
    assert [row["resource_type"] for row in rows(m)] == expected


@pytest.mark.parametrize("permissions", [[], ["dashboard:view"], ["task:run"], ["ledger:export"]])
def test_other_permissions_cannot_enter_monitoring_coverage(monitor_api, permissions):
    monitor_api.user.permissions = permissions
    response = monitor_api.client.get(BASE)
    assert response.status_code == 403 and response.json()["code"] == 40301


@pytest.mark.parametrize("allowed,header,query", [([1], None, 2), ([1], "2", None), (None, "1", 2)])
def test_tenant_header_and_explicit_parameter_cannot_bypass_scope(monitor_api, allowed, header, query):
    m = monitor_api
    m.user.tenant_ids = allowed
    response = m.client.get(BASE, headers={"X-Tenant-Id": header} if header else {},
                            params={"tenant_id": query} if query else {})
    assert response.status_code == 403
    assert response.json()["data"]["reason"] == "tenant_scope"


def test_all_tenants_means_visible_tenants_and_requested_intersection(monitor_api):
    m = monitor_api
    assert {row["tenant_id"] for row in rows(m, headers={"X-Tenant-Id": "all"})} == {1}
    m.user.tenant_ids = None
    assert [(row["tenant_id"], row["resource_type"]) for row in rows(m)] == [
        (tid, rtype) for tid in (1, 2) for rtype in TYPES]
    assert {row["tenant_id"] for row in rows(m, params={"tenant_id": 2})} == {2}
    assert {row["tenant_id"] for row in rows(m, headers={"X-Tenant-Id": "2"})} == {2}
    assert rows(m, params={"tenant_id": 999}) == []


@pytest.mark.parametrize("empty_scope", [True, False])
def test_no_visible_tenant_never_uses_legacy_configuration_or_orphan_resources(monitor_api, empty_scope):
    m = monitor_api
    if empty_scope:
        m.user.tenant_ids = []
    else:
        m.user.tenant_ids = None
        m.db.query(HcsProject).delete()
        m.db.query(HcsAccount).delete()
        m.db.commit()
    m.add(tid=None, pid=None)
    assert rows(m) == []


def test_initialized_tenant_stays_disabled_and_read_does_not_turn_it_on(monitor_api):
    m = monitor_api
    collect_switch.initialize_disabled(m.db, 1, "synthetic")
    m.db.commit()
    before = list(m.db.execute(select(CollectSwitch.resource_type, CollectSwitch.enabled)
                               .where(CollectSwitch.tenant_id == 1)))
    for row in rows(m):
        assert row["state"] == "collection_disabled"
        assert not row["collection_enabled"] and not row["ledger_enabled"] and not row["inspection_enabled"]
    after = list(m.db.execute(select(CollectSwitch.resource_type, CollectSwitch.enabled)
                              .where(CollectSwitch.tenant_id == 1)))
    assert before == after


def test_global_switch_and_tenant_override_use_existing_load_semantics(monitor_api):
    m = monitor_api
    m.db.add(CollectSwitch(tenant_id=0, resource_type="ecs_server", enabled=False))
    m.db.add(CollectSwitch(tenant_id=1, resource_type="ecs_server", enabled=True))
    m.db.commit()
    m.user.tenant_ids = None
    ecs = {row["tenant_id"]: row for row in rows(m) if row["resource_type"] == "ecs_server"}
    assert ecs[1]["collection_enabled"] and ecs[1]["state"] == "no_resources"
    assert not ecs[2]["collection_enabled"] and ecs[2]["state"] == "collection_disabled"


def test_fresh_coverage_uses_real_inspection_evidence_and_retains_known_abnormal(monitor_api):
    m = monitor_api
    m.add()
    m.add(health="MAJOR", age=60)
    row = one(m)
    assert row["resources"] == row["eligible_resources"] == row["inspected_resources"] == 2
    assert row["abnormal_resources"] == 1 and row["unknown_resources"] == 0
    assert row["last_inspected_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert row["state"] == "covered"
    assert row["state_label"] == "巡检已覆盖" and row["blocker_labels"] == []


@pytest.mark.parametrize("source", ["NONE", "AUTO", "AOM", "PROMETHEUS"])
def test_fresh_inspection_with_no_ecs_metric_source_does_not_promise_metric_coverage(monitor_api, source):
    m = monitor_api
    resource = m.add()
    resource.metrics = {}
    tenant = m.db.get(HcsAccount, 1)
    tenant.metrics_source, tenant.prometheus_url = source, None
    m.db.commit()
    assert effective(tenant) == "NONE"
    row = one(m)
    assert row["state"] == "covered" and row["state_label"] == "巡检已覆盖"
    assert row["inspected_resources"] == row["resources"] == 1
    assert row["metrics_enabled"] is True
    assert all(set(metric) == {"key", "label", "unit", "availability"} for metric in row["metrics"])


def test_disabling_only_ecs_metric_task_does_not_relabel_valid_inspection_as_partial(monitor_api):
    m = monitor_api
    m.add()
    m.db.get(InspectionTask, "ecs_metrics").enabled = False
    m.db.commit()
    row = one(m)
    assert row["state"] == "covered" and row["state_label"] == "巡检已覆盖"
    assert row["inspected_resources"] == 1 and row["inspection_enabled"] is True
    assert row["metrics_enabled"] is False
    assert row["blockers"] == ["metrics_task_disabled"]
    assert row["blocker_labels"] == ["指标采集任务未启用"]


@pytest.mark.parametrize("state,age,checked,missing", [
    ("NO_DATA", 0, 1, 0), ("INVALID", 0, 1, 0), ("FRESH", 901, 1, 0),
    ("FRESH", -31, 1, 0), ("FRESH", 0, 0, 0), ("FRESH", 0, True, 0), ("FRESH", 0, 1, -1),
])
def test_unknown_or_stale_evidence_cannot_be_certified_by_ledger_time(monitor_api, state, age, checked, missing):
    m = monitor_api
    m.add(state=state, age=age, checked=checked, missing=missing)
    row = one(m)
    assert row["inspected_resources"] == row["abnormal_resources"] == 0
    assert row["unknown_resources"] == 1
    assert row["state"] == ("stale" if age == 901 else "not_inspected")
    assert row["stale_resources"] == int(age == 901)
    if age == 901:
        assert row["last_inspected_at"] == (NOW - timedelta(seconds=901)).isoformat().replace("+00:00", "Z")
    else:
        assert row["last_inspected_at"] is None


def test_partial_and_stale_resources_are_visible_without_losing_existing_alarm(monitor_api):
    m = monitor_api
    m.add()
    m.add(state="PARTIAL")
    m.add(age=901, health="CRITICAL")
    m.add(state="NO_DATA")
    row = one(m)
    assert row["resources"] == 4 and row["inspected_resources"] == 1
    assert row["unknown_resources"] == 2 and row["abnormal_resources"] == 1
    assert row["partial_resources"] == row["stale_resources"] == 1 and row["state"] == "partial"


@pytest.mark.parametrize("kind", TYPES)
def test_multiple_projects_are_aggregated_without_certifying_disabled_or_foreign_project(kind, monitor_api):
    m = monitor_api
    m.add(kind)
    m.add(kind, pid=12)
    m.add(kind, pid=21)
    m.add(kind, pid=None)
    m.add(kind, lifecycle="DELETED")
    m.db.get(HcsProject, 12).enabled = False
    m.db.commit()
    row = one(m, kind)
    assert row["resources"] == 4 and row["eligible_resources"] == row["inspected_resources"] == 1
    assert row["unknown_resources"] == 3
    assert row["projects"] == 2 and row["enabled_projects"] == 1 and row["state"] == "partial"


@pytest.mark.parametrize("gate,expected,blocker", [
    ("tenant", "tenant_disabled", "tenant_disabled"), ("project", "projects_disabled", "projects_disabled"),
    ("no_project", "no_projects", "no_projects"),
    ("task", "inspection_disabled", "inspection_task_disabled"),
    ("missing_task", "inspection_disabled", "inspection_task_disabled"),
    ("ledger", "collection_disabled", "ledger_task_disabled"),
])
def test_disabled_tenant_project_or_task_cannot_claim_covered(monitor_api, gate, expected, blocker):
    m = monitor_api
    m.add()
    if gate == "tenant":
        m.db.get(HcsAccount, 1).enabled = False
    elif gate == "project":
        for pid in (11, 12):
            m.db.get(HcsProject, pid).enabled = False
    elif gate == "no_project":
        m.db.query(HcsProject).filter(HcsProject.account_id == 1).delete()
    elif gate in ("task", "ledger"):
        m.db.get(InspectionTask, "ecs_status" if gate == "task" else "ledger_sync").enabled = False
    else:
        m.db.delete(m.db.get(InspectionTask, "ecs_status"))
    m.db.commit()
    row = one(m)
    assert row["state"] == expected and blocker in row["blockers"]
    if gate != "ledger":
        assert not row["inspection_enabled"] and row["inspected_resources"] == 0 and row["unknown_resources"] == 1


@pytest.mark.parametrize("phase,expected,flag", [
    ("inventory", "collection_disabled", "ledger_enabled"),
    ("inspection", "inspection_disabled", "inspection_enabled"), ("metrics", "covered", "metrics_enabled"),
])
def test_ecs_existing_phase_policy_is_honored(monitor_api, phase, expected, flag):
    m = monitor_api
    m.add()
    policy = CollectionPolicy()
    getattr(policy, phase).enabled = False
    m.db.add(SysSetting(key=collection_policy.policy_key(1), value=policy.model_dump(mode="json")))
    m.db.commit()
    row = one(m)
    assert row["state"] == expected and not row[flag]
    assert ("ledger_phase_disabled" if phase == "inventory" else f"{phase}_phase_disabled") in row["blockers"]
    expected_label = {"inventory": "台账同步阶段已关闭", "inspection": "巡检阶段已关闭", "metrics": "指标采集阶段已关闭"}
    assert row["blocker_labels"] == [expected_label[phase]]


@pytest.mark.parametrize("selection", ["critical", "selected"])
def test_ecs_excluded_and_unmonitored_resources_are_not_currently_covered(monitor_api, selection):
    m = monitor_api
    included = m.add(critical=True)
    m.add(critical=False)
    m.add(critical=True, monitored=False)
    policy = CollectionPolicy.model_validate({"selection": {"mode": selection, "resource_ids": [included.id]}})
    m.db.add(SysSetting(key=collection_policy.policy_key(1), value=policy.model_dump(mode="json")))
    m.db.commit()
    row = one(m)
    assert row["eligible_resources"] == row["inspected_resources"] == 1
    assert row["unknown_resources"] == 2 and row["state"] == "partial"


@pytest.mark.parametrize("raw", [{"metrics": {"enabled": "false"}}, {"selection": {"mode": "broken"}}, []])
def test_invalid_ecs_policy_is_explicit_and_does_not_certify_old_evidence(monitor_api, raw):
    m = monitor_api
    m.add()
    m.db.add(SysSetting(key=collection_policy.policy_key(1), value=raw))
    m.db.commit()
    row = one(m)
    assert row["state"] == "configuration_invalid" and "invalid_policy" in row["blockers"]
    assert row["inspected_resources"] == 0 and row["unknown_resources"] == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_invalid_legacy_cron_and_independent_ecs_schedule_follow_existing_policy(monitor_api, explicit):
    m = monitor_api
    m.add()
    m.db.get(InspectionTask, "ecs_status").cron = "not-a-cron"
    if explicit:
        m.db.add(SysSetting(key=collection_policy.policy_key(1), value=CollectionPolicy().model_dump(mode="json")))
    m.db.commit()
    row = one(m)
    assert row["state"] == ("covered" if explicit else "configuration_invalid")
    assert row["inspection_enabled"] is explicit


def test_metric_definitions_and_source_description_have_only_one_guide_source(monitor_api):
    m = monitor_api
    for row in rows(m):
        guide = get_resource_guide(row["resource_type"])
        assert row["resource_name"] == guide.name and row["source_label"] == guide.summary
        assert row["metrics"] == [{"key": metric.key, "label": metric.label, "unit": metric.unit,
                                  "availability": metric.availability} for metric in guide.metrics]
        assert row["state"] == "no_resources" and row["state_label"] == "无资源"
        assert row["last_inspected_at"] is None
    gauss = one(m, "gaussdb_instance")
    assert {metric["key"] for metric in gauss["metrics"] if metric["availability"] == "not_collected"} == {
        "cpu_usage", "mem_usage"}


def test_blocker_dictionary_defines_a_readable_label_for_every_contract_value():
    assert set(monitoring_overview.BLOCKER_LABELS) == set(get_args(CoverageBlocker))
    assert all(label.strip() for label in monitoring_overview.BLOCKER_LABELS.values())


def test_multiple_blocker_labels_are_returned_in_exact_code_order(monitor_api):
    m = monitor_api
    m.db.get(HcsAccount, 1).enabled = False
    m.db.add(CollectSwitch(tenant_id=1, resource_type="ecs_server", enabled=False))
    for pid in (11, 12):
        m.db.get(HcsProject, pid).enabled = False
    for task in ("ledger_sync", "ecs_status", "ecs_metrics"):
        m.db.get(InspectionTask, task).enabled = False
    policy = CollectionPolicy.model_validate({
        "inventory": {"enabled": False}, "inspection": {"enabled": False}, "metrics": {"enabled": False}})
    m.db.add(SysSetting(key=collection_policy.policy_key(1), value=policy.model_dump(mode="json")))
    m.db.commit()
    row = one(m)
    assert row["blockers"] == [
        "tenant_disabled", "collection_disabled", "projects_disabled", "ledger_task_disabled",
        "inspection_task_disabled", "metrics_task_disabled", "ledger_phase_disabled", "inspection_phase_disabled",
        "metrics_phase_disabled"]
    assert row["blocker_labels"] == [
        "租户已停用", "该资源类型的采集开关已关闭", "全部项目已停用", "台账同步任务未启用",
        "巡检任务未启用", "指标采集任务未启用", "台账同步阶段已关闭", "巡检阶段已关闭", "指标采集阶段已关闭"]


@pytest.mark.parametrize("reason,expected", [
    ("no_projects", "未配置项目"), ("invalid_policy", "采集策略配置无效"), ("invalid_schedule", "任务调度配置无效"),
])
def test_configuration_blockers_explain_project_policy_and_schedule_failures(monitor_api, reason, expected):
    m = monitor_api
    if reason == "no_projects":
        m.db.query(HcsProject).filter(HcsProject.account_id == 1).delete()
    elif reason == "invalid_policy":
        m.db.add(SysSetting(key=collection_policy.policy_key(1), value={"selection": {"mode": "broken"}}))
    else:
        m.db.get(InspectionTask, "ecs_status").cron = "not-a-cron"
    m.db.commit()
    row = one(m)
    assert row["blockers"] == [reason]
    assert row["blocker_labels"] == [expected]


def test_coverage_has_fixed_batch_query_count_and_never_loads_credentials_or_writes(monitor_api):
    m = monitor_api
    m.user.tenant_ids = None
    for tid in range(3, 23):
        m.db.add(HcsAccount(id=tid, code=f"coverage-{tid}", name=f"租户{tid}",
            iam_endpoint="https://iam.invalid", domain_name="synthetic"))
    m.db.commit()
    m.add()
    statements = []

    def read_only(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement
        assert "secret_cipher" not in statement and "iam_endpoint" not in statement
        assert "prometheus_url" not in statement and "access_key" not in statement
        assert "hcs_account.endpoints" not in statement and "resource.metrics" not in statement
        assert "inspection_run" not in statement

    event.listen(m.db.get_bind(), "before_cursor_execute", read_only)
    try:
        response = rows(m)
        assert len(response) == 22 * 3
        assert len(statements) == 6
        statements.clear()
        assert len(rows(m, params={"tenant_id": 1})) == 3
        assert len(statements) == 6
        assert not m.db.new and not m.db.dirty and not m.db.deleted
    finally:
        event.remove(m.db.get_bind(), "before_cursor_execute", read_only)


def test_registered_endpoint_is_get_only(monitor_api):
    assert set(production_app.openapi()["paths"][BASE]) == {"get"}
    assert monitor_api.client.post(BASE).status_code == 405


@pytest.mark.parametrize("tenant", ["0", "-1", "wrong"])
def test_tenant_query_requires_a_positive_local_id(monitor_api, tenant):
    response = monitor_api.client.get(BASE, params={"tenant_id": tenant})
    assert response.status_code == 400 and response.json()["code"] == 40001


def test_expired_inspection_is_not_labelled_as_never_inspected(monitor_api):
    m = monitor_api
    m.add(age=901)
    row = one(m)
    assert row["state"] == "stale" and row["state_label"] == "巡检已过期"
    assert row["last_inspected_at"] is not None and row["stale_resources"] == 1
    assert row["inspected_resources"] == 0 and row["eligible_resources"] == 1


@pytest.mark.parametrize("kind", ["unmonitored", "selection", "unlinked"])
def test_resources_outside_current_scope_are_explicit_not_never_inspected(monitor_api, kind):
    m = monitor_api
    m.add(monitored=kind != "unmonitored", pid=None if kind == "unlinked" else 11)
    if kind == "selection":
        m.db.add(SysSetting(key=collection_policy.policy_key(1),
                           value={"selection": {"mode": "selected", "resource_ids": []}}))
        m.db.commit()
    row = one(m)
    assert row["resources"] == 1 and row["eligible_resources"] == row["inspected_resources"] == 0
    assert row["state"] == "no_eligible_resources"
    assert row["blockers"][-1] == "no_eligible_resources"
    assert row["blocker_labels"][-1] == monitoring_overview.BLOCKER_LABELS["no_eligible_resources"]


def test_stale_excluded_resource_does_not_relabel_never_inspected_included_resource(monitor_api):
    m = monitor_api
    m.add(age=901, pid=12)
    m.add(state="NO_DATA", pid=11)
    project = m.db.get(HcsProject, 12)
    assert project is not None
    project.enabled = False
    m.db.commit()
    row = one(m)
    assert row["stale_resources"] == 1 and row["eligible_resources"] == 1
    assert row["state"] == "not_inspected"
