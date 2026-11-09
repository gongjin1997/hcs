"""拆分 Gauss 巡检的证据边界；内存库、合成观测，不调用云或开发数据库。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from itertools import permutations
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.collectors.base import Observation, RunContext
from app.models import AlarmEvent, AlarmRule, EvalState, InspectionTask, Resource
from app.rules.loader import rules_by_target
from app.services import inspection_evidence as evidence
from app.services import pipeline
from app.services.inspection_health import current_health, inspection_state
from app.services.tenancy import Target

NOW = datetime(2026, 11, 9, 3, tzinfo=UTC)
TASK_CHECKS = {"gaussdb_status": "instance_status", "gaussdb_storage": "disk", "gaussdb_components": "components"}
TASKS = list(TASK_CHECKS)
KIND = "gaussdb_instance"


def context(task="gaussdb_status", *, tenant=1, project=11, required=None, ttl=900):
    return RunContext(client=SimpleNamespace(), account=f"tenant-{tenant}", project_id=f"project-{project}",
                      tenant_id=tenant, project_ref=project, params={"_task_code": task,
                      "_inspection_stale_after_sec": ttl, "_inspection_tasks": TASKS if required is None else required})


def observation(task="gaussdb_status", *, bad=False, rid="shared"):
    check = TASK_CHECKS[task]
    facts = {"instance_status": {"status": "ERROR" if bad else "ACTIVE"},
             "disk": {"disk_usage": 95 if bad else 25},
             "components": {"down_components": int(bad), "unknown_components": 0}}[check]
    return Observation(KIND, rid, rid, check, facts)


@pytest.fixture
def gauss_rules(mem_db):
    rows = [AlarmRule(id="gauss-status", name="status", resource_type=KIND, check_item="instance_status", kind="state",
                     expr={"when": "status == 'ERROR'", "severity": "P2"}, for_count=2,
                     recover={"when": "status == 'ACTIVE'", "for_count": 2}),
            AlarmRule(id="gauss-disk", name="disk", resource_type=KIND, check_item="disk", kind="threshold",
                     expr={"value": "disk_usage", "levels": [{"severity": "P2", "threshold": 90}]}, for_count=2,
                     recover={"below": 80, "for_count": 2}),
            AlarmRule(id="gauss-components", name="components", resource_type=KIND, check_item="components", kind="state",
                     expr={"when": "down_components > 0", "severity": "P2"}, for_count=2,
                     recover={"when": "down_components == 0 and unknown_components == 0", "for_count": 2})]
    mem_db.add_all(rows)
    mem_db.commit()
    return rows


def run(db, task="gaussdb_status", *, at=NOW, bad=False, ctx=None, rid="shared"):
    ctx = ctx or context(task)
    pipeline.process_observations(db, "gaussdb", [observation(task, bad=bad, rid=rid)], ctx, at)
    return db.scalar(select(Resource).where(Resource.tenant_id == ctx.tenant_id,
        Resource.project_ref == ctx.project_ref, Resource.resource_id == rid))


def fail(db, ctx, at=NOW):
    states, resources, counts = evidence.interrupt_missing(db, ctx, rules_by_target(db, {KIND}), [], at)
    evidence.mark_missing_resources(resources, counts, ctx)
    pipeline.refresh_health(db, list(resources.values()), at)
    db.commit()
    return states, resources, counts


def state(db, task, ctx=None, rid="shared"):
    ctx = ctx or context(task)
    rule = {"instance_status": "gauss-status", "disk": "gauss-disk", "components": "gauss-components"}[TASK_CHECKS[task]]
    fp = fingerprint(ctx.account, ctx.project_id, KIND, rid, rule, "")
    return db.get(EvalState, fp)


def configure(db, required=TASKS):
    for code in TASKS:
        db.add(InspectionTask(code=code, name=code, service="gaussdb", interval_sec=120, enabled=code in required))
    db.commit()


@pytest.mark.parametrize("order", list(permutations(TASKS)))
def test_success_requires_each_enabled_task_in_any_completion_order(mem_db, gauss_rules, order):
    for index, task in enumerate(order):
        row = run(mem_db, task, at=NOW + timedelta(seconds=index))
        assert inspection_state(row, NOW + timedelta(seconds=index)) == ("FRESH" if index == 2 else "PARTIAL")
    raw = row.attrs["inspection_monitoring"]
    assert set(raw["tasks"]) == set(TASKS)
    assert raw["evaluated_at"] == NOW.isoformat()
    assert raw["checked_rules"] == 3 and raw["unavailable_rules"] == 0
    for index, task in enumerate(order):
        assert raw["tasks"][task]["evaluated_at"] == (NOW + timedelta(seconds=index)).isoformat()


def test_interleaved_hits_and_recovery_only_change_their_own_check(mem_db, gauss_rules):
    run(mem_db, "gaussdb_storage", bad=True)
    disk_before = deepcopy(state(mem_db, "gaussdb_storage").data)
    run(mem_db, bad=True, at=NOW + timedelta(seconds=1))
    run(mem_db, "gaussdb_components", bad=True, at=NOW + timedelta(seconds=2))
    assert state(mem_db, "gaussdb_storage").data == disk_before
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=3))
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.check_item == "disk"))
    assert alarm.status == "FIRING"
    run(mem_db, "gaussdb_storage", at=NOW + timedelta(seconds=4))
    recovery = deepcopy(state(mem_db, "gaussdb_storage").data)
    assert recovery["ok_hits"] == 1
    component_before = deepcopy(state(mem_db, "gaussdb_components").data)
    fail(mem_db, context(), NOW + timedelta(seconds=5))
    assert state(mem_db, "gaussdb_storage").data == recovery
    assert state(mem_db, "gaussdb_components").data == component_before
    assert alarm.status == "FIRING"
    run(mem_db, "gaussdb_storage", at=NOW + timedelta(seconds=6))
    assert alarm.status == "RESOLVED"


@pytest.mark.parametrize("task", ["gaussdb_storage", "gaussdb_components"])
@pytest.mark.parametrize("disabled_rule", [False, True])
def test_status_never_interrupts_legacy_owned_other_checks(mem_db, gauss_rules, task, disabled_rule):
    row = run(mem_db)
    rule = gauss_rules[1 if task == "gaussdb_storage" else 2]
    old_ctx = context()
    fp = fingerprint(old_ctx.account, old_ctx.project_id, KIND, row.resource_id, rule.id, "")
    data = {"hits": 7, "ok_hits": 1, "_last_evaluated_at": NOW.isoformat(), evidence.SCOPE: evidence.scope_key(old_ctx),
            evidence.META: {"version": 1, "resource_ref": row.id, "resource_type": KIND, "resource_id": row.resource_id,
                            "rule_id": rule.id, "check_item": rule.check_item, "sub_object": "",
                            "task_code": "gaussdb_status", "state": None, "execution_id": None, "since": NOW.isoformat()}}
    mem_db.add(EvalState(fingerprint=fp, data=deepcopy(data)))
    alarm = AlarmEvent(tenant_id=1, resource_ref=row.id, resource_type=KIND, resource_name=row.name, service="gaussdb",
                       fingerprint=fp, rule_id=rule.id, severity="P2", status="FIRING", title="synthetic",
                       check_item=rule.check_item, first_seen=NOW, last_seen=NOW)
    mem_db.add(alarm)
    rule.enabled = not disabled_rule
    mem_db.commit()
    alarm_before = (alarm.status, alarm.last_seen, alarm.count, deepcopy(alarm.detail))
    run(mem_db, at=NOW + timedelta(seconds=10))
    fail(mem_db, old_ctx, NOW + timedelta(seconds=20))
    assert mem_db.get(EvalState, fp).data == data
    assert (alarm.status, alarm.last_seen, alarm.count, alarm.detail) == alarm_before
    assert row.health == "MAJOR"
    if not disabled_rule:
        # 只有新归属的真实观测能接管；恢复确认从 1 重新累计。
        run(mem_db, task, at=NOW + timedelta(seconds=30))
        adopted = mem_db.get(EvalState, fp).data
        assert adopted[evidence.SCOPE] == evidence.scope_key(context(task))
        assert adopted[evidence.META]["task_code"] == task
        assert adopted["ok_hits"] == 1 and adopted["hits"] == 0
        assert alarm.status == "FIRING"
        run(mem_db, at=NOW + timedelta(seconds=40))
        assert mem_db.get(EvalState, fp).data == adopted
        run(mem_db, task, at=NOW + timedelta(seconds=50))
        assert alarm.status == "RESOLVED"


def test_failure_keeps_task_time_and_later_status_cannot_mask_it(mem_db, gauss_rules):
    for task in TASKS:
        row = run(mem_db, task)
    fail(mem_db, context("gaussdb_storage"), NOW + timedelta(minutes=1))
    row = run(mem_db, at=NOW + timedelta(minutes=2))
    tasks = row.attrs["inspection_monitoring"]["tasks"]
    assert tasks["gaussdb_storage"]["evaluated_at"] == NOW.isoformat()
    assert tasks["gaussdb_storage"]["checked_rules"] == 0
    assert tasks["gaussdb_storage"]["unavailable_rules"] == 1
    assert tasks["gaussdb_status"]["evaluated_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert inspection_state(row, NOW + timedelta(minutes=2)) == "PARTIAL"
    assert current_health(row, NOW + timedelta(minutes=2)) == "UNKNOWN"
    row = run(mem_db, "gaussdb_storage", at=NOW + timedelta(minutes=3))
    assert inspection_state(row, NOW + timedelta(minutes=3)) == "FRESH"


def test_never_observed_failure_cannot_borrow_other_task_timestamp(mem_db, gauss_rules):
    row = run(mem_db)
    run(mem_db, "gaussdb_components")
    fail(mem_db, context("gaussdb_storage"), NOW + timedelta(minutes=1))
    run(mem_db, at=NOW + timedelta(minutes=2))
    raw = row.attrs["inspection_monitoring"]
    assert raw["tasks"]["gaussdb_storage"]["evaluated_at"] is None
    assert raw["evaluated_at"] is None
    assert inspection_state(row, NOW + timedelta(minutes=2)) == "PARTIAL"


def test_new_task_failure_cannot_adopt_or_duplicate_legacy_status_state(mem_db, gauss_rules):
    row = run(mem_db, "gaussdb_storage", bad=True)
    stored = state(mem_db, "gaussdb_storage")
    data = deepcopy(stored.data)
    data[evidence.SCOPE] = evidence.scope_key(context())
    data[evidence.META]["task_code"] = "gaussdb_status"
    stored.data = deepcopy(data)
    mem_db.commit()
    fail(mem_db, context("gaussdb_storage"), NOW + timedelta(seconds=1))
    assert stored.data == data
    assert len(list(mem_db.scalars(select(EvalState)))) == 1
    assert row.attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"]["unavailable_rules"] == 1
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=2))
    assert stored.data["hits"] == 1
    assert stored.data[evidence.META]["task_code"] == "gaussdb_storage"


def test_legacy_flat_result_is_not_copied_to_new_task_or_refreshed_on_failure(mem_db, gauss_rules):
    row = run(mem_db)
    old = NOW - timedelta(hours=1)
    row.attrs = {"unrelated": {"value": 7}, "inspection_monitoring": {
        "task_code": "gaussdb_status", "evaluated_at": old.isoformat(), "checked_rules": 3,
        "unavailable_rules": 0, "stale_after_sec": 900}}
    mem_db.commit()
    fail(mem_db, context("gaussdb_storage"), NOW)
    raw = row.attrs["inspection_monitoring"]
    assert raw["tasks"]["gaussdb_status"]["evaluated_at"] == old.isoformat()
    assert raw["tasks"]["gaussdb_storage"]["evaluated_at"] is None
    assert "gaussdb_components" not in raw["tasks"]
    assert raw["evaluated_at"] is None and row.attrs["unrelated"] == {"value": 7}


def test_each_task_uses_its_own_ttl_instead_of_aggregate_minimum(mem_db, gauss_rules):
    row = run(mem_db, "gaussdb_storage", ctx=context("gaussdb_storage", ttl=3600))
    for task in ("gaussdb_status", "gaussdb_components"):
        run(mem_db, task, ctx=context(task, ttl=60), at=NOW + timedelta(minutes=2))
    # 总摘要的最老时间和最短 TTL 不可组合成某个任务的假过期。
    assert inspection_state(row, NOW + timedelta(minutes=2)) == "FRESH"
    run(mem_db, at=NOW + timedelta(hours=2))
    assert inspection_state(row, NOW + timedelta(hours=2)) == "STALE"
    assert row.attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"]["evaluated_at"] == NOW.isoformat()


@pytest.mark.parametrize("key,value", [("evaluated_at", None), ("evaluated_at", "2026-11-09T03:00:31+00:00"),
    ("evaluated_at", "2026-11-09T03:00:00"), ("checked_rules", True), ("checked_rules", 0),
    ("unavailable_rules", -1), ("unavailable_rules", False), ("stale_after_sec", 59),
    ("task_code", "gaussdb_status")])
def test_each_enabled_task_requires_valid_timestamp_counts_and_ownership(mem_db, gauss_rules, key, value):
    for task in TASKS:
        row = run(mem_db, task)
    attrs = deepcopy(row.attrs)
    attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"][key] = value
    row.attrs = deepcopy(attrs)
    assert inspection_state(row, NOW) == "PARTIAL" and current_health(row, NOW) == "UNKNOWN"
    assert row.attrs == attrs  # 读取不会修补或续期旧结果。


@pytest.mark.parametrize("peer_tenant,peer_project", [(1, 12), (2, 21)])
def test_failure_and_success_do_not_touch_peer_tenant_or_project(mem_db, gauss_rules, peer_tenant, peer_project):
    for ctx in (context(), context(tenant=peer_tenant, project=peer_project)):
        for task in TASKS:
            ctx.params["_task_code"] = task
            row = run(mem_db, task, ctx=ctx, bad=True)
    peer = row
    attrs = deepcopy(peer.attrs)
    snapshots = {item.fingerprint: deepcopy(item.data) for item in mem_db.scalars(select(EvalState))
                 if item.data[evidence.META]["resource_ref"] == peer.id}
    fail(mem_db, context("gaussdb_storage"), NOW + timedelta(minutes=1))
    run(mem_db, at=NOW + timedelta(minutes=2))
    assert peer.attrs == attrs
    for fp, data in snapshots.items():
        assert mem_db.get(EvalState, fp).data == data


def test_legacy_null_project_isolated_by_external_scope(mem_db, gauss_rules):
    own, peer = context("gaussdb_storage"), context("gaussdb_storage", project=12)
    own.project_ref = peer.project_ref = None
    run(mem_db, "gaussdb_storage", ctx=own, rid="own", bad=True)
    row = run(mem_db, "gaussdb_storage", ctx=peer, rid="peer", bad=True)
    attrs = deepcopy(row.attrs)
    data = deepcopy(state(mem_db, "gaussdb_storage", peer, "peer").data)
    fail(mem_db, own, NOW + timedelta(minutes=1))
    assert state(mem_db, "gaussdb_storage", own, "own").data["hits"] == 0
    assert state(mem_db, "gaussdb_storage", peer, "peer").data == data
    assert row.attrs == attrs


@pytest.mark.parametrize("tenant,project", [(2, 11), (1, 12)])
def test_record_rejects_mismatched_resource_scope(mem_db, gauss_rules, tenant, project):
    row = run(mem_db)
    attrs = deepcopy(row.attrs)
    with pytest.raises(ValueError, match="租户/项目"):
        evidence.record_inspection(row, context(tenant=tenant, project=project), 1, 0, NOW)
    assert row.attrs == attrs


def test_disabled_task_cannot_interrupt_active_evidence(mem_db, gauss_rules):
    row = run(mem_db, "gaussdb_storage", bad=True)
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=1))
    data = deepcopy(state(mem_db, "gaussdb_storage").data)
    attrs = deepcopy(row.attrs)
    fail(mem_db, context("gaussdb_storage", required=["gaussdb_status"]), NOW + timedelta(seconds=2))
    assert state(mem_db, "gaussdb_storage").data == data and row.attrs == attrs
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"


def test_failure_rechecks_task_disabled_after_context_was_created(mem_db, gauss_rules):
    configure(mem_db)
    row = run(mem_db, "gaussdb_storage", bad=True)
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=1))
    stale_ctx = context("gaussdb_storage")
    before = deepcopy(state(mem_db, "gaussdb_storage").data)
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = False
    evidence.sync_gauss_task_requirements(mem_db)
    mem_db.commit()
    attrs = deepcopy(row.attrs)
    states, missing, counts = fail(mem_db, stale_ctx, NOW + timedelta(seconds=2))
    assert states == missing == counts == {}
    assert stale_ctx.params["_inspection_tasks"] == ["gaussdb_components", "gaussdb_status"]
    assert row.attrs == attrs and state(mem_db, "gaussdb_storage").data == before
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"


def test_failure_rechecks_peer_task_enabled_after_context_was_created(mem_db, gauss_rules):
    configure(mem_db, ["gaussdb_status"])
    row = run(mem_db)
    stale_ctx = context(required=["gaussdb_status"])
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = True
    evidence.sync_gauss_task_requirements(mem_db)
    mem_db.commit()
    fail(mem_db, stale_ctx, NOW + timedelta(seconds=1))
    assert row.attrs["inspection_monitoring"]["required_tasks"] == ["gaussdb_status", "gaussdb_storage"]
    assert "gaussdb_storage" not in row.attrs["inspection_monitoring"]["tasks"]


def test_global_task_toggles_sync_only_requirements_without_sample_or_alarm_changes(mem_db, gauss_rules):
    configure(mem_db, ["gaussdb_status"])
    rows = [run(mem_db, ctx=context(required=["gaussdb_status"], tenant=tid, project=pid))
            for tid, pid in ((1, 11), (1, 12), (2, 21))]
    dcs = Resource(service="dcs", resource_type="dcs_instance", resource_id="dcs", name="dcs",
                   attrs={"inspection_monitoring": {"task_code": "dcs_status", "evaluated_at": NOW.isoformat(),
                       "checked_rules": 1, "unavailable_rules": 0, "stale_after_sec": 900}})
    mem_db.add(dcs)
    mem_db.commit()
    dcs_attrs = deepcopy(dcs.attrs)
    before = [deepcopy(row.attrs["inspection_monitoring"]["tasks"]) for row in rows]
    health = [row.health for row in rows]
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = True
    assert evidence.sync_gauss_task_requirements(mem_db) == 3
    for row, tasks, persisted_health in zip(rows, before, health, strict=True):
        assert row.attrs["inspection_monitoring"]["tasks"] == tasks
        assert inspection_state(row, NOW) == "PARTIAL" and current_health(row, NOW) == "UNKNOWN"
        assert row.health == persisted_health
    assert dcs.attrs == dcs_attrs
    assert evidence.sync_gauss_task_requirements(mem_db) == 0
    # 停用尚无证据的容量任务应立即解除“覆盖不全”，不用等下一轮状态检查。
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = False
    evidence.sync_gauss_task_requirements(mem_db)
    assert all(inspection_state(row, NOW) == "FRESH" for row in rows)
    # 所有任务停用不构造 FRESH；仍不修改已有异常健康。
    rows[0].health = "MAJOR"
    mem_db.get(InspectionTask, "gaussdb_status").enabled = False
    evidence.sync_gauss_task_requirements(mem_db)
    assert all(inspection_state(row, NOW) == "DISABLED" for row in rows)
    assert current_health(rows[0], NOW) == "MAJOR"
    assert rows[0].attrs["inspection_monitoring"]["tasks"] == before[0]


def test_disabling_failed_task_keeps_its_alarm_and_reenabling_uses_old_evidence(mem_db, gauss_rules):
    configure(mem_db)
    for task in TASKS:
        row = run(mem_db, task, bad=task == "gaussdb_storage")
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=1))
    fail(mem_db, context("gaussdb_storage"), NOW + timedelta(seconds=2))
    alarm = mem_db.scalar(select(AlarmEvent))
    before = deepcopy(row.attrs["inspection_monitoring"]["tasks"])
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = False
    evidence.sync_gauss_task_requirements(mem_db)
    assert inspection_state(row, NOW + timedelta(seconds=2)) == "FRESH"
    assert current_health(row, NOW + timedelta(seconds=2)) == "MAJOR"
    assert alarm.status == "FIRING"
    assert row.attrs["inspection_monitoring"]["tasks"] == before
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = True
    evidence.sync_gauss_task_requirements(mem_db)
    assert inspection_state(row, NOW + timedelta(seconds=2)) == "PARTIAL"
    assert row.attrs["inspection_monitoring"]["tasks"] == before
    assert alarm.status == "FIRING"


def test_failure_refreshes_identity_map_before_merging_other_task_result(mem_db, gauss_rules):
    row = run(mem_db)
    with Session(mem_db.get_bind(), expire_on_commit=False) as peer:
        copy = peer.get(Resource, row.id)
        evidence.record_inspection(copy, context("gaussdb_storage"), 1, 0, NOW + timedelta(seconds=1))
        peer.commit()
        disk = deepcopy(copy.attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"])
    assert "gaussdb_storage" not in row.attrs["inspection_monitoring"]["tasks"]
    fail(mem_db, context(), NOW + timedelta(seconds=2))
    assert row.attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"] == disk


def test_pipeline_failure_context_uses_enabled_tasks_without_freshening_time(mem_db, gauss_rules):
    configure(mem_db)
    for task in TASKS:
        row = run(mem_db, task)
    target = Target(tenant_id=1, code="tenant-1", name="one", short_name="one", project_ref=11,
                    project_id="project-11", project_name="synthetic", region="synthetic", workspaces=[])
    pipeline._interrupt_task(mem_db, target, "gaussdb_storage", NOW + timedelta(minutes=1))
    assert row.attrs["inspection_monitoring"]["required_tasks"] == sorted(TASKS)
    assert row.attrs["inspection_monitoring"]["tasks"]["gaussdb_storage"]["evaluated_at"] == NOW.isoformat()
    assert inspection_state(row, NOW + timedelta(minutes=1)) == "PARTIAL"


def test_process_refresh_after_resource_lock_does_not_restore_obsolete_requirements(mem_db, gauss_rules, monkeypatch):
    configure(mem_db, ["gaussdb_status"])
    row = run(mem_db, ctx=context(required=["gaussdb_status"]))
    stale_ctx = context(required=["gaussdb_status"])
    original = pipeline._inspection_task_scope
    refreshed = []

    def enable_storage_on_refresh(db, ctx):
        # 模拟云请求结束、取得资源锁时，管理员已启用另一个任务。
        db.get(InspectionTask, "gaussdb_storage").enabled = True
        evidence.sync_gauss_task_requirements(db)
        original(db, ctx)
        refreshed.append(list(ctx.params["_inspection_tasks"]))

    monkeypatch.setattr(pipeline, "_inspection_task_scope", enable_storage_on_refresh)
    run(mem_db, ctx=stale_ctx, at=NOW + timedelta(seconds=1))
    assert refreshed == [["gaussdb_status", "gaussdb_storage"]]
    assert row.attrs["inspection_monitoring"]["required_tasks"] == ["gaussdb_status", "gaussdb_storage"]
    assert "gaussdb_storage" not in row.attrs["inspection_monitoring"]["tasks"]
    assert inspection_state(row, NOW + timedelta(seconds=1)) == "PARTIAL"


def test_process_discards_inflight_observation_if_its_task_was_disabled(mem_db, gauss_rules, monkeypatch):
    configure(mem_db)
    row = run(mem_db, "gaussdb_storage", bad=True)
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=1))
    run(mem_db, "gaussdb_storage", at=NOW + timedelta(seconds=2))
    before = deepcopy(state(mem_db, "gaussdb_storage").data)
    tasks = deepcopy(row.attrs["inspection_monitoring"]["tasks"])
    original = pipeline._inspection_task_scope

    def disable_storage_on_refresh(db, ctx):
        db.get(InspectionTask, "gaussdb_storage").enabled = False
        evidence.sync_gauss_task_requirements(db)
        original(db, ctx)

    monkeypatch.setattr(pipeline, "_inspection_task_scope", disable_storage_on_refresh)
    with pytest.raises(pipeline._CollectionPolicyChanged):
        run(mem_db, "gaussdb_storage", at=NOW + timedelta(seconds=3))
    assert state(mem_db, "gaussdb_storage").data == before
    assert row.attrs["inspection_monitoring"]["tasks"] == tasks
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"


def test_disabled_run_task_skips_before_cloud_and_does_not_clear_evidence(mem_db, gauss_rules, monkeypatch):
    from app.collectors.registry import TASKS as collectors

    configure(mem_db)
    row = run(mem_db, "gaussdb_storage", bad=True)
    before = deepcopy(state(mem_db, "gaussdb_storage").data)
    mem_db.get(InspectionTask, "gaussdb_storage").enabled = False
    evidence.sync_gauss_task_requirements(mem_db)
    mem_db.commit()
    attrs = deepcopy(row.attrs)
    collector = SimpleNamespace(service="gaussdb", collect=Mock(side_effect=AssertionError("must not collect")))
    monkeypatch.setitem(collectors, "gaussdb_storage", collector)
    target = Target(tenant_id=1, code="tenant-1", name="one", short_name="one", project_ref=11,
                    project_id="project-11", project_name="synthetic", region="synthetic", workspaces=[])
    client = Mock(side_effect=AssertionError("must not call cloud"))
    result = pipeline._run_task(mem_db, "gaussdb_storage", client=client, target=target, trigger="MANUAL")
    assert result.status == "SKIPPED"
    collector.collect.assert_not_called()
    assert not client.mock_calls
    assert state(mem_db, "gaussdb_storage").data == before and row.attrs == attrs


def test_run_task_discards_result_after_task_disabled_during_collection(mem_db, gauss_rules, monkeypatch):
    from app.collectors.registry import TASKS as collectors

    configure(mem_db)
    row = run(mem_db, "gaussdb_storage", bad=True)
    run(mem_db, "gaussdb_storage", bad=True, at=NOW + timedelta(seconds=1))
    run(mem_db, "gaussdb_storage", at=NOW + timedelta(seconds=2))
    before = deepcopy(state(mem_db, "gaussdb_storage").data)
    tasks = deepcopy(row.attrs["inspection_monitoring"]["tasks"])

    def collect_and_disable(ctx):
        mem_db.get(InspectionTask, "gaussdb_storage").enabled = False
        evidence.sync_gauss_task_requirements(mem_db)
        mem_db.commit()
        return [observation("gaussdb_storage")]

    monkeypatch.setitem(collectors, "gaussdb_storage", SimpleNamespace(service="gaussdb", collect=collect_and_disable))
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW + timedelta(seconds=3))
    target = Target(tenant_id=1, code="tenant-1", name="one", short_name="one", project_ref=11,
                    project_id="project-11", project_name="synthetic", region="synthetic", workspaces=[])
    client = Mock(calls=0)
    client.metrics_snapshot.return_value = {}
    result = pipeline._run_task(mem_db, "gaussdb_storage", client=client, target=target, trigger="MANUAL")
    assert result.status == "SKIPPED" and result.stats["reason"] == "policy_changed"
    assert state(mem_db, "gaussdb_storage").data == before
    assert row.attrs["inspection_monitoring"]["tasks"] == tasks
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"


def test_legacy_context_without_task_keeps_flat_format_and_cannot_erase_task_results(mem_db, gauss_rules):
    ctx = RunContext(client=SimpleNamespace(), account="tenant-1", project_id="project-11", tenant_id=1, project_ref=11)
    row = run(mem_db, ctx=ctx)
    assert "tasks" not in row.attrs["inspection_monitoring"]
    assert inspection_state(row, NOW) == "FRESH"
    row = run(mem_db)
    before = deepcopy(row.attrs["inspection_monitoring"])
    evidence.record_inspection(row, ctx, 1, 0, NOW + timedelta(minutes=1))
    assert row.attrs["inspection_monitoring"] == before


def test_other_resource_retains_flat_result_and_missing_timestamp(mem_db):
    row = Resource(service="dcs", resource_type="dcs_instance", resource_id="dcs", name="dcs",
                   tenant_id=1, project_ref=11, attrs={"other": True})
    mem_db.add(row)
    mem_db.flush()
    ctx = context("dcs_status")
    evidence.record_inspection(row, ctx, 2, 0, NOW)
    assert row.attrs["inspection_monitoring"] == {"task_code": "dcs_status", "evaluated_at": NOW.isoformat(),
        "checked_rules": 2, "unavailable_rules": 0, "stale_after_sec": 900}
    assert inspection_state(row, NOW) == "FRESH"
    evidence.mark_missing_resources({row.id: row}, {row.id: 1}, ctx)
    assert row.attrs["inspection_monitoring"]["evaluated_at"] == NOW.isoformat()
    assert current_health(row, NOW) == "UNKNOWN"
    assert row.attrs["other"] is True


@pytest.mark.parametrize("task", TASKS)
def test_no_enabled_rules_still_requires_valid_owned_observation(mem_db, gauss_rules, task):
    for rule in gauss_rules:
        if rule.check_item == TASK_CHECKS[task]:
            rule.enabled = False
    mem_db.commit()
    row = run(mem_db, task, ctx=context(task, required=[task]))
    raw = row.attrs["inspection_monitoring"]["tasks"][task]
    assert raw["checked_rules"] == raw["unavailable_rules"] == 0
    assert raw["no_applicable_rules"] is True and raw["observation_available"] is True
    assert inspection_state(row, NOW) == "FRESH" and current_health(row, NOW) == "OK"
    fail(mem_db, context(task, required=[task]), NOW + timedelta(seconds=1))
    assert row.attrs["inspection_monitoring"]["tasks"][task]["evaluated_at"] == NOW.isoformat()
    assert inspection_state(row, NOW + timedelta(seconds=1)) == "NO_DATA"


@pytest.mark.parametrize("task,facts", [("gaussdb_storage", {"disk_usage": None}),
    ("gaussdb_storage", {"disk_usage": float("nan")}), ("gaussdb_storage", {"disk_usage": True}),
    ("gaussdb_storage", {"disk_usage": 101}), ("gaussdb_status", {"status": None}),
    ("gaussdb_status", {"status": "UNDOCUMENTED"}),
    ("gaussdb_components", {"down_components": None, "unknown_components": None}),
    ("gaussdb_components", {"down_components": 0, "unknown_components": 1})])
def test_no_enabled_rules_cannot_make_missing_or_invalid_sample_fresh(mem_db, gauss_rules, task, facts):
    for rule in gauss_rules:
        rule.enabled = False
    mem_db.commit()
    obs = observation(task)
    obs.facts = facts
    pipeline.process_observations(mem_db, "gaussdb", [obs], context(task, required=[task]), NOW)
    row = mem_db.scalar(select(Resource))
    assert row.attrs["inspection_monitoring"]["tasks"][task]["observation_available"] is False
    assert inspection_state(row, NOW) == "NO_DATA" and current_health(row, NOW) == "UNKNOWN"


def test_ruleless_task_cannot_borrow_another_task_or_resource_observation(mem_db, gauss_rules):
    row = run(mem_db)
    ctx = context("gaussdb_storage", required=["gaussdb_storage"])
    for observations in (None, [observation()], [observation("gaussdb_storage", rid="different")]):
        evidence.record_inspection(row, ctx, 0, 0, NOW, observations=observations)
        assert inspection_state(row, NOW) == "NO_DATA"


@pytest.fixture
def pg_resource_lock_gate(mem_db):
    """在真实 PG 行锁已取得后暂停首事务，使第二个事务真实等待同一批资源。"""
    engine = mem_db.get_bind()
    first_locked, second_requested, second_locked, release = Event(), Event(), Event(), Event()

    def before(conn, cursor, statement, parameters, context, executemany):
        if (conn.info.get("gauss_inspection_test_worker") == "second" and "FROM resource" in statement
                and "FOR UPDATE" in statement):
            second_requested.set()

    def after(conn, cursor, statement, parameters, context, executemany):
        if "FROM resource" not in statement or "FOR UPDATE" not in statement:
            return
        worker = conn.info.get("gauss_inspection_test_worker")
        if worker == "first" and not first_locked.is_set():
            first_locked.set()
            assert release.wait(8), "首事务等待释放超时"
        elif worker == "second":
            second_locked.set()

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        yield first_locked, second_requested, second_locked, release
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


def concurrent_pg_actions(engine, gate, first_action, second_action):
    first_locked, second_requested, second_locked, release = gate

    def worker(label, action):
        with Session(engine, expire_on_commit=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            info = db.connection().info
            info["gauss_inspection_test_worker"] = label
            try:
                value = action(db)
                db.commit()
                return value
            finally:
                info.pop("gauss_inspection_test_worker", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "first", first_action)
        try:
            assert first_locked.wait(5)
            second = pool.submit(worker, "second", second_action)
            assert second_requested.wait(5)
            assert not second_locked.wait(0.2), "第二事务不应越过第一事务持有的资源锁"
        finally:
            release.set()
        first.result(timeout=12)
        second.result(timeout=12)


@pytest.mark.pg
def test_pg_partial_and_full_runs_lock_same_target_order_without_deadlock(
        mem_db, gauss_rules, pg_resource_lock_gate):
    configure(mem_db)
    for task in TASKS:
        pipeline.process_observations(mem_db, "gaussdb", [observation(task, rid=rid) for rid in ("a", "b")],
                                      context(task), NOW)
    peer = run(mem_db, ctx=context(tenant=2, project=21), rid="a")
    peer_attrs = deepcopy(peer.attrs)
    peer_id = peer.id
    mem_db.commit()

    def partial(db):
        # 只有较大 ID 返回。若先只锁 b、再去补锁 a，就会与完整列表事务形成锁顺序反转。
        return pipeline.process_observations(db, "gaussdb", [observation("gaussdb_storage", rid="b")],
            context("gaussdb_storage"), NOW + timedelta(seconds=1))

    def full(db):
        return pipeline.process_observations(db, "gaussdb", [observation(rid=rid) for rid in ("a", "b")],
            context(), NOW + timedelta(seconds=2))

    concurrent_pg_actions(mem_db.get_bind(), pg_resource_lock_gate, partial, full)
    mem_db.expire_all()
    rows = {row.resource_id: row for row in mem_db.scalars(select(Resource).where(Resource.project_ref == 11))}
    for rid, row in rows.items():
        tasks = row.attrs["inspection_monitoring"]["tasks"]
        assert tasks["gaussdb_status"]["evaluated_at"] == (NOW + timedelta(seconds=2)).isoformat()
        assert tasks["gaussdb_storage"]["evaluated_at"] == (NOW + timedelta(seconds=1 if rid == "b" else 0)).isoformat()
    assert inspection_state(rows["a"], NOW + timedelta(seconds=2)) == "PARTIAL"
    assert inspection_state(rows["b"], NOW + timedelta(seconds=2)) == "FRESH"
    assert mem_db.get(Resource, peer_id).attrs == peer_attrs


@pytest.mark.pg
def test_pg_concurrent_task_toggles_read_requirements_after_resource_lock(
        mem_db, gauss_rules, pg_resource_lock_gate):
    configure(mem_db, ["gaussdb_status"])
    row = run(mem_db)
    row_id = row.id
    tasks = deepcopy(row.attrs["inspection_monitoring"]["tasks"])
    mem_db.commit()

    def enable(db, code):
        db.get(InspectionTask, code).enabled = True
        db.flush()
        return evidence.sync_gauss_task_requirements(db)

    concurrent_pg_actions(mem_db.get_bind(), pg_resource_lock_gate,
        lambda db: enable(db, "gaussdb_storage"), lambda db: enable(db, "gaussdb_components"))
    mem_db.expire_all()
    raw = mem_db.get(Resource, row_id).attrs["inspection_monitoring"]
    assert raw["required_tasks"] == sorted(TASKS)
    assert raw["tasks"] == tasks  # 启用任务只改变需求，不能补造首次样本。
