"""Collection policies: isolation, gates, clocks and physical-sample evaluation.

All cloud-facing paths use in-process doubles. Run with INSPECTOR_ENV_FILE=''.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.v1.collection_policies import router
from app.collectors.base import Observation, RunContext
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.hcs import HcsError
from app.ledger import sync
from app.ledger.fetchers import LedgerRecord
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AlarmRule,
    AuditLog,
    CollectSwitch,
    EvalState,
    HcsAccount,
    InspectionTask,
    Notification,
    Resource,
    ResourceChange,
    SysSetting,
)
from app.models.base import utcnow
from app.schemas.collection_policy import CollectionPolicy
from app.services import collect_switch, pipeline
from app.services import collection_policy as policies
from app.services.tenancy import Target

TARGET = Target(11, "one", "one", "one", 101, "p1", "p1", "region")


def tenant(db, tid=11):
    row = HcsAccount(id=tid, code=f"tenant-{tid}", name=f"tenant-{tid}",
                     iam_endpoint="https://example.invalid", domain_name="example")
    db.add(row)
    db.commit()
    return row


def resource(db, *, tid=11, project=101, rid="host", **kwargs):
    row = Resource(tenant_id=tid, project_ref=project, service="ecs", resource_type="ecs_server",
                   resource_id=rid, name=rid, **kwargs)
    db.add(row)
    db.commit()
    return row


def store(db, **kwargs):
    policy = CollectionPolicy(**kwargs)
    db.add(SysSetting(key=policies.policy_key(11), value=policy.model_dump(mode="json")))
    db.commit()
    return policy


class Client:
    calls = 0

    def configure_qps(self, _value):
        pass

    def metrics_snapshot(self):
        return {"calls": self.calls, "errors": 0, "rate_limit_ms": 0, "services": {}}


@pytest.fixture
def policy_api(mem_db):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = CurrentUser(id=1, username="tester", display_name="Tester", tenant_ids=[11],
                       permissions=["collect:manage", "ledger:view"])
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user

    @app.exception_handler(BizError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message, "data": exc.data})

    with TestClient(app) as client:
        yield client, user


def test_defaults_are_independent_and_never_create_master_switches(mem_db):
    p = policies.effective(mem_db, 11)
    assert (p.inventory.interval_sec, p.metrics.interval_sec, p.inspection.interval_sec,
            p.quota.interval_sec, p.stale_after_sec) == (86400, 300, 120, 3600, 900)
    assert p.metric_keys == ["cpu_util", "mem_util", "disk_util", "boot_time"]
    assert p.selection.mode == "all" and p.selection.resource_ids == []
    assert p.quota.enabled is False
    assert mem_db.scalar(select(func.count()).select_from(SysSetting)) == 0
    assert mem_db.scalar(select(func.count()).select_from(CollectSwitch)) == 0


@pytest.mark.parametrize("kwargs", [
    {"metrics": {"interval_sec": 29}}, {"inspection": {"interval_sec": True}},
    {"inventory": {"interval_sec": 99999999}}, {"metric_keys": ["password"]},
    {"metric_keys": ["cpu_util", "cpu_util"]}, {"selection": {"mode": "other"}},
    {"selection": {"mode": "selected", "resource_ids": [True]}},
    {"selection": {"mode": "selected", "resource_ids": ["1"]}},
    {"selection": {"mode": "selected", "resource_ids": [1, 1]}},
    {"stale_after_sec": 0}, {"metrics_source": "CES"},
    {"prometheus": {"queries": {"cpu_util": "up\n"}}},
    {"prometheus": {"queries": {"password": "up"}}},
    {"prometheus": {"queries": {"cpu_util": "http://example.invalid"}}},
    {"aom": {"namespace": "CUSTOM", "dimension": "id", "metrics": {"password": "remote"}}},
    {"aom": {"namespace": "CUSTOM", "dimension": "id", "metrics": {"cpu_util": "cpu"},
             "units": {"other": "Percent"}}},
])
def test_invalid_policy_fails_closed(kwargs):
    with pytest.raises(ValidationError):
        CollectionPolicy(**kwargs)


def test_legacy_task_aom_mapping_is_ignored_but_prometheus_fallback_is_preserved(mem_db):
    mapping = {"namespace": "CUSTOM", "dimension": "instance_id", "metrics": {"cpu_util": "cpu"}}
    mem_db.add(InspectionTask(code="ecs_status", name="old", service="ecs", interval_sec=120,
                              params={"aom": mapping, "prometheus": {"cpu_util": "up"}}))
    mem_db.commit()
    first = policies.effective(mem_db, 11)
    assert first.aom is None and first.prometheus.queries == {"cpu_util": "up"}
    store(mem_db, aom=None, prometheus=None)
    assert policies.effective(mem_db, 11).aom is None
    assert policies.effective(mem_db, 11).prometheus is None
    assert policies.effective(mem_db, 22).aom is None
    assert mem_db.get(InspectionTask, "ecs_status").params["aom"] == mapping


def test_local_selection_is_tenant_project_scoped_and_honors_monitored(mem_db):
    mine = resource(mem_db, critical=True)
    ordinary = resource(mem_db, rid="ordinary")
    excluded = resource(mem_db, rid="disabled", monitored=False, critical=True)
    deleted = resource(mem_db, rid="deleted", lifecycle="DELETED", critical=True)
    other_project = resource(mem_db, project=102, rid="host", critical=True)
    other_tenant = resource(mem_db, tid=22, project=202, rid="host", critical=True)
    p = CollectionPolicy(selection={"mode": "critical"})
    assert policies.select_resources(mem_db, 11, 101, p) == [mine]
    assert policies.resource_included(mine, p) and not policies.resource_included(ordinary, p)
    p.selection.mode, p.selection.resource_ids = "selected", [r.id for r in (
        mine, excluded, deleted, other_project, other_tenant)]
    assert policies.select_resources(mem_db, 11, 101, p) == [mine]
    p.selection.resource_ids = []
    assert policies.select_resources(mem_db, 11, 101, p) == []
    mem_db.add(CollectSwitch(tenant_id=11, resource_type="ecs_server", enabled=False))
    mem_db.commit()
    assert policies.select_resources(mem_db, 11, 101, CollectionPolicy()) == []


def test_selection_batches_queries_instead_of_querying_each_resource(mem_db):
    mem_db.add_all(Resource(tenant_id=11, project_ref=101, service="ecs", resource_type="ecs_server",
                           resource_id=str(i), name=str(i)) for i in range(1200))
    mem_db.commit()
    ids = list(mem_db.scalars(select(Resource.id)))
    p = CollectionPolicy(selection={"mode": "selected", "resource_ids": ids})
    queries = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement)

    event.listen(mem_db.get_bind(), "before_cursor_execute", record)
    try:
        assert len(policies.select_resources(mem_db, 11, 101, p)) == 1200
    finally:
        event.remove(mem_db.get_bind(), "before_cursor_execute", record)
    assert len(queries) == 4  # One master-switch read and three bounded resource batches.


def test_api_scopes_permission_audit_and_no_automatic_enable(mem_db, policy_api):
    tenant(mem_db)
    tenant(mem_db, 22)
    collect_switch.initialize_disabled(mem_db, 11, "test")
    mem_db.commit()
    api, user = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    assert api.get(path).status_code == 200
    assert api.get(path.replace("/11/", "/22/")).status_code == 404
    assert api.put(path.replace("/11/", "/22/"), json={}).status_code == 404
    assert api.put(path.replace("/11/", "/0/"), json={}).status_code == 400
    user.permissions = ["ledger:view"]
    assert api.put(path, json={}).status_code == 403
    user.permissions = ["collect:manage", "task:view"]
    response = api.put(path, json={"metrics": {"interval_sec": 180}, "inspection": {"enabled": False}})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert not data["master_enabled"] and not data["phase_status"]["metrics"]["effective_enabled"]
    assert data["policy"]["metrics"]["interval_sec"] == 180
    assert not collect_switch.load(mem_db).enabled(11, "ecs_server")
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "collection_policy.update"))
    assert audit.tenant_id == 11 and audit.detail["after"]["inspection"]["enabled"] is False


def test_api_rejects_foreign_and_deleted_local_ids_without_writes(mem_db, policy_api):
    tenant(mem_db)
    other = resource(mem_db, tid=22, project=202)
    gone = resource(mem_db, rid="gone", lifecycle="DELETED")
    api, _user = policy_api
    for rid in (other.id, gone.id, 99999):
        response = api.put("/api/v1/system/collection-policies/11/ecs_server", json={
            "selection": {"mode": "selected", "resource_ids": [rid]}})
        assert response.status_code == 400
    assert mem_db.scalar(select(SysSetting)) is None
    assert mem_db.scalar(select(AuditLog)) is None


def test_api_selection_partial_saves_preserve_mode_and_reject_foreign_ids(mem_db, policy_api):
    tenant(mem_db)
    first = resource(mem_db)
    foreign = resource(mem_db, tid=22, project=202)
    api, _user = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    response = api.put(path, json={"selection": {"mode": "selected", "resource_ids": [first.id]}})
    assert response.status_code == 200, response.text
    response = api.put(path, json={"metrics": {"interval_sec": 600}})
    assert response.json()["data"]["policy"]["selection"] == {"mode": "selected", "resource_ids": [first.id]}
    rejected = api.put(path, json={"selection": {"resource_ids": [foreign.id]}})
    assert rejected.status_code == 400
    assert policies.effective(mem_db, 11).selection.resource_ids == [first.id]
    response = api.put(path, json={"selection": {"resource_ids": []}})
    assert response.status_code == 200
    assert response.json()["data"]["policy"]["selection"] == {"mode": "selected", "resource_ids": []}
    assert policies.select_resources(mem_db, 11, 101, policies.effective(mem_db, 11)) == []


def test_policy_guard_does_not_create_explicit_policy_or_override_legacy_schedule(mem_db):
    mem_db.add(InspectionTask(code="ecs_status", name="status", service="ecs", interval_sec=600,
                              cron="0 * * * *", enabled=True))
    mem_db.commit()
    policy = policies.lock_for_run(mem_db, 11, {})
    mem_db.commit()
    assert mem_db.get(SysSetting, policies.policy_key(11)) is None
    assert policies.schedule(mem_db, 11, "inspection", policy) == (600, "0 * * * *", "task")


def test_unselected_observations_do_not_update_resource_evaluate_or_interrupt_state(mem_db, monkeypatch):
    tenant(mem_db)
    selected = resource(mem_db, rid="selected")
    excluded = resource(mem_db, rid="excluded")
    store(mem_db)
    setup_rule(mem_db)
    now = utcnow()
    ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101, db=mem_db,
                     params={"_task_code": "ecs_status", "_collection_phase": "inspection"})
    observed = Observation("ecs_server", excluded.resource_id, excluded.name, "cpu", {"cpu_util": 99},
                           metrics={"cpu_util": 99}, metric_observed_at=now)
    pipeline.process_observations(mem_db, "ecs", [observed], ctx, now)
    state = mem_db.scalar(select(EvalState))
    before_state = dict(state.data)
    before_resource = (dict(excluded.attrs), dict(excluded.metrics), excluded.last_seen, excluded.health)
    policies.save(mem_db, 11, "ecs_server", CollectionPolicy(metrics={"enabled": False},
        selection={"mode": "selected", "resource_ids": [selected.id]}), "scope-editor")
    mem_db.commit()
    evaluated = Mock(side_effect=AssertionError("unselected ECS must not be evaluated"))
    monkeypatch.setattr(pipeline, "evaluate_rule", evaluated)
    # Include hostile/unexpected observations to exercise the final pipeline gate,
    # independent of a correctly behaving collector.
    pipeline.process_observations(mem_db, "ecs", [observed,
        Observation("ecs_server", "unknown", "unknown", "cpu", {"cpu_util": 99})], ctx, now)
    assert not evaluated.called and ctx.stats["processed_resources"] == 0
    assert state.data == before_state
    assert (excluded.attrs, excluded.metrics, excluded.last_seen, excluded.health) == before_resource
    assert mem_db.scalar(select(Resource).where(Resource.resource_id == "unknown")) is None


def test_policy_save_after_outer_run_check_discards_observations(mem_db, monkeypatch):
    tenant(mem_db)
    host = resource(mem_db)
    store(mem_db)
    monkeypatch.setitem(pipeline.TASKS, "ecs_status", SimpleNamespace(service="ecs", collect=lambda ctx: [
        Observation("ecs_server", host.resource_id, host.name, "instance_status", {"status": "ERROR"},
                    status_raw="ERROR")]))
    process = pipeline.process_observations

    def save_before_persistence(db, service, observations, ctx, now):
        policies.save(db, 11, "ecs_server", CollectionPolicy(selection={"mode": "selected", "resource_ids": []}), "editor")
        db.commit()
        return process(db, service, observations, ctx, now)

    monkeypatch.setattr(pipeline, "process_observations", save_before_persistence)
    result = pipeline.run_task(mem_db, "ecs_status", client=Client(), target=TARGET, trigger="MANUAL")
    assert result.status == "SKIPPED" and result.stats["reason"] == "policy_changed"
    assert mem_db.get(Resource, host.id).status_raw != "ERROR"
    assert mem_db.scalar(select(EvalState)) is None
    assert policies.effective(mem_db, 11).selection.resource_ids == []


@pytest.mark.pg
def test_pg_policy_save_while_worker_waits_discards_pending_changes_and_observations(mem_db):
    tenant(mem_db)
    host = resource(mem_db)
    store(mem_db)
    params = {"_task_code": "ecs_status", "_collection_phase": "inspection"}
    policies.capture_run(mem_db, 11, params)
    mem_db.rollback()
    engine = mem_db.get_bind()
    waiting = Event()

    def before_execute(conn, _cursor, statement, _parameters, _context, _many):
        if conn.info.get("ecs_scope_worker") and "hcs_account" in statement and "FOR UPDATE" in statement:
            waiting.set()

    def worker():
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            connection = db.connection()
            connection_info = connection.info
            connection_info["ecs_scope_worker"] = True
            try:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                db.add(ResourceChange(resource_ref=host.id, resource_type="ecs_server", resource_id=host.resource_id,
                    name=host.name, tenant_id=11, change_type="REBOOT", changes=[], source="INSPECT", operator="worker"))
                db.flush()
                ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101,
                                 db=db, params=dict(params))
                with pytest.raises(policies.PolicyChanged):
                    pipeline.process_observations(db, "ecs", [Observation("ecs_server", host.resource_id, host.name,
                        "instance_status", {"status": "ERROR"}, status_raw="ERROR")], ctx, utcnow())
                db.rollback()
            finally:
                connection_info.pop("ecs_scope_worker", None)

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with Session(engine) as editor, ThreadPoolExecutor(max_workers=1) as pool:
            policies.save(editor, 11, "ecs_server", CollectionPolicy(selection={"mode": "selected", "resource_ids": []}),
                          "scope-editor")
            future = pool.submit(worker)
            try:
                assert waiting.wait(3), "worker did not reach the tenant lock"
                assert not future.done()
            finally:
                editor.commit()
            future.result(timeout=6)
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
    mem_db.expire_all()
    assert mem_db.get(Resource, host.id).status_raw != "ERROR"
    assert mem_db.scalar(select(ResourceChange)) is None
    assert mem_db.scalar(select(EvalState)) is None
    assert policies.effective(mem_db, 11).selection.resource_ids == []


@pytest.mark.pg
def test_pg_metrics_and_selection_wait_for_tenant_without_holding_guard_or_deadlocking(mem_db):
    tenant(mem_db)
    host = resource(mem_db)
    policies.save(mem_db, 11, "ecs_server", CollectionPolicy(), "initial")
    alarm = AlarmEvent(tenant_id=11, fingerprint="scope-lock", rule_id="scope-lock", service="ecs",
        resource_type="ecs_server", resource_ref=host.id, resource_name=host.name, check_item="disk",
        severity="P2", status="FIRING", first_seen=utcnow(), last_seen=utcnow(), title="disk", detail={})
    mem_db.add(alarm)
    mem_db.commit()
    engine = mem_db.get_bind()
    entered = {role: Event() for role in ("metrics", "selection")}

    def before_execute(conn, _cursor, statement, _parameters, _context, _many):
        role = conn.info.get("ecs_scope_lock_role")
        if role in entered and "hcs_account" in statement and "FOR UPDATE" in statement:
            entered[role].set()

    def worker(role):
        with Session(engine, autoflush=False) as db:
            connection = db.connection()
            connection_info = connection.info
            connection_info["ecs_scope_lock_role"] = role
            try:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                if role == "metrics":
                    policies.lock_for_run(db, 11, {})
                    db.scalar(select(Resource).where(Resource.id == host.id).with_for_update())
                    db.scalar(select(AlarmEvent).where(AlarmEvent.id == alarm.id).with_for_update())
                else:
                    policies.save(db, 11, "ecs_server",
                                  CollectionPolicy(selection={"mode": "selected", "resource_ids": []}), "editor")
                db.commit()
            finally:
                connection_info.pop("ecs_scope_lock_role", None)

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with Session(engine) as manageone_editor, ThreadPoolExecutor(max_workers=2) as pool:
            # Same lock prefix as ManageOne save, using only synthetic rows and
            # no credentials, endpoints or production configuration.
            manageone_editor.scalar(select(HcsAccount).where(HcsAccount.id == 11).with_for_update())
            manageone_editor.scalar(select(AlarmEvent).where(AlarmEvent.id == alarm.id).with_for_update())
            futures = [pool.submit(worker, role) for role in entered]
            try:
                assert all(signal.wait(3) for signal in entered.values())
                assert all(not future.done() for future in futures)
                with Session(engine) as observer:
                    # Neither waiting worker may hold the guard and wait back
                    # on a tenant/alarm lock held by a ManageOne save.
                    guard = observer.scalar(select(SysSetting).where(
                        SysSetting.key == policies._guard_key(11)).with_for_update(nowait=True))
                    assert guard is not None
                    observer.rollback()
            finally:
                manageone_editor.commit()
            for future in futures:
                future.result(timeout=6)
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
    mem_db.expire_all()
    assert mem_db.get(AlarmEvent, alarm.id).status == "CLOSED"


def test_disabling_inspection_closes_active_only_once_and_keeps_history(mem_db):
    tenant(mem_db)
    for i, (tid, status) in enumerate(((11, "FIRING"), (11, "ACKED"), (11, "SUPPRESSED"),
                                       (11, "RESOLVED"), (22, "FIRING"))):
        mem_db.add(AlarmEvent(tenant_id=tid, fingerprint=f"alarm-{i}", rule_id="r", service="ecs",
            resource_type="ecs_server", resource_name="host", severity="P2", status=status,
            title="CPU", check_item="cpu"))
    mem_db.commit()
    p = CollectionPolicy(inspection={"enabled": False})
    result = policies.save(mem_db, 11, "ecs_server", p, "operator")
    mem_db.commit()
    assert result["closed_alarms"] == 3
    assert policies.save(mem_db, 11, "ecs_server", p, "operator")["closed_alarms"] == 0
    assert [r.action for r in mem_db.scalars(select(AlarmEventLog))] == ["CLOSE"] * 3
    assert mem_db.scalar(select(func.count()).select_from(AlarmEvent)) == 5
    assert mem_db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == "alarm-3")).status == "RESOLVED"
    assert mem_db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == "alarm-4")).status == "FIRING"


def test_clocks_independent_by_tenant_project_type_phase_and_durable(mem_db):
    now = utcnow()
    p = CollectionPolicy(metrics={"interval_sec": 180})
    claimed = policies.claim(mem_db, 11, 101, "metrics", p, now=now)
    assert claimed is not None
    assert policies.claim(mem_db, 11, 101, "metrics", p, force=True, now=now) is None
    assert policies.finish(mem_db, claimed, True, now=now)
    assert not policies.due(mem_db, 11, 101, "metrics", p, now=now + timedelta(seconds=179))
    assert policies.due(mem_db, 11, 101, "metrics", p, now=now + timedelta(seconds=180))
    assert policies.due(mem_db, 11, 102, "metrics", p, now=now)
    assert policies.due(mem_db, 22, 101, "metrics", p, now=now)
    assert policies.due(mem_db, 11, 101, "inspection", p, now=now)
    assert policies.due(mem_db, 11, 101, "metrics", p, rtype="other", now=now)
    mem_db.expire_all()
    assert not policies.due(mem_db, 11, 101, "metrics", p, now=now)
    assert len(claimed.key) <= 64


def test_failure_retries_next_tick_and_expired_owner_cannot_finish_new_claim(mem_db):
    now = utcnow()
    claim = policies.claim(mem_db, 11, 101, "inventory", now=now)
    assert policies.finish(mem_db, claim, False, now=now)
    assert not policies.due(mem_db, 11, 101, "inventory", now=now + timedelta(seconds=59))
    retry = policies.claim(mem_db, 11, 101, "inventory", now=now + timedelta(seconds=60))
    assert retry is not None
    replacement = policies.claim(mem_db, 11, 101, "inventory", now=now + timedelta(seconds=60 + policies.LEASE_SEC))
    assert replacement is not None and replacement.token != retry.token
    assert not policies.finish(mem_db, retry, True, now=now)
    assert policies.finish(mem_db, replacement, True, now=now)


@pytest.mark.parametrize("phase", ["inventory", "metrics", "inspection", "quota"])
def test_manual_force_never_bypasses_master_or_stage_gate(mem_db, phase):
    p = CollectionPolicy(**{phase: {"enabled": False}})
    assert policies.claim(mem_db, 11, 101, phase, p, force=True) is None
    mem_db.add(CollectSwitch(tenant_id=11, resource_type="ecs_server", enabled=False))
    mem_db.commit()
    assert policies.claim(mem_db, 11, 101, phase, CollectionPolicy(), force=True) is None


def setup_rule(db):
    db.add(AlarmRule(id="policy-cpu", name="CPU", resource_type="ecs_server", check_item="cpu", kind="threshold",
        expr={"value": "cpu_util", "levels": [{"threshold": 80, "severity": "P2"}]}, for_count=2,
        recover={"when": "cpu_util < 70", "for_count": 2}))
    db.commit()


def test_physical_sample_cannot_advance_hits_or_recovery_twice(mem_db):
    resource(mem_db)
    setup_rule(mem_db)
    now = utcnow()
    ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101, db=mem_db,
                     params={"_collection_phase": "inspection"})

    def point(value, at):
        obs = Observation("ecs_server", "host", "host", "cpu", {"cpu_util": value, "metric_observed_at": at.isoformat()})
        pipeline.process_observations(mem_db, "ecs", [obs], ctx, now=now)

    point(95, now - timedelta(seconds=10))
    point(95, now - timedelta(seconds=10))
    assert mem_db.scalar(select(EvalState)).data["hits"] == 1
    assert mem_db.scalar(select(AlarmEvent)) is None
    point(95, now - timedelta(seconds=9))
    alarm = mem_db.scalar(select(AlarmEvent))
    assert alarm.status == "FIRING"
    point(10, now - timedelta(seconds=8))
    point(10, now - timedelta(seconds=8))
    assert alarm.status == "FIRING" and mem_db.scalar(select(EvalState)).data["ok_hits"] == 1
    point(10, now - timedelta(seconds=7))
    assert alarm.status == "RESOLVED"


def test_metrics_phase_never_evaluates_and_stale_inspection_never_recovers(mem_db):
    resource(mem_db)
    setup_rule(mem_db)
    now = utcnow()
    ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101, db=mem_db,
                     params={"_collection_phase": "metrics"})
    obs = Observation("ecs_server", "host", "host", "cpu", {"cpu_util": 99, "metric_observed_at": now.isoformat()},
                      metrics={"cpu_util": 99})
    for _ in range(3):
        pipeline.process_observations(mem_db, "ecs", [obs], ctx, now=now)
    assert mem_db.scalar(select(EvalState)) is None and mem_db.scalar(select(AlarmEvent)) is None
    ctx.params["_collection_phase"] = "inspection"
    obs.facts["metric_observed_at"] = (now - timedelta(seconds=901)).isoformat()
    pipeline.process_observations(mem_db, "ecs", [obs], ctx, now=now)
    assert mem_db.scalar(select(EvalState)).data == {"hits": 0, "ok_hits": 0}


def test_inventory_keeps_unselected_hosts_and_independent_project_clocks(mem_db, monkeypatch):
    first = resource(mem_db)
    second = resource(mem_db, rid="other")
    store(mem_db, selection={"mode": "selected", "resource_ids": [first.id]})
    fetch = Mock(return_value=[LedgerRecord("host", "host"), LedgerRecord("other", "other")])
    monkeypatch.setitem(sync.FETCHERS, "ecs_server", fetch)
    run = sync.sync_ledger(mem_db, client=Client(), target=TARGET, types=["ecs_server"])
    assert run.status == "SUCCESS" and run.resource_total == 2
    assert first.lifecycle == second.lifecycle == "ACTIVE"
    assert sync.sync_ledger(mem_db, client=Client(), target=TARGET, types=["ecs_server"]).status == "SKIPPED"
    assert fetch.call_count == 1
    assert sync.sync_ledger(mem_db, client=Client(), target=TARGET, types=["ecs_server"], trigger="MANUAL").status == "SUCCESS"
    assert fetch.call_count == 2


def test_pipeline_passes_full_json_policy_and_metrics_phase_does_not_inspect(mem_db, monkeypatch):
    resource(mem_db)
    store(mem_db, metrics={"interval_sec": 180}, inspection={"enabled": False})
    seen = []

    def collect(ctx):
        seen.append(ctx)
        return [Observation("ecs_server", "host", "host", "cpu", {"cpu_util": 99}, metrics={"cpu_util": 99})]

    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=collect))
    run = pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET)
    assert run.status == "SUCCESS"
    assert seen[0].params["_collection_phase"] == "metrics"
    assert type(seen[0].params["_collection_policy"]) is dict
    assert seen[0].params["_collection_policy"]["metrics"]["interval_sec"] == 180
    assert not seen[0].params["_collection_policy"]["inspection"]["enabled"]
    assert mem_db.scalar(select(AlarmEvent)) is None


def test_failed_run_releases_clock_for_retry_without_advancing_success(mem_db, monkeypatch):
    def fail(_ctx):
        raise HcsError(503, "MOCK.FAILURE", "synthetic failure")

    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=fail))
    run = pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET)
    assert run.status == "FAILED"
    clock = mem_db.scalar(select(SysSetting.value).where(
        SysSetting.key == policies.clock_key(11, 101, "ecs_server", "metrics")))
    assert "last_success" not in clock and "token" not in clock and "retry_at" in clock


def test_manual_refresh_respects_explicit_task_gate(mem_db):
    mem_db.add(InspectionTask(code="ecs_metrics", name="metrics", service="ecs", interval_sec=300, enabled=False))
    mem_db.commit()
    assert not policies.stage_enabled(mem_db, 11, "metrics", CollectionPolicy().model_dump(mode="json"))
    assert policies.claim(mem_db, 11, 101, "metrics", force=True) is None


def test_clock_concurrent_sessions_have_one_winner_and_survive_restart(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'clocks.db'}", connect_args={"timeout": 0.1})
    for table in (SysSetting.__table__, CollectSwitch.__table__, InspectionTask.__table__):
        table.create(engine)
    barrier = Barrier(4)
    now = utcnow()

    def attempt(_):
        with Session(engine) as db:
            barrier.wait(timeout=5)
            return policies.claim(db, 11, 101, "metrics", CollectionPolicy(), now=now)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [claim for claim in pool.map(attempt, range(4)) if claim is not None]
        assert len(claims) == 1
        with Session(engine) as db:
            assert policies.finish(db, claims[0], True, now=now)
        with Session(engine) as restarted:
            assert not policies.due(restarted, 11, 101, "metrics", now=now)
    finally:
        engine.dispose()


def test_transient_clock_lock_failure_rolls_back_and_retries(mem_db, monkeypatch):
    insert = policies._insert
    calls = []
    waits = []

    def locked_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("insert", {}, Exception("database is locked"))
        return insert(*args)

    monkeypatch.setattr(policies, "_insert", locked_once)
    monkeypatch.setattr(policies, "sleep", waits.append)
    assert policies.claim(mem_db, 11, 101, "metrics") is not None
    assert len(calls) == 2 and waits == [0.01]


def test_duplicate_metric_guard_does_not_delay_instance_status_rules(mem_db):
    resource(mem_db)
    mem_db.add(AlarmRule(id="status", name="status", resource_type="ecs_server", check_item="instance_status",
        kind="state", expr={"when": "status == 'ERROR'", "severity": "P1"}, for_count=2, recover={}))
    mem_db.commit()
    now = utcnow()
    ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101, db=mem_db,
                     params={"_collection_phase": "inspection"})
    obs = Observation("ecs_server", "host", "host", "instance_status", {"status": "ERROR"})
    for _ in range(2):
        pipeline.process_observations(mem_db, "ecs", [obs], ctx, now=now)
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"
    assert mem_db.scalar(select(EvalState)).data["hits"] == 2


def test_explicit_null_mapping_is_removed_from_legacy_context(mem_db, monkeypatch):
    store(mem_db, aom=None, prometheus=None)
    mem_db.add(InspectionTask(code="ecs_metrics", name="metrics", service="ecs", interval_sec=300, enabled=True,
        params={"aom": {"namespace": "OLD"}, "prometheus": {"cpu_util": "legacy"}}))
    mem_db.commit()
    contexts = []
    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(
        service="ecs", collect=lambda ctx: contexts.append(ctx) or []))
    assert pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET).status == "SUCCESS"
    assert "aom" not in contexts[0].params and "prometheus" not in contexts[0].params
    assert contexts[0].params["_collection_policy"]["aom"] is None


def test_disable_during_collection_rolls_back_pending_collector_writes(mem_db, monkeypatch):
    row = resource(mem_db)
    store(mem_db)

    def collect(ctx):
        # An API session changes the policy while collection is in flight.
        disabled = CollectionPolicy(metrics={"enabled": False}).model_dump(mode="json")
        with Session(mem_db.get_bind()) as other:
            other.execute(update(SysSetting).where(SysSetting.key == policies.policy_key(11)).values(value=disabled))
            other.commit()
        ctx.db.add(ResourceChange(resource_ref=row.id, resource_type="ecs_server", resource_id="host", name="host",
                                  tenant_id=11, change_type="REBOOT", changes=[], source="INSPECT", operator="test"))
        return []

    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=collect))
    result = pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET)
    assert result.status == "SKIPPED" and result.stats["reason"] == "policy_changed"
    assert mem_db.scalar(select(ResourceChange)) is None
    assert not policies.effective(mem_db, 11).metrics.enabled


def test_scheduled_custom_frequency_runs_on_due_ticks_without_client_initialization_between(mem_db, monkeypatch):
    store(mem_db, metrics={"interval_sec": 180})
    now = utcnow()
    clock = [now]
    collects = []
    clients = []
    monkeypatch.setattr(policies, "utcnow", lambda: clock[0])
    monkeypatch.setattr(pipeline, "resolve_targets", lambda *_: [TARGET])

    def make_client(*_args, **_kwargs):
        clients.append(clock[0])
        return Client()

    def collect(_ctx):
        collects.append(clock[0])
        return []

    monkeypatch.setattr(Target, "client", make_client)
    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=collect))
    for seconds in (0, 60, 120, 180):
        clock[0] = now + timedelta(seconds=seconds)
        runs = pipeline.run_all(mem_db, "ecs_metrics")
        assert len(runs) == (1 if seconds in (0, 180) else 0)
    assert clients == collects == [now, now + timedelta(seconds=180)]


def test_partial_phase_persists_valid_observations_but_retries_before_full_interval(mem_db, monkeypatch):
    row = resource(mem_db)

    def collect(ctx):
        ctx.errors.append("synthetic partial failure")
        return [Observation("ecs_server", "host", "host", "metric_sample", {}, metrics={"cpu_util": 10})]

    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=collect))
    result = pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET)
    assert result.status == "PARTIAL" and row.metrics["cpu_util"] == 10
    clock = mem_db.scalar(select(SysSetting.value).where(
        SysSetting.key == policies.clock_key(11, 101, "ecs_server", "metrics")))
    assert "last_success" not in clock and "retry_at" in clock and "token" not in clock


def test_inventory_poll_skips_due_and_disabled_targets_without_run_or_client(mem_db, monkeypatch):
    monkeypatch.setattr(sync, "resolve_targets", lambda *_: [TARGET])
    make_client = Mock(side_effect=AssertionError("must not create a cloud client"))
    monkeypatch.setattr(Target, "client", make_client)
    claimed = policies.claim(mem_db, 11, 101, "inventory")
    policies.finish(mem_db, claimed, True)
    assert sync.sync_all(mem_db, types=["ecs_server"]) == []
    mem_db.add(CollectSwitch(tenant_id=11, resource_type="ecs_server", enabled=False))
    mem_db.commit()
    assert sync.sync_all(mem_db, types=["ecs_server"]) == []
    make_client.assert_not_called()


def test_new_tenant_initializer_explicitly_disables_all_stages_and_is_idempotent(mem_db, policy_api):
    tenant(mem_db)
    policies.initialize_disabled(mem_db, 11, "creator")
    mem_db.commit()
    policy = policies.effective(mem_db, 11)
    assert all(not getattr(policy, phase).enabled for phase in policies.PHASES)
    assert (policy.inventory.interval_sec, policy.metrics.interval_sec, policy.inspection.interval_sec,
            policy.quota.interval_sec) == (86400, 300, 120, 3600)
    assert policy.aom is None and policy.prometheus is None
    row = mem_db.get(SysSetting, policies.policy_key(11))
    assert row.updated_by == "creator"
    policy.metrics.enabled = True
    policies.save(mem_db, 11, "ecs_server", policy, "editor")
    mem_db.commit()
    policies.initialize_disabled(mem_db, 11, "another-creator")
    mem_db.commit()
    assert policies.effective(mem_db, 11).metrics.enabled
    assert row.updated_by == "editor"
    policies.initialize_disabled(mem_db, 0, "global")
    assert mem_db.get(SysSetting, policies.policy_key(0)) is None


def test_put_omitted_stages_do_not_enable_new_tenant_policy(mem_db, policy_api):
    tenant(mem_db)
    policies.initialize_disabled(mem_db, 11, "creator")
    mem_db.commit()
    api, _user = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    response = api.put(path, json={"metrics": {"interval_sec": 600}})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert all(not data["policy"][phase]["enabled"] for phase in policies.PHASES)
    assert all(not data["phase_status"][phase]["configured_enabled"] for phase in policies.PHASES)
    assert all(not data["phase_status"][phase]["effective_enabled"] for phase in policies.PHASES)
    assert data["policy"]["metrics"]["interval_sec"] == 600
    response = api.put(path, json={"metrics": {"enabled": True}})
    assert response.json()["data"]["policy"]["metrics"] == {"enabled": True, "interval_sec": 600}
    assert response.json()["data"]["policy"]["inspection"]["enabled"] is False


def test_legacy_custom_intervals_are_fallback_only_and_remain_live(mem_db):
    for code, interval in (("ecs_status", 600), ("ecs_metrics", 600)):
        mem_db.add(InspectionTask(code=code, name=code, service="ecs", interval_sec=interval, enabled=True))
    mem_db.commit()
    p = policies.effective(mem_db, 11)
    assert p.metrics.interval_sec == p.inspection.interval_sec == 600
    now = utcnow()
    claim = policies.claim(mem_db, 11, 101, "inspection", now=now)
    policies.finish(mem_db, claim, True, now=now)
    assert not policies.due(mem_db, 11, 101, "inspection", now=now + timedelta(seconds=599))
    assert policies.due(mem_db, 11, 101, "inspection", now=now + timedelta(seconds=600))
    task = mem_db.get(InspectionTask, "ecs_status")
    task.interval_sec = 900
    mem_db.commit()
    assert policies.effective(mem_db, 11).inspection.interval_sec == 900
    assert not policies.due(mem_db, 11, 101, "inspection", now=now + timedelta(seconds=600))
    store(mem_db, inspection={"interval_sec": 120})
    assert policies.effective(mem_db, 11).inspection.interval_sec == 120
    assert policies.due(mem_db, 11, 101, "inspection", now=now + timedelta(seconds=120))


def test_legacy_daily_cron_is_honored_until_explicit_tenant_override(mem_db, monkeypatch):
    monkeypatch.setattr(policies.get_settings(), "timezone", "UTC")
    mem_db.add(InspectionTask(code="ecs_status", name="status", service="ecs", interval_sec=600,
                              cron="0 3 * * *", enabled=True))
    mem_db.commit()
    midday = datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert not policies.due(mem_db, 11, 101, "inspection", now=midday)
    at_fire = datetime(2026, 11, 3, 3, 0, 20, tzinfo=UTC)
    assert policies.due(mem_db, 11, 101, "inspection", now=at_fire)
    claim = policies.claim(mem_db, 11, 101, "inspection", now=at_fire)
    policies.finish(mem_db, claim, True, now=at_fire)
    assert not policies.due(mem_db, 11, 101, "inspection", now=at_fire + timedelta(minutes=10))
    status = policies.phase_status(mem_db, 11, policies.effective(mem_db, 11))["inspection"]
    assert status["cron"] == "0 3 * * *" and status["schedule_source"] == "task"
    assert status["effective_enabled"] and status["interval_sec"] == 600
    store(mem_db, inspection={"interval_sec": 120})
    assert policies.due(mem_db, 11, 101, "inspection", now=at_fire + timedelta(seconds=120))
    status = policies.phase_status(mem_db, 11, policies.effective(mem_db, 11))["inspection"]
    assert status["cron"] is None and status["schedule_source"] == "policy"
    assert status["effective_enabled"] and status["interval_sec"] == 120


def test_other_resource_types_do_not_depend_on_ecs_policy_validity(mem_db):
    mem_db.add(SysSetting(key=policies.policy_key(11), value={"invalid_ecs_setting": True}))
    mem_db.commit()
    ctx = RunContext(client=Client(), account="one", project_id="p1", tenant_id=11, project_ref=101, db=mem_db)
    observations = [Observation("dcs_instance", "cache", "cache", "instance_status", {}, status_raw="RUNNING")]
    pipeline.process_observations(mem_db, "dcs", observations, ctx, now=utcnow())
    assert mem_db.scalar(select(Resource).where(Resource.resource_type == "dcs_instance")).status_raw == "RUNNING"


def policy_alarm(db, host=None, *, check="cpu", status="FIRING", tid=11, rtype="ecs_server", rule_id=None):
    alarm = AlarmEvent(tenant_id=host.tenant_id if host is not None else tid, fingerprint=uuid4().hex,
        rule_id=rule_id or f"policy-{check}", resource_ref=host.id if host is not None else None,
        service="ecs" if rtype == "ecs_server" else "dcs", resource_type=rtype,
        resource_name=host.name if host is not None else "legacy host", check_item=check,
        severity="P2", status=status, title=check or "legacy check", current_value="99%", count=7,
        detail={"original": "history"})
    db.add(alarm)
    db.flush()
    return alarm


@pytest.mark.parametrize("changes,closed_checks", [
    ({"metrics": {"enabled": False}}, {"cpu", "memory", "disk"}),
    ({"metric_keys": ["mem_util", "disk_util", "boot_time"]}, {"cpu"}),
    ({"metric_keys": ["cpu_util", "disk_util", "boot_time"]}, {"memory"}),
    ({"metric_keys": ["cpu_util", "mem_util", "boot_time"]}, {"disk"}),
    ({"metric_keys": ["cpu_util", "mem_util", "disk_util"]}, set()),
    ({"metric_keys": []}, {"cpu", "memory", "disk"}),
])
def test_policy_disables_only_affected_metric_alarms_with_administrative_history(mem_db, changes, closed_checks):
    tenant(mem_db)
    host = resource(mem_db, metrics={"cpu_util": 99})
    alarms = {check: policy_alarm(mem_db, host, check=check, status=status) for check, status in (
        ("cpu", "FIRING"), ("memory", "ACKED"), ("disk", "SUPPRESSED"),
        ("instance_status", "FIRING"), ("reboot", "ACKED"))}
    historical = policy_alarm(mem_db, host, status="RESOLVED")
    already_closed = policy_alarm(mem_db, host, status="CLOSED")
    foreign = policy_alarm(mem_db, resource(mem_db, tid=22, project=202))
    another_type = policy_alarm(mem_db, tid=11, rtype="dcs_instance")
    mem_db.commit()
    before_statuses = {check: alarm.status for check, alarm in alarms.items()}
    prior_fire = AlarmEventLog(alarm_id=alarms["cpu"].id, action="FIRE", to_status="FIRING", operator="system")
    mem_db.add(prior_fire)
    mem_db.commit()
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(**changes), "policy-editor")
    mem_db.commit()
    assert result["closed_alarms"] == len(closed_checks)
    for check, alarm in alarms.items():
        assert alarm.status == ("CLOSED" if check in closed_checks else before_statuses[check])
        assert alarm.current_value == "99%" and alarm.count == 7 and alarm.detail == {"original": "history"}
        assert (alarm.resolved_at is not None) == (check in closed_checks)
    logs = list(mem_db.scalars(select(AlarmEventLog).where(AlarmEventLog.action == "CLOSE")))
    assert {entry.alarm_id for entry in logs} == {alarms[check].id for check in closed_checks}
    assert all(entry.operator == "policy-editor" and entry.to_status == "CLOSED"
               and "非指标恢复" in entry.note for entry in logs)
    assert mem_db.get(AlarmEventLog, prior_fire.id).action == "FIRE"
    assert historical.status == "RESOLVED" and already_closed.status == "CLOSED"
    assert foreign.status == another_type.status == "FIRING"
    assert host.metrics == {"cpu_util": 99}
    assert mem_db.scalar(select(func.count()).select_from(Notification)) == 0
    assert policies.save(mem_db, 11, "ecs_server", CollectionPolicy(**changes), "policy-editor")["closed_alarms"] == 0


@pytest.mark.parametrize("mode", ["critical", "selected"])
def test_narrowed_selection_closes_only_excluded_local_resource_alarms(mem_db, mode):
    tenant(mem_db)
    first = resource(mem_db, critical=True)
    # Same cloud ID in another project must be distinguished by local Resource.id.
    second = resource(mem_db, project=102, critical=False)
    foreign_host = resource(mem_db, tid=22, project=202, critical=False)
    checks = ("cpu", "instance_status", "reboot")
    first_alarms = [policy_alarm(mem_db, first, check=check) for check in checks]
    second_alarms = [policy_alarm(mem_db, second, check=check) for check in checks]
    foreign = [policy_alarm(mem_db, foreign_host, check=check) for check in checks]
    mem_db.commit()
    included, excluded = (first_alarms, second_alarms) if mode == "critical" else (second_alarms, first_alarms)
    selection = {"mode": mode, "resource_ids": [second.id] if mode == "selected" else []}
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(selection=selection), "scope-editor")
    mem_db.commit()
    assert result["closed_alarms"] == len(excluded)
    assert all(alarm.status == "CLOSED" for alarm in excluded)
    assert all(alarm.status == "FIRING" for alarm in [*included, *foreign])
    logs = list(mem_db.scalars(select(AlarmEventLog)))
    assert {entry.alarm_id for entry in logs} == {alarm.id for alarm in excluded}
    assert all(entry.action == "CLOSE" and "监控范围" in entry.note and "非指标恢复" in entry.note for entry in logs)


def test_selection_and_metric_disable_close_union_and_preserve_selected_status_and_reboot(mem_db):
    tenant(mem_db)
    included = resource(mem_db)
    excluded = resource(mem_db, rid="excluded")
    selected_alarms = {check: policy_alarm(mem_db, included, check=check)
                       for check in ("cpu", "instance_status", "reboot")}
    excluded_alarms = [policy_alarm(mem_db, excluded, check=check) for check in ("cpu", "instance_status", "reboot")]
    mem_db.commit()
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(metrics={"enabled": False},
        selection={"mode": "selected", "resource_ids": [included.id]}), "editor")
    mem_db.commit()
    assert result["closed_alarms"] == 4
    assert selected_alarms["cpu"].status == "CLOSED"
    assert selected_alarms["instance_status"].status == selected_alarms["reboot"].status == "FIRING"
    assert all(alarm.status == "CLOSED" for alarm in excluded_alarms)
    assert mem_db.scalar(select(func.count()).select_from(AlarmEventLog)) == 4


def test_legacy_alarm_missing_check_uses_rule_check_without_misclassifying_other_rules(mem_db):
    tenant(mem_db)
    host = resource(mem_db)
    for rule_id, rtype, check in (("legacy-cpu", "ecs_server", "cpu"),
                                 ("legacy-status", "ecs_server", "instance_status"),
                                 ("foreign-rule", "dcs_instance", "cpu")):
        mem_db.add(AlarmRule(id=rule_id, name=rule_id, resource_type=rtype, check_item=check,
                            kind="state", expr={"when": "True", "severity": "P2"}, recover={}))
    mem_db.flush()
    cpu = policy_alarm(mem_db, host, check="", rule_id="legacy-cpu")
    state = policy_alarm(mem_db, host, check="", rule_id="legacy-status")
    unknown = policy_alarm(mem_db, host, check="", rule_id="missing-rule")
    other_rule = policy_alarm(mem_db, host, check="", rule_id="foreign-rule")
    mem_db.commit()
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(metrics={"enabled": False}), "editor")
    mem_db.commit()
    assert result["closed_alarms"] == 1 and cpu.status == "CLOSED"
    assert state.status == unknown.status == other_rule.status == "FIRING"


def test_transient_metric_failure_and_non_disabling_policy_edits_never_close_alarms(mem_db, monkeypatch):
    tenant(mem_db)
    host = resource(mem_db)
    existing = policy_alarm(mem_db, host)
    store(mem_db)

    def transient_failure(ctx):
        ctx.errors.append("synthetic transient metric failure")
        return []

    monkeypatch.setitem(pipeline.TASKS, "ecs_metrics", SimpleNamespace(service="ecs", collect=transient_failure))
    assert pipeline.run_task(mem_db, "ecs_metrics", client=Client(), target=TARGET, trigger="MANUAL").status == "PARTIAL"
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(metrics={"interval_sec": 600},
        stale_after_sec=1800, inventory={"enabled": False}, quota={"enabled": False}), "editor")
    mem_db.commit()
    assert result["closed_alarms"] == 0 and existing.status == "FIRING" and existing.resolved_at is None
    assert mem_db.scalar(select(AlarmEventLog)) is None


def test_administrative_alarm_closure_and_policy_write_roll_back_together(mem_db):
    tenant(mem_db)
    host = resource(mem_db)
    existing = policy_alarm(mem_db, host)
    store(mem_db)
    result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(metrics={"enabled": False}), "editor")
    assert result["closed_alarms"] == 1
    mem_db.rollback()
    assert existing.status == "FIRING" and existing.resolved_at is None
    assert policies.effective(mem_db, 11).metrics.enabled
    assert mem_db.scalar(select(AlarmEventLog)) is None


def test_affected_alarm_lookup_is_batched_without_per_resource_queries(mem_db):
    tenant(mem_db)
    hosts = [Resource(tenant_id=11, project_ref=101, service="ecs", resource_type="ecs_server",
                      resource_id=f"batch-{index}", name=f"batch-{index}") for index in range(600)]
    mem_db.add_all(hosts)
    mem_db.flush()
    alarms = [AlarmEvent(tenant_id=11, fingerprint=uuid4().hex, rule_id="policy-cpu", resource_ref=host.id,
        service="ecs", resource_type="ecs_server", resource_name=host.name, check_item="cpu",
        severity="P2", status="FIRING", title="CPU") for host in hosts]
    mem_db.add_all(alarms)
    mem_db.commit()
    mem_db.expire_all()
    reads = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    event.listen(mem_db.get_bind(), "before_cursor_execute", record)
    try:
        result = policies.save(mem_db, 11, "ecs_server", CollectionPolicy(
            metric_keys=["mem_util", "disk_util", "boot_time"]), "editor")
        mem_db.commit()
    finally:
        event.remove(mem_db.get_bind(), "before_cursor_execute", record)
    assert result["closed_alarms"] == 600
    assert len(reads) <= 8  # 固定一次租户锁读，用于在批量告警关闭前序列化策略变更。
    assert len([statement for statement in reads if "FROM alarm_event " in statement]) == 2
    assert all(alarm.status == "CLOSED" for alarm in alarms)
    assert mem_db.scalar(select(func.count()).select_from(AlarmEventLog)) == 600


@pytest.mark.parametrize("raw", [{}, {"namespace": "OLD"}, "invalid", [], False, 0,
    {"namespace": "CUSTOM", "dimension": "instance_id", "metrics": {"cpu_util": "cpu"}}])
def test_saved_aom_policy_is_read_without_validation_error_or_database_rewrite(mem_db, raw):
    value = {**CollectionPolicy().model_dump(mode="json"), "aom": raw,
             "metrics": {"enabled": False, "interval_sec": 180},
             "prometheus": {"queries": {"cpu_util": "up"}, "instance_label": "instance"}}
    row = SysSetting(key=policies.policy_key(11), value=value)
    mem_db.add(row)
    mem_db.commit()
    before = row.updated_at
    for _ in range(2):
        effective = policies.effective(mem_db, 11)
        assert effective.aom is None and effective.prometheus.queries == {"cpu_util": "up"}
        assert not effective.metrics.enabled and effective.metrics.interval_sec == 180
    mem_db.refresh(row)
    assert row.value == value and row.updated_at == before
    assert not mem_db.dirty


@pytest.mark.parametrize("raw", [{}, {"namespace": "OLD"}, "invalid", [], False, 0,
    {"namespace": "CUSTOM", "dimension": "instance_id", "metrics": {"cpu_util": "cpu"}}])
def test_new_non_null_aom_policy_rejected_by_schema_and_api_without_writes(mem_db, policy_api, raw):
    with pytest.raises(ValidationError):
        CollectionPolicy(aom=raw)
    tenant(mem_db)
    api, _user = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    before = list(mem_db.scalars(select(SysSetting.key)))
    response = api.put(path, json={"aom": raw})
    assert response.status_code == 422, response.text
    assert list(mem_db.scalars(select(SysSetting.key))) == before
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_api_reads_old_aom_and_accepts_null_to_clear_explicitly(mem_db, policy_api):
    tenant(mem_db)
    raw = {**CollectionPolicy().model_dump(mode="json"), "aom": {"namespace": "OLD"}}
    row = SysSetting(key=policies.policy_key(11), value=raw)
    mem_db.add(row)
    mem_db.commit()
    api, _user = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    response = api.get(path)
    assert response.status_code == 200 and response.json()["data"]["policy"]["aom"] is None
    mem_db.refresh(row)
    assert row.value == raw
    response = api.put(path, json={"aom": None, "metrics": {"interval_sec": 180}})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["policy"]["aom"] is None
    mem_db.refresh(row)
    assert row.value["aom"] is None and row.value["metrics"]["interval_sec"] == 180


@pytest.mark.parametrize("raw", [{}, {"namespace": "OLD"}, "invalid", [], False, 0])
def test_invalid_legacy_task_aom_never_blocks_valid_prometheus_or_phase_defaults(mem_db, raw):
    mem_db.add_all([
        InspectionTask(code="ecs_metrics", name="metrics", service="ecs", interval_sec=360,
                       params={"aom": raw, "prometheus": {"cpu_util": "up"}}),
        InspectionTask(code="ecs_status", name="status", service="ecs", interval_sec=180,
                       params={"aom": raw}),
    ])
    mem_db.commit()
    for tid in (11, 22):
        effective = policies.effective(mem_db, tid)
        assert effective.aom is None and effective.prometheus.queries == {"cpu_util": "up"}
        assert effective.metrics.interval_sec == 360 and effective.inspection.interval_sec == 180
    assert mem_db.get(InspectionTask, "ecs_metrics").params["aom"] == raw
    assert mem_db.get(InspectionTask, "ecs_status").params["aom"] == raw
    assert mem_db.scalar(select(func.count()).select_from(SysSetting)) == 0
