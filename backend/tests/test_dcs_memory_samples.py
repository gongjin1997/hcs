"""本地 DCS 内存历史：只用独立数据库及进程内 HCS Mock，不读取真实部署配置。"""
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.collectors.base import Observation, RunContext
from app.models import CollectSwitch, DcsMemorySample, Resource
from app.services import cleanup, pipeline
from app.services.pipeline import _dcs_memory_values, process_observations, run_task
from app.services.tenancy import resolve_targets

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def context(tenant_id=101, project_ref=1001):
    return RunContext(client=None, account=str(tenant_id), project_id=str(project_ref),
                      tenant_id=tenant_id, project_ref=project_ref)


def memory(resource_id="shared", metrics=None, check_item="memory", resource_type="dcs_instance"):
    return Observation(resource_type, resource_id, resource_id, check_item, {},
                       metrics={"memory_usage": 25.0, "used_memory_mb": 25, "max_memory_mb": 100}
                       if metrics is None else metrics)


def samples(db):
    return list(db.scalars(select(DcsMemorySample).order_by(DcsMemorySample.id)))


def resource(db, resource_id="retention", **kwargs):
    row = Resource(service="dcs", resource_type="dcs_instance", resource_id=resource_id, name=resource_id,
                   **kwargs)
    db.add(row)
    db.flush()
    return row


def sample(db, row, at):
    db.add(DcsMemorySample(resource_ref=row.id, tenant_id=row.tenant_id, project_ref=row.project_ref,
                           observed_at=at, memory_usage=25))
    db.flush()


def test_run_task_collects_real_mock_readings_at_response_time(world, mem_db, mock_http, monkeypatch):
    target = next(t for t in resolve_targets(mem_db) if t.code == "tenant-a")
    client = target.client(http=mock_http, mock_url="http://mock")
    clock = [NOW]
    monkeypatch.setattr(pipeline, "utcnow", lambda: clock[0])
    monkeypatch.setattr("app.collectors.dcs.utcnow", lambda: clock[0] + timedelta(seconds=5))
    run = run_task(mem_db, "dcs_status", client=client, target=target)
    assert run.status == "SUCCESS"
    first = samples(mem_db)
    assert first
    for point in first:
        row = mem_db.get(Resource, point.resource_ref)
        assert row.resource_type == "dcs_instance"
        assert point.tenant_id == target.tenant_id and point.project_ref == target.project_ref
        assert point.observed_at == NOW + timedelta(seconds=5)
        assert point.memory_usage == row.metrics["memory_usage"]
        assert point.used_memory_mb == row.metrics["used_memory_mb"]
        assert point.max_memory_mb == row.metrics["max_memory_mb"]
    clock[0] += timedelta(minutes=1)
    assert run_task(mem_db, "dcs_status", client=client, target=target).status == "SUCCESS"
    assert len(samples(mem_db)) == len(first) * 2
    assert run_task(mem_db, "dcs_status", client=client, target=target).status == "SUCCESS"
    assert len(samples(mem_db)) == len(first) * 2


def test_same_cloud_id_is_isolated_across_tenants_projects_and_legacy(mem_db):
    for tenant, project, usage in ((101, 1001, 11), (202, 2002, 22), (101, 1002, 33), (None, None, 44)):
        process_observations(mem_db, "dcs", [memory(metrics={"memory_usage": usage})], context(tenant, project), NOW)
    points = samples(mem_db)
    assert len({point.resource_ref for point in points}) == 4
    assert {(p.tenant_id, p.project_ref, p.memory_usage) for p in points} == {
        (101, 1001, 11), (202, 2002, 22), (101, 1002, 33), (None, None, 44)}
    for point in points:
        row = mem_db.get(Resource, point.resource_ref)
        assert (row.tenant_id, row.project_ref) == (point.tenant_id, point.project_ref)


def test_only_current_memory_metrics_create_history_not_facts_or_other_checks(mem_db):
    process_observations(mem_db, "dcs", [memory()], context(), NOW)
    next_time = NOW + timedelta(minutes=1)
    only_facts = Observation("dcs_instance", "shared", "shared", "memory", {"memory_usage": 80}, metrics={})
    cpu = memory(metrics={"memory_usage": 99}, check_item="cpu")
    incomplete = memory(metrics={"used_memory_mb": 10, "max_memory_mb": 100})
    process_observations(mem_db, "dcs", [only_facts, cpu, incomplete], context(), next_time)
    assert len(samples(mem_db)) == 1
    assert mem_db.scalar(select(Resource)).metrics["memory_usage"] == 99
    process_observations(mem_db, "ecs", [memory("ecs", resource_type="ecs_server")], context(), next_time)
    assert len(samples(mem_db)) == 1


def test_dedupes_checks_retries_and_equivalent_utc_instants(mem_db):
    process_observations(mem_db, "dcs", [memory(), memory(), memory(check_item="instance_status")], context(), NOW)
    same_in_japan = NOW.astimezone(timezone(timedelta(hours=9)))
    process_observations(mem_db, "dcs", [memory(metrics={"memory_usage": 75})], context(), same_in_japan)
    assert len(samples(mem_db)) == 1 and samples(mem_db)[0].memory_usage == 25
    process_observations(mem_db, "dcs", [memory()], context(), NOW + timedelta(seconds=1))
    assert len(samples(mem_db)) == 2


@pytest.mark.parametrize("metrics", [
    {}, {"memory_usage": None}, {"memory_usage": ""}, {"memory_usage": "25"}, {"memory_usage": True},
    {"memory_usage": float("nan")}, {"memory_usage": float("inf")}, {"memory_usage": -float("inf")},
    {"memory_usage": -0.1}, {"memory_usage": 10 ** 1000},
    {"memory_usage": 25, "used_memory_mb": float("nan")},
    {"memory_usage": 25, "used_memory_mb": float("inf")},
    {"memory_usage": 25, "used_memory_mb": -1},
    {"memory_usage": 25, "used_memory_mb": "25"},
    {"memory_usage": 25, "max_memory_mb": float("nan")},
    {"memory_usage": 25, "max_memory_mb": float("inf")},
    {"memory_usage": 25, "max_memory_mb": 0},
    {"memory_usage": 25, "max_memory_mb": -1},
    {"memory_usage": 25, "max_memory_mb": False},
    {"used_memory_mb": 1, "max_memory_mb": 100},
])
def test_invalid_or_incomplete_metrics_are_not_coerced_to_zero(metrics):
    assert _dcs_memory_values(metrics) is None


@pytest.mark.parametrize("usage", [0, 100, 100.1, 250])
def test_real_zero_and_full_usage_are_valid_and_missing_mb_stays_null(mem_db, usage):
    process_observations(mem_db, "dcs", [memory(metrics={"memory_usage": usage})], context(), NOW)
    point, = samples(mem_db)
    assert point.memory_usage == usage
    assert point.used_memory_mb is None and point.max_memory_mb is None


def test_over_capacity_reading_is_preserved_without_clamping(mem_db):
    process_observations(mem_db, "dcs", [memory(metrics={
        "memory_usage": 125.5, "used_memory_mb": 125.5, "max_memory_mb": 100,
    })], context(), NOW)
    point, = samples(mem_db)
    assert (point.memory_usage, point.used_memory_mb, point.max_memory_mb) == (125.5, 125.5, 100)


@pytest.mark.parametrize("disabled_by", ["global", "tenant", "monitored", "deleted"])
def test_disabled_collection_or_resource_does_not_write_history(mem_db, disabled_by):
    if disabled_by in {"global", "tenant"}:
        mem_db.add(CollectSwitch(tenant_id=0 if disabled_by == "global" else 101,
                                 resource_type="dcs_instance", enabled=False))
        mem_db.commit()
    else:
        row = resource(mem_db, "shared", tenant_id=101, project_ref=1001)
        row.monitored = disabled_by != "monitored"
        row.lifecycle = "DELETED" if disabled_by == "deleted" else "ACTIVE"
        mem_db.commit()
    process_observations(mem_db, "dcs", [memory()], context(), NOW)
    assert samples(mem_db) == []


def test_tenant_enable_overrides_global_disable(mem_db):
    mem_db.add_all([CollectSwitch(tenant_id=0, resource_type="dcs_instance", enabled=False),
                    CollectSwitch(tenant_id=101, resource_type="dcs_instance", enabled=True)])
    mem_db.commit()
    process_observations(mem_db, "dcs", [memory()], context(), NOW)
    process_observations(mem_db, "dcs", [memory()], context(202, 2002), NOW)
    assert len(samples(mem_db)) == 1 and samples(mem_db)[0].tenant_id == 101


def test_invalid_values_do_not_reuse_the_previous_valid_snapshot(mem_db):
    process_observations(mem_db, "dcs", [memory()], context(), NOW)
    process_observations(mem_db, "dcs", [memory(metrics={"memory_usage": None})], context(), NOW + timedelta(minutes=1))
    process_observations(mem_db, "dcs", [memory(metrics={})], context(), NOW + timedelta(minutes=2))
    assert len(samples(mem_db)) == 1


def test_database_unique_index_and_resource_cascade(mem_db):
    if mem_db.get_bind().dialect.name == "sqlite":
        mem_db.execute(text("PRAGMA foreign_keys=ON"))
        mem_db.commit()
    row = resource(mem_db)
    sample(mem_db, row, NOW)
    mem_db.commit()
    with pytest.raises(IntegrityError), mem_db.begin_nested():
        sample(mem_db, row, NOW)
    mem_db.delete(row)
    mem_db.commit()
    assert mem_db.scalar(select(func.count()).select_from(DcsMemorySample)) == 0


def test_cleanup_fixed_90_days_boundary_and_resource_removal(mem_db):
    active = resource(mem_db, tenant_id=101, project_ref=1001)
    sample(mem_db, active, NOW - timedelta(days=90, microseconds=1))
    sample(mem_db, active, NOW - timedelta(days=90))
    sample(mem_db, active, NOW)
    released = resource(mem_db, "released", lifecycle="DELETED", deleted_at=NOW - timedelta(days=400))
    sample(mem_db, released, NOW)
    recent_release = resource(mem_db, "recent-release", lifecycle="DELETED", deleted_at=NOW - timedelta(days=1))
    sample(mem_db, recent_release, NOW)
    mem_db.commit()
    assert cleanup.DCS_MEMORY_RETENTION_DAYS == 90
    result = cleanup.run_cleanup(mem_db, NOW)
    assert result["dcs_memory_sample"] == 2 and result["resource"] == 1
    assert {point.resource_ref for point in samples(mem_db)} == {active.id, recent_release.id}
    assert len(samples(mem_db)) == 3
    assert cleanup.run_cleanup(mem_db, NOW)["dcs_memory_sample"] == 0


def test_cleanup_batches_expired_history(mem_db, monkeypatch):
    row = resource(mem_db)
    for day in range(91, 96):
        sample(mem_db, row, NOW - timedelta(days=day))
    sample(mem_db, row, NOW)
    mem_db.commit()
    monkeypatch.setattr(cleanup, "BATCH", 2)
    assert cleanup.run_cleanup(mem_db, NOW)["dcs_memory_sample"] == 5
    assert len(samples(mem_db)) == 1


def test_cleanup_removes_orphans_when_sqlite_foreign_keys_are_disabled(mem_db):
    if mem_db.get_bind().dialect.name != "sqlite":
        pytest.skip("PostgreSQL 由外键级联处理；本例验证 SQLite 未启用外键时的兼容清理")
    mem_db.execute(text("PRAGMA foreign_keys=OFF"))
    mem_db.commit()
    row = resource(mem_db)
    sample(mem_db, row, NOW)
    mem_db.commit()
    mem_db.delete(row)
    mem_db.commit()
    assert len(samples(mem_db)) == 1
    assert cleanup.run_cleanup(mem_db, NOW)["dcs_memory_sample"] == 1
    assert samples(mem_db) == []


def test_history_writes_are_batched_without_per_resource_queries(mem_db):
    observations = [memory(str(i)) for i in range(200)]
    process_observations(mem_db, "dcs", observations, context(), NOW)
    statements = []
    def count(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(mem_db.get_bind(), "before_cursor_execute", count)
    try:
        process_observations(mem_db, "dcs", observations, context(), NOW + timedelta(minutes=1))
    finally:
        event.remove(mem_db.get_bind(), "before_cursor_execute", count)
    assert len(samples(mem_db)) == 400
    assert len(statements) <= 30
