"""DataArts definitions, execution instances and engine evidence are distinct read-only objects."""
from copy import deepcopy
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.hcs import HcsError
from app.models import Resource
from app.models.base import utcnow
from app.services.pipeline import process_observations

NOW = 1_800_000_000_000


class JobApi:
    def __init__(self, jobs=None, definitions=None, instances=None, statuses=None):
        self.jobs, self.definitions = jobs or [], definitions or {}
        self.instances, self.statuses = instances or [], statuses or {}
        self.calls = []

    def get(self, service, path, **kwargs):
        assert service == "dataarts"
        self.calls.append((path, deepcopy(kwargs)))
        if path == "/v1/{project_id}/jobs":
            q = kwargs["params"]
            rows = [job for job in self.jobs if job["jobType"] == q["jobType"]]
            start = q["offset"] * q["limit"]
            return {"total": len(rows), "jobs": deepcopy(rows[start:start + q["limit"]])}
        if path == "/v1/{project_id}/jobs/instances/detail":
            if isinstance(self.instances, Exception):
                raise self.instances
            q = kwargs["params"]
            assert q["minPlanTime"] == NOW - 86400000 and q["maxPlanTime"] == NOW
            start = q["offset"] * q["limit"]
            return {"total": len(self.instances), "instances": deepcopy(self.instances[start:start + q["limit"]])}
        name = unquote(path.split("/jobs/", 1)[1].removesuffix("/status"))
        if path.endswith("/status"):
            result = self.statuses.get(name, {"name": name, "status": "NORMAL", "nodes": []})
        else:
            job = next(job for job in self.jobs if job["name"] == name)
            result = self.definitions.get(name, {"name": name, "processType": job["jobType"], "nodes": []})
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def job(name="weekly", kind="BATCH", **extra):
    return {"name": name, "jobType": kind, "status": "SCHEDULING", **extra}


def instance(name="weekly", identity=1, **extra):
    return {"jobName": name, "instanceId": identity, "status": "success", "planTime": NOW - 60000,
            "startTime": NOW - 60000, "endTime": NOW - 30000, "instanceType": 0, **extra}


def context(client, **kwargs):
    return RunContext(client=client, account="synthetic", project_id="synthetic", tenant_id=1,
                      project_ref=1, workspaces=["ws-one"], **kwargs)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.collectors.dataarts.time.time", lambda: NOW / 1000)


def test_defined_weekly_jobs_remain_visible_without_recent_instances():
    api = JobApi(jobs=[job("weekly"), job("disabled", status="STOPPED")])
    ctx = context(api)
    rows = DataArtsBatchCollector().collect(ctx)
    assert len(rows) == 2 and not ctx.errors
    assert {r.name for r in rows} == {"weekly", "disabled"}
    assert all(r.attrs["definition_present"] and r.attrs["data_object"] == "DEFINITION" for r in rows)
    assert all(r.attrs["instance_status"] is None and r.facts == {} for r in rows)
    assert all(r.metrics["run_minutes"] is None and r.attrs["instance_query_status"] == "EMPTY" for r in rows)
    assert rows[0].attrs["schedule_status"] == "SCHEDULING"
    assert rows[1].attrs["schedule_status"] == "STOPPED"
    assert ctx.stats["dataarts"]["workspaces"][0]["reason"] == "NO_RECENT_INSTANCES"
    assert api.calls[0][1]["params"]["jobType"] == "BATCH"


@pytest.mark.parametrize("single", ["FlinkSQL", "FlinkJar"])
def test_flink_classification_uses_documented_definition_not_mode_or_job_name(single):
    api = JobApi(jobs=[job("stream", "REAL_TIME"), job("flink-in-name", "REAL_TIME")], definitions={
        "stream": {"name": "stream", "processType": "REAL_TIME", "singleNodeJobType": single, "nodes": []},
        "flink-in-name": {"name": "flink-in-name", "processType": "REAL_TIME", "nodes": [{"type": "MRSSpark"}]},
    })
    rows = DataArtsRealtimeCollector().collect(context(api))
    parents = {r.name: r for r in rows if r.check_item == "job_status"}
    assert parents["stream"].attrs["engines"] == ["Flink"]
    assert parents["stream"].attrs["single_node_type"] == single
    assert parents["flink-in-name"].attrs["engines"] == ["Spark"]
    assert parents["flink-in-name"].attrs["job_type"] == "REAL_TIME"
    assert all(kwargs.get("headers") == {"workspace": "ws-one"} for _, kwargs in api.calls)


def test_mixed_workflow_keeps_multiple_engines_and_one_parent_definition():
    api = JobApi(jobs=[job()], definitions={"weekly": {"name": "weekly", "processType": "BATCH", "nodes": [
        {"type": "HiveSQL"}, {"type": "SparkSQL"}, {"type": "MRSFlinkJob"}]}})
    row = DataArtsBatchCollector().collect(context(api))[0]
    assert row.attrs["engines"] == ["Flink", "Hive", "Spark"]
    assert row.attrs["engine"] is None
    assert row.attrs["job_type"] == "BATCH"


def test_instance_failure_keeps_definition_without_stale_execution_success(mem_db):
    api = JobApi(jobs=[job()], instances=HcsError(503, "TEST.UPSTREAM", "synthetic"))
    ctx = context(api)
    rows = DataArtsBatchCollector().collect(ctx)
    assert len(rows) == 1 and ctx.errors
    row = rows[0]
    assert row.attrs["instance_query_status"] == "FAILED"
    assert row.attrs["instance_status"] is None and row.facts == {}
    assert row.metrics["run_minutes"] is None


def test_realtime_status_failure_keeps_definition_and_engine_without_fake_running():
    api = JobApi(jobs=[job("live", "REAL_TIME")], statuses={"live": HcsError(403, "DENIED", "synthetic")})
    ctx = context(api)
    row = DataArtsRealtimeCollector().collect(ctx)[0]
    assert ctx.errors and row.attrs["definition_present"]
    assert row.attrs["job_status"] is None and row.attrs["status_query_status"] == "FAILED"
    assert row.facts == {} and row.metrics == {"nodes": None, "bad_nodes": None}


def test_subjob_instance_is_not_mislabelled_as_a_batch_definition():
    api = JobApi(jobs=[job()], instances=[instance(), instance("real_time_node", instanceType=6)])
    rows = {r.name: r for r in DataArtsBatchCollector().collect(context(api))}
    assert rows["weekly"].attrs["definition_present"] is True
    orphan = rows["real_time_node"]
    assert orphan.attrs["definition_present"] is False and orphan.attrs["job_type"] is None
    assert orphan.attrs["data_object"] == "INSTANCE" and orphan.attrs["instance_type"] == 6
    assert orphan.facts["last_status"] == "success"


def test_empty_default_workspace_records_distinct_diagnostic_and_safe_counts(caplog):
    api = JobApi()
    ctx = context(api)
    ctx.workspaces = []
    with caplog.at_level("INFO"):
        assert DataArtsBatchCollector().collect(ctx) == []
    summary = ctx.stats["dataarts"]["workspaces"][0]
    assert summary["reason"] == "DEFAULT_WORKSPACE_EMPTY"
    assert summary["definitions"] == summary["instances"] == 0
    pages = [r for r in caplog.records if r.msg == "job_collection_page"]
    assert len(pages) == 2 and all(r.returned_count == r.response_total == 0 for r in pages)
    assert all(r.workspace_mode == "default" for r in pages)


def test_missing_metadata_does_not_reuse_old_engine_or_mark_definition_success():
    api = JobApi(jobs=[job()], definitions={"weekly": {}})
    ctx = context(api)
    row = DataArtsBatchCollector().collect(ctx)[0]
    assert ctx.errors and row.attrs["engines"] == []
    assert row.attrs["definition_metadata"]["status"] == "FAILED"


def test_definition_cache_requires_same_tenant_project_and_source_revision(mem_db):
    api = JobApi(jobs=[job(lastUpdateTime=1234567890000)])
    ctx = context(api, db=mem_db)
    meta = {"status": "AVAILABLE", "revision": [None, "BATCH", 1234567890000, None],
            "engines": ["Flink"], "single_node_type": "FlinkSQL", "node_types": []}
    mem_db.add(Resource(tenant_id=2, project_ref=2, service="dataarts", resource_type="dgc_batch_job",
                        resource_id="ws-one/weekly", name="weekly", attrs={"definition_metadata": meta}))
    mem_db.commit()
    row = DataArtsBatchCollector().collect(ctx)[0]
    assert row.attrs["engine"] is None
    assert any(p == "/v1/{project_id}/jobs/weekly" for p, _ in api.calls)
    api.calls.clear()
    process_observations(mem_db, "dataarts", [row], ctx, now=utcnow())
    mem_db.commit()
    DataArtsBatchCollector().collect(ctx)
    assert not any(p == "/v1/{project_id}/jobs/weekly" for p, _ in api.calls)
    api.jobs[0]["lastUpdateTime"] += 1
    api.calls.clear()
    DataArtsBatchCollector().collect(ctx)
    assert any(p == "/v1/{project_id}/jobs/weekly" for p, _ in api.calls)
    assert len(list(mem_db.scalars(select(Resource)))) == 2
