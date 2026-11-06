"""Provider → policy → persistence → API 时间语义；仅内存库，不访问云服务。"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.collectors.base import RunContext
from app.metrics.ecs_readings import EcsReadingsResult
from app.metrics.prometheus import DEFAULT_QUERIES
from app.models import EcsMetricSample, HcsAccount, InspectionTask, Resource, SysSetting
from app.schemas.collection_policy import CollectionPolicy, CollectionPolicyUpdate
from app.services import ecs_monitoring as service
from tests import test_collection_policy as policy_fixtures
from tests import test_ecs_monitoring as fixtures

ecs_world = fixtures.ecs_world
policy_api = policy_fixtures.policy_api
NOW = fixtures.NOW


def mapping():
    return {"queries": {"cpu_util": "measured_cpu_percent"}, "instance_label": "vm_id",
            "units": {"cpu_util": "%"}, "timestamp_queries": {"cpu_util": "measured_cpu_source_seconds"},
            "label_filters": {"project_id": "cloud-project-11", "job": "verified-exporter"}}


def test_policy_round_trip_keeps_provider_configuration():
    raw = mapping()
    assert CollectionPolicy(prometheus=raw).model_dump()["prometheus"] == raw


@pytest.mark.parametrize("field,value", [
    ("units", {"cpu_util": "bytes"}), ("units", {"boot_time": "%"}),
    ("units", {"invented": "%"}), ("timestamp_queries", {"cpu_util": "http://invalid"}),
    ("timestamp_queries", {"cpu_util": "bad\nquery"}), ("timestamp_queries", {"invented": "x"}),
    ("label_filters", {"not-label": "x"}), ("label_filters", {"job": ""}),
    ("label_filters", {"job": "x" * 1025}), ("label_filters", {"job": 1}),
])
def test_invalid_unit_timestamp_or_scope_is_rejected(field, value):
    raw = mapping()
    raw[field] = value
    with pytest.raises(ValidationError):
        CollectionPolicy(prometheus=raw)


def test_custom_query_needs_explicit_unit_built_in_does_not():
    with pytest.raises(ValidationError, match="单位"):
        CollectionPolicyUpdate(prometheus={"queries": {"cpu_util": "measured_cpu_percent"}})
    assert CollectionPolicy(prometheus={"queries": {"cpu_util": DEFAULT_QUERIES["cpu_util"]}})


@pytest.mark.parametrize("field,value", [
    ("units", {"cpu_util": "percent"}),
    ("timestamp_queries", {"cpu_util": "another_source_seconds"}),
    ("label_filters", {"project_id": "cloud-project-12"}),
])
def test_each_mapping_dimension_invalidates_current_snapshot(ecs_world, field, value):
    row, tenant, policy = fixtures.snapshot(ecs_world)
    policy["prometheus"] = mapping()
    signature = service.config_signature(tenant, policy, "PROMETHEUS")
    meta = deepcopy(row.attrs["ecs_monitoring"])
    meta["config_signature"] = signature
    row.attrs = {"ecs_monitoring": meta}
    assert fixtures.out(ecs_world, row, tenant, policy).latest["cpu_util"].value == 40
    policy["prometheus"][field] = value
    assert fixtures.out(ecs_world, row, tenant, policy).latest["cpu_util"].value is None
    assert policy["prometheus"]["timestamp_queries"]
    assert policy["prometheus"]["units"]


def test_evaluation_keeps_real_zero_but_never_claims_physical_sampling(ecs_world):
    row, tenant, policy = fixtures.snapshot(ecs_world, value=0)
    meta = deepcopy(row.attrs["ecs_monitoring"])
    meta["latest"]["cpu_util"]["timestamp_semantics"] = "evaluation"
    row.attrs = {"ecs_monitoring": meta}
    latest = fixtures.out(ecs_world, row, tenant, policy)
    assert latest.latest["cpu_util"].value == 0
    assert latest.latest["cpu_util"].timestamp_semantics == "evaluation"
    assert latest.latest["cpu_util"].unit == "%"
    assert "求值" in latest.diagnostic.message and "物理" in latest.diagnostic.message
    # 新语义不可由查询配置推断成旧快照的证据。
    assert latest.latest["mem_util"].timestamp_semantics == "unknown"


def test_persistence_metadata_is_separate_from_original_labels_and_idempotent(ecs_world):
    row = ecs_world.get(Resource, 1)
    point = fixtures.point(0, dimensions={"job": "source", "mountpoint": "/", "timestamp_semantics": "original-label"})
    labels = deepcopy(point.dimensions)
    diagnostic = {"metrics": {"cpu_util": {"timestamp_semantics": "evaluation"}}}
    for _ in range(2):
        accepted, rejected = service.save_readings(ecs_world, [row], {row.resource_id: [point]}, NOW,
            diagnostic=diagnostic, signature="test-digest")
        ecs_world.flush()
        assert accepted[row.id] == [point] and rejected == 0
    saved = ecs_world.scalar(select(EcsMetricSample))
    assert saved.dimensions["__ecs_monitoring"] == {
        "version": 1, "timestamp_semantics": "evaluation", "config_signature": "test-digest"}
    assert saved.dimensions["mountpoint"] == "/"
    assert point.dimensions == labels
    assert len(list(ecs_world.scalars(select(EcsMetricSample)))) == 1


def test_history_separates_evaluation_source_and_unknown_without_dropping_values(ecs_world):
    row = ecs_world.get(Resource, 1)
    for i, semantics in enumerate(("evaluation", "source_sample", "unknown")):
        point = fixtures.point(i * 20, at=NOW-timedelta(seconds=100+i))
        service.save_readings(ecs_world, [row], {row.resource_id: [point]}, NOW,
            diagnostic={"metrics": {"cpu_util": {"timestamp_semantics": semantics}}}, signature="test-digest")
    ecs_world.commit()
    result = service.history(ecs_world, fixtures.reader(), 1,
        service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 300, ["cpu_util"]))
    assert {s.timestamp_semantics for s in result.series} == {"evaluation", "source_sample", "unknown"}
    assert sorted(p.average for s in result.series for p in s.points if p.sample_count) == [0, 20, 40]
    assert any("求值" in warning for warning in result.warnings)
    assert any("未知" in warning for warning in result.warnings)


def test_collector_passes_complete_mapping_and_keeps_provider_time_evidence(ecs_world, monkeypatch):
    from app.collectors import ecs_metrics
    policy = CollectionPolicy(prometheus=mapping(), metric_keys=["cpu_util"]).model_dump(mode="json")
    ecs_world.add(SysSetting(key="collection_policy:1:ecs_server", value=policy))
    ecs_world.commit()
    point = fixtures.point(0, dimensions={"job": "verified-exporter"})

    def collect(_ctx, _ids, _index, **kwargs):
        assert kwargs["config"]["prometheus"] == mapping()
        return EcsReadingsResult({"shared-cloud-id": [point]}, "PROMETHEUS",
            {"status": "ready", "metrics": {"cpu_util": {"timestamp_semantics": "source_sample"}}})

    monkeypatch.setattr(ecs_metrics, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=ecs_world.get(HcsAccount, 1), db=ecs_world)
    observations = ecs_metrics.EcsMetricsCollector().collect(ctx)
    item = observations[0].attrs["ecs_monitoring"]["latest"]["cpu_util"]
    assert item["timestamp_semantics"] == "source_sample" and item["unit"] == "%" and item["value"] == 0
    assert item["dimensions"] == {"job": "verified-exporter"}
    saved = ecs_world.scalar(select(EcsMetricSample))
    assert saved.dimensions["__ecs_monitoring"]["timestamp_semantics"] == "source_sample"


def test_historical_missing_units_is_readable_without_guessing_or_database_rewrite(ecs_world):
    from app.services.collection_policy import effective
    original = {"prometheus": {"queries": {"cpu_util": "custom_unknown_unit"}}}
    row = SysSetting(key="collection_policy:1:ecs_server", value=original)
    ecs_world.add(row)
    ecs_world.commit()
    data = effective(ecs_world, 1).model_dump()["prometheus"]
    assert data["units"] == {} and data["timestamp_queries"] == {}
    assert row.value == original
    with pytest.raises(ValidationError, match="单位"):
        CollectionPolicyUpdate(prometheus=data)


def test_legacy_task_structured_mapping_without_queries_keeps_time_and_units(ecs_world):
    from app.services.collection_policy import effective
    config = {"units": {"cpu_util": "%"}, "timestamp_queries": {"cpu_util": "source_seconds"},
              "label_filters": {"job": "verified-exporter"}}
    ecs_world.add(InspectionTask(code="ecs_metrics", name="测试任务", service="ecs", interval_sec=300,
        params={"prometheus": config}))
    ecs_world.commit()
    result = effective(ecs_world, 1).model_dump()["prometheus"]
    assert all(result[key] == value for key, value in config.items())


def test_policy_api_preserves_fields_and_rejects_unverified_unit_on_write(mem_db, policy_api):
    policy_fixtures.tenant(mem_db)
    api, _ = policy_api
    path = "/api/v1/system/collection-policies/11/ecs_server"
    result = api.put(path, json={"prometheus": mapping()})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["policy"]["prometheus"] == mapping()
    assert api.get(path).json()["data"]["policy"]["prometheus"] == mapping()
    invalid = mapping()
    invalid.pop("units")
    assert api.put(path, json={"prometheus": invalid}).status_code == 422
    assert api.get(path).json()["data"]["policy"]["prometheus"] == mapping()


def test_api_response_accepts_unknown_historical_units_without_rewriting(mem_db, policy_api):
    policy_fixtures.tenant(mem_db)
    original = {"prometheus": {"queries": {"cpu_util": "custom_unverified"}}}
    row = SysSetting(key="collection_policy:11:ecs_server", value=original)
    mem_db.add(row)
    mem_db.commit()
    api, _ = policy_api
    response = api.get("/api/v1/system/collection-policies/11/ecs_server")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["policy"]["prometheus"]["units"] == {}
    mem_db.refresh(row)
    assert row.value == original


@pytest.mark.parametrize("field,value", [
    ("units", {"cpu_util": "percent"}),
    ("timestamp_queries", {"cpu_util": "changed_source_seconds"}),
    ("label_filters", {"job": "changed-exporter"}),
])
def test_configuration_changed_during_query_does_not_persist_old_result(ecs_world, monkeypatch, field, value):
    from app.collectors import ecs_metrics
    policy = CollectionPolicy(prometheus=mapping(), metric_keys=["cpu_util"]).model_dump(mode="json")
    row = SysSetting(key="collection_policy:1:ecs_server", value=policy)
    ecs_world.add(row)
    ecs_world.commit()

    def collect(_ctx, _ids, _index, **_kwargs):
        changed = deepcopy(policy)
        changed["prometheus"][field] = value
        row.value = changed
        ecs_world.commit()
        return EcsReadingsResult({"shared-cloud-id": [fixtures.point(0)]}, "PROMETHEUS", {"status": "ready"})

    monkeypatch.setattr(ecs_metrics, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=ecs_world.get(HcsAccount, 1), db=ecs_world)
    assert ecs_metrics.EcsMetricsCollector().collect(ctx) == []
    assert ctx.stats["metrics_skipped"] == "configuration_changed"
    assert ecs_world.scalar(select(EcsMetricSample)) is None
    saved = service.policy_dict(ecs_world, 1)["prometheus"]
    assert saved[field] == value and saved["units"] and saved["timestamp_queries"]


def test_postgres_history_json_semantics_grouping_compiles_without_live_database(ecs_world, monkeypatch):
    resource = ecs_world.get(Resource, 1)
    current = service._outputs(ecs_world, [resource], NOW)
    statements = []
    fake_db = SimpleNamespace(scalar=lambda _: resource,
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()),
        execute=lambda query: statements.append(query) or [])
    monkeypatch.setattr(service, "_outputs", lambda *_: current)
    service.history(fake_db, fixtures.reader(), 1,
        service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 60, ["cpu_util"]))
    query = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "->>" in query and "GROUP BY" in query and "CASE WHEN" in query
