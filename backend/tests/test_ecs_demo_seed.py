"""Demo ECS alarms require distinct synthetic observations through the pipeline."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.collectors import ecs_metrics
from app.core.config import get_settings
from app.demo import seed
from app.metrics.ecs_readings import collect_readings
from app.models import AlarmEvent, AlarmRule, EcsMetricSample, EvalState, HcsAccount, InspectionTask, Resource
from app.models.base import utcnow


def test_demo_ecs_history_has_three_distinct_past_samples_with_scope_and_latest_snapshot(demo_db):
    rows = list(demo_db.scalars(select(EcsMetricSample)))
    assert rows and {row.source for row in rows} == {"PROMETHEUS"}
    times = sorted({row.observed_at for row in rows})
    assert len(times) == 3 and times[1]-times[0] == times[2]-times[1] == timedelta(minutes=1)
    assert all(row.observed_at < row.collected_at and row.period_sec == 60 for row in rows)
    grouped = {}
    for row in rows:
        grouped.setdefault((row.resource_ref, row.metric, row.source), []).append(row)
        resource = demo_db.get(Resource, row.resource_ref)
        assert (row.tenant_id, row.project_ref) == (resource.tenant_id, resource.project_ref)
        assert resource.attrs["ecs_monitoring"]["diagnostic"]["demo_synthetic"] is True
        assert resource.attrs["ecs_monitoring"]["latest"][row.metric]["observed_at"] == times[-1].isoformat()
    assert all(sorted(row.observed_at for row in points) == times for points in grouped.values())
    assert ecs_metrics.collect_readings is collect_readings


def test_demo_cpu_alarm_comes_from_three_distinct_evaluated_samples_not_duplicate_evidence(demo_db):
    resource = demo_db.scalar(select(Resource).where(Resource.name == "ecs-etl-02", Resource.resource_type == "ecs_server"))
    alarm = demo_db.scalar(select(AlarmEvent).where(AlarmEvent.resource_ref == resource.id,
        AlarmEvent.rule_id == "R-ECS-004", AlarmEvent.status == "FIRING"))
    assert alarm is not None
    assert demo_db.get(AlarmRule, "R-ECS-004").for_count == 3
    state = demo_db.get(EvalState, alarm.fingerprint)
    assert state.data["hits"] == 3
    observed = sorted(demo_db.scalars(select(EcsMetricSample.observed_at).where(
        EcsMetricSample.resource_ref == resource.id, EcsMetricSample.metric == "cpu_util")))
    assert len(observed) == len(set(observed)) == 3
    assert state.data["_ecs_metric_observed_at"] == observed[-1].isoformat()


@pytest.mark.parametrize("production,local_mock", [(True, True), (False, False)])
def test_synthetic_ecs_seed_readings_cannot_be_used_for_production_or_nonmock_clients(monkeypatch, production, local_mock):
    monkeypatch.setattr(get_settings(), "env", "prod" if production else "test")
    ctx = SimpleNamespace(client=SimpleNamespace(mock_url=seed.MOCK if local_mock else None), account="tenant-a")
    with pytest.raises(ValueError, match="non-production local demo"):
        seed._demo_ecs_readings(ctx, ["one"], {}, metrics=["cpu_util"], observed_at=utcnow()-timedelta(minutes=1))


def test_demo_seed_does_not_register_aom_endpoints_mappings_or_capabilities(demo_db):
    for tenant in demo_db.scalars(select(HcsAccount)):
        assert tenant.metrics_source != "AOM" and "aom" not in tenant.endpoints and "aom" not in tenant.capabilities
        assert tenant.prometheus_url and tenant.capabilities["prometheus"]["available"] is True
    assert all("aom" not in (task.params or {}) for task in demo_db.scalars(select(InspectionTask)))
