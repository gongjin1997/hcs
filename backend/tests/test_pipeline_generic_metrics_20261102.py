"""通用运行指标完整快照：不让过滤掉的 None 复用旧值，不触碰专用指标阶段。"""
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from app.collectors.base import Observation, RunContext
from app.models import Resource
from app.services.pipeline import upsert_resource
from app.services.resource_guides import get_resource_guide

NOW = datetime(2026, 11, 2, 3, tzinfo=UTC)


def context():
    return RunContext(client=Mock(), account="synthetic", project_id="project-11", tenant_id=1, project_ref=11,
                      params={"_inspection_stale_after_sec": 3600})


@pytest.mark.parametrize("kind", ["dgc_realtime_job", "dgc_batch_job", "mrs_job", "cce_workload"])
def test_missing_current_metrics_clear_old_guide_managed_values(mem_db, kind):
    guide = get_resource_guide(kind)
    keys = {field.key for field in guide.metrics if field.availability != "not_collected"}
    assert keys
    row = Resource(service="synthetic", resource_type=kind, resource_id="one", name="one", tenant_id=1,
                   project_ref=11, metrics=dict.fromkeys(keys, 17))
    mem_db.add(row)
    mem_db.commit()
    current = upsert_resource(mem_db, "synthetic", [Observation(kind, "one", "one", "status", {})], NOW, context())
    assert all(current.metrics[key] is None for key in keys)
    assert current.attrs["metrics_monitoring"] == {
        "observed_at": NOW.isoformat(), "time_basis": "LOCAL_COLLECTION", "stale_after_sec": 3600}


def test_realtime_parent_metrics_are_not_erased_by_child_observations(mem_db):
    group = [Observation("dgc_realtime_job", "one", "one", "job_status", {}, metrics={"nodes": 3, "bad_nodes": 0}),
             Observation("dgc_realtime_job", "one", "one", "node_status", {}, sub_object="node-a")]
    row = upsert_resource(mem_db, "dataarts", group, NOW, context())
    assert row.metrics["nodes"] == 3 and row.metrics["bad_nodes"] == 0


@pytest.mark.parametrize("kind,key", [("ecs_server", "cpu_util"), ("dcs_instance", "memory_usage"),
                                      ("gaussdb_instance", "disk_usage")])
def test_generic_snapshot_never_clears_independently_sampled_metrics(mem_db, kind, key):
    mem_db.add(Resource(service="synthetic", resource_type=kind, resource_id="one", name="one", tenant_id=1,
                        project_ref=11, metrics={key: 17}))
    mem_db.commit()
    ctx = context()
    ctx.params["_collection_phase"] = "metrics"
    row = upsert_resource(mem_db, "synthetic", [Observation(kind, "one", "one", "status", {})], NOW, ctx)
    assert row.metrics[key] == 17 and "metrics_monitoring" not in row.attrs


def test_uncollected_metric_is_not_created_as_a_zero_or_baseline(mem_db):
    row = upsert_resource(mem_db, "dataarts", [Observation("dgc_batch_job", "one", "one", "instance_status", {})],
                          NOW, context())
    guide = get_resource_guide("dgc_batch_job")
    for field in guide.metrics:
        if field.availability == "not_collected":
            assert field.key not in row.metrics
    assert row.metrics["run_minutes"] is None
