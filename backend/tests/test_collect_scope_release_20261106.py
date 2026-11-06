"""采集范围变更期间的在途观测与连续命中隔离。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.collectors.base import Observation, RunContext
from app.models import AlarmEvent, AlarmRule, EvalState
from app.models.base import utcnow
from app.services import collect_scope, pipeline
from app.services.inspection_evidence import META
from app.services.pipeline import process_observations
from app.services.tenancy import Target

from .test_collect_scope import add_resource, exclude, tenant
from .test_worker_business_logging import Client


def context():
    return RunContext(client=None, account="scope-release", project_id="project-101", tenant_id=11,
                      project_ref=101, params={"_task_code": "dcs_status"})


def observation():
    return Observation("dcs_instance", "redis-a", "redis-a", "instance_status", {"status": "ERROR"},
                       status_raw="ERROR")


def setup_rule(db):
    tenant(db)
    resource = add_resource(db, "dcs_instance", "redis-a", "redis-a")
    db.add(AlarmRule(id="R-SCOPE-RELEASE", name="范围连续命中", resource_type="dcs_instance",
                     check_item="instance_status", kind="state", for_count=2,
                     expr={"when": "status == 'ERROR'", "severity": "P2"}, recover={"when": "status == 'RUNNING'"}))
    db.commit()
    return resource


@pytest.mark.parametrize("initially_excluded", [False, True])
def test_final_processing_reads_scope_saved_after_collection_started(mem_db, initially_excluded):
    resource = setup_rule(mem_db)
    initial = [exclude(name="redis-a")] if initially_excluded else []
    collect_scope.save(mem_db, 11, "dcs_instance", initial, "tester")
    mem_db.commit()
    ctx = context()
    ctx.scopes = collect_scope.load(mem_db)
    current = [] if initially_excluded else [exclude(name="redis-a")]
    collect_scope.save(mem_db, 11, "dcs_instance", current, "tester")
    mem_db.commit()

    process_observations(mem_db, "dcs", [observation()], ctx, utcnow())
    mem_db.commit()
    mem_db.refresh(resource)
    assert resource.scope_excluded is not initially_excluded
    assert ctx.stats.get("out_of_scope", 0) == (0 if initially_excluded else 1)
    assert bool(mem_db.scalar(select(EvalState))) is initially_excluded


@pytest.mark.parametrize("changed_tenant", [11, 12])
def test_run_discards_collected_batch_only_when_its_effective_scope_changed(mem_db, monkeypatch, changed_tenant):
    resource = setup_rule(mem_db)
    tenant(mem_db, 12)

    def collect(ctx):
        collect_scope.save(ctx.db, changed_tenant, "dcs_instance", [exclude(name="redis-a")], "tester")
        ctx.db.commit()
        return [observation()]

    monkeypatch.setitem(pipeline.TASKS, "dcs_status", SimpleNamespace(service="dcs", collect=collect))
    target = Target(11, "scope-release", "scope-release", "scope-release", 101, "project-101", "project-101", "")
    run = pipeline.run_task(mem_db, "dcs_status", client=Client(), target=target)
    assert run.status == ("SKIPPED" if changed_tenant == 11 else "SUCCESS")
    if changed_tenant == 11:
        assert run.stats["reason"] == "policy_changed"
    mem_db.refresh(resource)
    assert resource.scope_excluded is (changed_tenant == 11)
    assert bool(mem_db.scalar(select(EvalState))) is (changed_tenant != 11)


@pytest.mark.parametrize("entrypoint", ["save", "refresh", "filter"])
def test_exclusion_interrupts_counts_and_duration_without_touching_other_resources(mem_db, entrypoint):
    resource = setup_rule(mem_db)
    other_project = add_resource(mem_db, "dcs_instance", "redis-a", "redis-a", project=102)
    other_tenant = add_resource(mem_db, "dcs_instance", "redis-a", "redis-a", tid=12, project=201)
    other_kind = add_resource(mem_db, "gaussdb_instance", "redis-a", "redis-a")
    for row in (other_project, other_tenant, other_kind):
        mem_db.add(EvalState(fingerprint=f"unaffected-{row.id}", data={
            "hits": 1, "ok_hits": 1, META: {"resource_ref": row.id, "since": "2026-11-06T00:00:00+00:00"}}))
    mem_db.commit()
    now = utcnow()
    process_observations(mem_db, "dcs", [observation()], context(), now)
    mem_db.commit()
    state = next(s for s in mem_db.scalars(select(EvalState)) if not s.fingerprint.startswith("unaffected-"))
    assert state.data["hits"] == 1
    assert mem_db.scalar(select(AlarmEvent)) is None

    # 限定项目维度，证明相同云资源 ID 的其他项目不被中断。
    rules = [exclude(name="redis-a", business="scope-test")]
    resource.business = "scope-test"
    mem_db.commit()
    if entrypoint == "save":
        collect_scope.save(mem_db, 11, "dcs_instance", rules, "tester")
    else:
        scopes = collect_scope.Scopes(raw={(11, "dcs_instance"): {"rules": rules}})
        if entrypoint == "refresh":
            collect_scope.refresh_resource(mem_db, resource, scopes=scopes)
        else:
            key = ("dcs_instance", "redis-a")
            assert collect_scope.filter_groups(mem_db, 11, {key: [observation()]}, {key: resource}, scopes) == {}
    mem_db.commit()
    mem_db.refresh(state)
    assert state.data["hits"] == state.data["ok_hits"] == 0
    assert state.data[META]["since"] is None
    assert state.data[META]["missing_at"]
    for row in (other_project, other_tenant, other_kind):
        untouched = mem_db.get(EvalState, f"unaffected-{row.id}")
        assert untouched.data["hits"] == untouched.data["ok_hits"] == 1

    collect_scope.save(mem_db, 11, "dcs_instance", [], "tester")
    mem_db.commit()
    process_observations(mem_db, "dcs", [observation()], context(), now + timedelta(seconds=30))
    mem_db.commit()
    assert mem_db.scalar(select(AlarmEvent)) is None
    process_observations(mem_db, "dcs", [observation()], context(), now + timedelta(seconds=60))
    mem_db.commit()
    assert mem_db.scalar(select(AlarmEvent)).status == "FIRING"
