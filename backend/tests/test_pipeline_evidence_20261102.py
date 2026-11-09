"""纯本地流水线证据：缺采隔离、旧状态升级、duration 验证与实际调度周期。"""
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.alarms.fingerprint import fingerprint
from app.collectors.base import Observation, RunContext
from app.core.config import get_settings
from app.models import AlarmEvent, AlarmRule, EvalState, InspectionTask, Resource, SysSetting
from app.schemas.collection_policy import CollectionPolicy
from app.services import collection_policy, pipeline
from app.services.inspection_health import current_health
from app.services.tenancy import Target

NOW = datetime(2026, 11, 2, 3, tzinfo=UTC)


def ctx(tenant=1, project=11, task="dgc_realtime_status", **params):
    return RunContext(client=Mock(), account=f"tenant-{tenant}", project_id=f"project-{project}",
                      tenant_id=tenant, project_ref=project,
                      params={"_task_code": task, "_inspection_stale_after_sec": 900, **params})


def add_rule(db, code="local-job", *, check="job_status", kind="dgc_realtime_job", when="status == 'EXCEPTION'", count=2):
    row = AlarmRule(id=code, name=code, resource_type=kind, check_item=check, kind="state", for_count=count,
                    expr={"when": when, "severity": "P3"}, recover={"when": "status == 'NORMAL'", "for_count": 2})
    db.add(row)
    db.commit()
    return row


def bad(rid="job"):
    return Observation("dgc_realtime_job", rid, rid, "job_status", {"status": "EXCEPTION"}, status_raw="EXCEPTION")


def key(context, rule="local-job", rid="job", sub="", kind="dgc_realtime_job"):
    return fingerprint(context.account, context.project_id, kind, rid, rule, sub)


def test_empty_task_breaks_only_its_own_tenant_project_and_task(mem_db):
    add_rule(mem_db)
    add_rule(mem_db, "local-other", check="other_status")
    contexts = [ctx(), ctx(project=12), ctx(tenant=2, project=21), ctx(task="other-task")]
    for context in contexts:
        observation = bad("other") if context.params["_task_code"] == "other-task" else bad()
        if observation.resource_id == "other":
            observation.check_item = "other_status"
        pipeline.process_observations(mem_db, "dataarts", [observation], context, NOW)
    snapshots = {row.fingerprint: dict(row.data) for row in mem_db.scalars(select(EvalState))}
    pipeline.process_observations(mem_db, "dataarts", [], contexts[0], NOW + timedelta(minutes=5))
    assert mem_db.get(EvalState, key(contexts[0])).data["hits"] == 0
    for context in contexts[1:]:
        fp = key(context, "local-other", "other") if context.params["_task_code"] == "other-task" else key(context)
        assert mem_db.get(EvalState, fp).data == snapshots[fp]
    own = mem_db.scalar(select(Resource).where(Resource.tenant_id == 1, Resource.project_ref == 11,
                                               Resource.resource_id == "job"))
    assert current_health(own, NOW + timedelta(minutes=5)) == "UNKNOWN"
    assert mem_db.scalars(select(AlarmEvent)).all() == []


def test_empty_legacy_task_resets_only_recoverable_scoped_fingerprints(mem_db):
    add_rule(mem_db)
    add_rule(mem_db, "local-node", check="node_status", when="node_status == 'EXCEPTION'")
    own, foreign = ctx(), ctx(project=12)
    for context in (own, foreign):
        mem_db.add(Resource(tenant_id=context.tenant_id, project_ref=context.project_ref,
                            service="dataarts", resource_type="dgc_realtime_job", resource_id="job", name="job",
                            attrs={"nodes": [{"name": "node-a", "status": "EXCEPTION"}]}))
        for rule, sub in (("local-job", ""), ("local-node", "node-a")):
            mem_db.add(EvalState(fingerprint=key(context, rule, sub=sub), data={"hits": 1, "ok_hits": 1}))
    mem_db.commit()
    pipeline.process_observations(mem_db, "dataarts", [], own, NOW)
    for rule, sub in (("local-job", ""), ("local-node", "node-a")):
        assert mem_db.get(EvalState, key(own, rule, sub=sub)).data["hits"] == 0
        assert mem_db.get(EvalState, key(own, rule, sub=sub)).data["ok_hits"] == 0
        assert mem_db.get(EvalState, key(foreign, rule, sub=sub)).data == {"hits": 1, "ok_hits": 1}


def test_unattributable_legacy_counter_restarts_once_then_can_accumulate(mem_db):
    add_rule(mem_db)
    context = ctx()
    mem_db.add(EvalState(fingerprint=key(context), data={"hits": 1, "ok_hits": 0}))
    mem_db.commit()
    pipeline.process_observations(mem_db, "dataarts", [bad()], context, NOW)
    assert mem_db.scalars(select(AlarmEvent)).all() == []
    assert mem_db.get(EvalState, key(context)).data["hits"] == 1
    pipeline.process_observations(mem_db, "dataarts", [bad()], context, NOW + timedelta(minutes=1))
    assert len(mem_db.scalars(select(AlarmEvent)).all()) == 1


@pytest.mark.parametrize("duration,expected", [(0, False), (2.5, True), (False, False), (-1, False),
                                               (None, False), (float("nan"), False), (float("inf"), False), ("20", False)])
def test_explicit_duration_must_be_finite_nonnegative_number(mem_db, duration, expected):
    add_rule(mem_db, when="status == 'EXCEPTION' and status_duration_min > 1", count=1)
    observation = bad()
    observation.facts["status_duration_min"] = duration
    pipeline.process_observations(mem_db, "dataarts", [observation], ctx(), NOW)
    assert bool(mem_db.scalars(select(AlarmEvent)).all()) is expected


def test_node_duration_survives_unrelated_parent_status_change(mem_db):
    add_rule(mem_db, "local-node", check="node_status", when="node_status == 'STARTING' and status_duration_min > 10", count=1)
    context = ctx()
    parent = bad()
    parent.facts = {"status": "NORMAL"}
    parent.status_raw = "NORMAL"
    node = Observation("dgc_realtime_job", "job", "job", "node_status", {"node_status": "STARTING"}, sub_object="node")
    pipeline.process_observations(mem_db, "dataarts", [parent, node], context, NOW)
    parent.facts = {"status": "EXCEPTION"}
    parent.status_raw = "EXCEPTION"
    pipeline.process_observations(mem_db, "dataarts", [parent, node], context, NOW + timedelta(minutes=12))
    assert len(mem_db.scalars(select(AlarmEvent)).all()) == 1


def test_execution_identity_change_resets_hits_even_if_state_is_unchanged(mem_db):
    add_rule(mem_db, kind="dgc_mrs_node", check="job_status", when="node_status == 'waiting'")
    context = ctx(task="dgc_mrs_node_status")
    for offset, execution in ((0, "one"), (1, "two")):
        observation = Observation("dgc_mrs_node", "ws/job/node", "job", "job_status", {"node_status": "waiting"},
                                  status_raw="waiting", attrs={"instance_id": execution})
        pipeline.process_observations(mem_db, "mrs", [observation], context, NOW + timedelta(minutes=offset))
    assert mem_db.scalars(select(AlarmEvent)).all() == []
    resource = mem_db.scalar(select(Resource))
    assert resource.status_since == NOW + timedelta(minutes=1)


@pytest.mark.parametrize("task,cron,policy,expected", [
    ("dgc_realtime_status", "0 3 * * *", None, 259200),
    ("ecs_status", "0 3 * * *", None, 259200),
    ("ecs_status", "0 3 * * *", {"inspection": {"interval_sec": 1200}, "stale_after_sec": 60}, 3600),
])
def test_run_context_freshness_uses_real_cron_or_tenant_inspection_schedule(mem_db, monkeypatch, task, cron, policy, expected):
    from app.collectors.registry import TASKS

    captured = []

    class Collector:
        code, service = task, "ecs" if task == "ecs_status" else "dataarts"

        def collect(self, context):
            captured.append(context)
            return []

    mem_db.add(InspectionTask(code=task, name=task, service=Collector.service, interval_sec=120, cron=cron))
    if policy is not None:
        mem_db.add(SysSetting(key=collection_policy.policy_key(1),
                              value=CollectionPolicy.model_validate(policy).model_dump(mode="json")))
    mem_db.commit()
    monkeypatch.setitem(TASKS, task, Collector())
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW)
    monkeypatch.setattr(get_settings(), "timezone", "UTC")
    client = Mock(calls=0)
    client.metrics_snapshot.return_value = {}
    target = Target(tenant_id=1, code="tenant-1", name="one", short_name="one", project_ref=11,
                    project_id="project-11", project_name="project", region="synthetic", workspaces=[])
    result = pipeline._run_task(mem_db, task, client=client, target=target, trigger="MANUAL")
    assert result.status == "SUCCESS"
    assert captured[0].params["_inspection_stale_after_sec"] == expected
    if policy is not None:
        assert captured[0].params["_collection_policy"]["stale_after_sec"] == 60


def target(project=11):
    return Target(tenant_id=1, code="tenant-1", name="one", short_name="one", project_ref=project,
                  project_id=f"project-{project}", project_name="project", region="synthetic", workspaces=[])


def test_collection_failure_breaks_counts_and_preserves_existing_alarm(mem_db, monkeypatch):
    from app.collectors.registry import TASKS
    from app.hcs import HcsError

    add_rule(mem_db, count=1)
    pipeline.process_observations(mem_db, "dataarts", [bad()], ctx(), NOW)

    class FailedCollector:
        code, service = "dgc_realtime_status", "dataarts"

        def collect(self, context):
            raise HcsError(503, "SYNTHETIC", "local injected failure")

    monkeypatch.setitem(TASKS, "dgc_realtime_status", FailedCollector())
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW + timedelta(minutes=5))
    client = Mock(calls=0)
    client.metrics_snapshot.return_value = {}
    run = pipeline._run_task(mem_db, "dgc_realtime_status", client=client, target=target(), trigger="MANUAL")
    assert run.status == "FAILED" and run.project_ref == 11
    assert mem_db.get(EvalState, key(ctx())).data["hits"] == 0
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"
    resource = mem_db.scalar(select(Resource))
    assert resource.health == "WARNING" and resource.last_seen == NOW


def test_run_all_client_initialization_failure_only_interrupts_that_project(mem_db, monkeypatch):
    from app.hcs import HcsError

    add_rule(mem_db)
    for context in (ctx(), ctx(project=12)):
        pipeline.process_observations(mem_db, "dataarts", [bad()], context, NOW)
    monkeypatch.setattr(pipeline, "resolve_targets", lambda *args: [target()])
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW + timedelta(minutes=5))

    def unavailable(*args, **kwargs):
        raise HcsError(401, "SYNTHETIC", "local factory failure")

    monkeypatch.setattr(Target, "client", unavailable)
    runs = pipeline.run_all(mem_db, "dgc_realtime_status")
    assert len(runs) == 1 and runs[0].project_ref == 11 and runs[0].status == "FAILED"
    assert mem_db.get(EvalState, key(ctx())).data["hits"] == 0
    assert mem_db.get(EvalState, key(ctx(project=12))).data["hits"] == 1


def test_not_due_ecs_task_skip_records_project_without_interrupting_data(mem_db, monkeypatch):
    monkeypatch.setattr(collection_policy, "claim", lambda *args, **kwargs: None)
    run = pipeline.run_task(mem_db, "ecs_status", client=Mock(), target=target())
    assert run.status == "SKIPPED" and run.project_ref == 11


def test_disabled_collection_skip_records_project(mem_db):
    from app.models import CollectSwitch

    mem_db.add(CollectSwitch(tenant_id=1, resource_type="dgc_realtime_job", enabled=False))
    mem_db.commit()
    run = pipeline._run_task(mem_db, "dgc_realtime_status", client=Mock(), target=target(), trigger="MANUAL")
    assert run.status == "SKIPPED" and run.project_ref == 11


def test_global_cleanup_has_no_project(mem_db, monkeypatch):
    monkeypatch.setattr(pipeline, "run_cleanup", lambda db, trigger: {})
    run = pipeline._run_cleanup_task(mem_db, "MANUAL")
    assert run.project_ref is None


def test_unknown_source_cannot_recover_even_a_preserved_custom_rule(mem_db):
    saved = add_rule(mem_db, kind="dgc_mrs_node", when="node_status == 'waiting' and status_duration_min > 15", count=1)
    saved.recover = {"when": "node_status != 'waiting'"}
    saved.updated_by = "operator"
    mem_db.commit()
    context = ctx(task="dgc_mrs_node_status")
    observation = Observation("dgc_mrs_node", "ws/job/node", "job", "job_status",
                              {"node_status": "waiting", "status_duration_min": 20},
                              status_raw="waiting", attrs={"instance_id": "one"})
    pipeline.process_observations(mem_db, "mrs", [observation], context, NOW)
    observation.facts, observation.status_raw = {"node_status": "FUTURE_STATE"}, "FUTURE_STATE"
    pipeline.process_observations(mem_db, "mrs", [observation], context, NOW + timedelta(minutes=1))
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"
    assert saved.recover == {"when": "node_status != 'waiting'"} and saved.updated_by == "operator"


def test_scope_uses_stable_local_identity_after_account_or_project_label_change(mem_db):
    add_rule(mem_db)
    context = ctx()
    pipeline.process_observations(mem_db, "dataarts", [bad()], context, NOW)
    previous = key(context)
    context.account, context.project_id = "renamed-account", "project-alias"
    pipeline.process_observations(mem_db, "dataarts", [], context, NOW + timedelta(minutes=1))
    assert mem_db.get(EvalState, previous).data["hits"] == 0


def test_null_local_project_uses_external_identity_without_touching_peer_project(mem_db):
    from copy import deepcopy

    add_rule(mem_db)
    own, peer = ctx(), ctx(project=12)
    own.project_ref = peer.project_ref = None
    pipeline.process_observations(mem_db, "dataarts", [bad("own")], own, NOW)
    pipeline.process_observations(mem_db, "dataarts", [bad("peer")], peer, NOW)
    peer_state = deepcopy(mem_db.get(EvalState, key(peer, rid="peer")).data)
    peer_resource = mem_db.scalar(select(Resource).where(Resource.resource_id == "peer"))
    peer_attrs = deepcopy(peer_resource.attrs)
    pipeline.process_observations(mem_db, "dataarts", [], own, NOW + timedelta(minutes=1))
    assert mem_db.get(EvalState, key(own, rid="own")).data["hits"] == 0
    assert mem_db.get(EvalState, key(peer, rid="peer")).data == peer_state
    assert peer_resource.attrs == peer_attrs


@pytest.mark.parametrize("resource_tenant,alarm_tenant,expected", [
    (1, 2, "OK"), (1, None, "OK"), (None, 1, "OK"), (None, None, "WARNING"), (1, 1, "WARNING")])
def test_refresh_health_only_uses_alarms_in_the_resources_actual_tenant(
        mem_db, resource_tenant, alarm_tenant, expected):
    from uuid import uuid4

    resource = Resource(service="dataarts", resource_type="dgc_realtime_job", resource_id="scoped-health", name="health",
                        tenant_id=resource_tenant, health="MAJOR", attrs={"inspection_monitoring": {
                            "evaluated_at": NOW.isoformat(), "checked_rules": 1, "unavailable_rules": 0,
                            "stale_after_sec": 900, "task_code": "dgc_realtime_status"}})
    mem_db.add(resource)
    mem_db.flush()
    mem_db.add(AlarmEvent(tenant_id=alarm_tenant, resource_ref=resource.id, resource_type=resource.resource_type,
                         resource_name=resource.name, service=resource.service, fingerprint=uuid4().hex,
                         rule_id="scoped-health", severity="P3", status="FIRING", title="synthetic", check_item="job_status"))
    mem_db.commit()
    pipeline.refresh_health(mem_db, [resource], NOW)
    assert resource.health == expected
