"""MRS native / DataArts 节点分离：本地合成响应，不访问局点。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.mrs import NODE_CACHE_VERSION, DataArtsMrsNodeCollector, MrsJobCollector
from app.hcs import HcsError
from app.models import AlarmEvent, CollectSwitch, InspectionTask, SysSetting
from app.rules.loader import sync_default_rules
from app.services import collect_switch, inspection_evidence, pipeline
from app.services.bootstrap import ensure_tasks
from app.services.collect_scope import Scopes

from .test_mrs_hcs851_contract import DataArtsClient, inst, job_ctx, node

NOW = 1_793_952_000_000


def job(jid="exec-1", name="same", **extra):
    return {"job_id": jid, "job_name": name, "job_type": "Flink", "job_state": "FINISHED",
            "job_result": "SUCCEEDED", "submitted_time": NOW - 200_000,
            "started_time": NOW - 120_000, "finished_time": NOW - 60_000,
            "elapsed_time": 999_999, **extra}


class NativeClient:
    def __init__(self, jobs=None, clusters=None, respond=None):
        self.jobs = jobs if jobs is not None else [job()]
        self.clusters = clusters if clusters is not None else [("c1", "cluster-one")]
        self.respond, self.calls = respond, []

    def get(self, service, path, **kwargs):
        self.calls.append((service, path, deepcopy(kwargs)))
        assert service == "mrs" and "headers" not in kwargs
        if path.endswith("cluster_infos"):
            return {"clusterTotal": str(len(self.clusters)), "clusters": [
                {"clusterId": cid, "clusterName": name} for cid, name in self.clusters]}
        if self.respond:
            return deepcopy(self.respond(path, kwargs["params"]))
        p = kwargs["params"]
        jobs = self.jobs
        if "job_state" in p:
            jobs = [j for j in jobs if j.get("job_state") == p["job_state"]]
        if "submitted_time_begin" in p:
            jobs = [j for j in jobs if p["submitted_time_begin"] <= j["submitted_time"] <= p["submitted_time_end"]]
        start = (p["offset"] - 1) * p["limit"]
        return {"job_list": deepcopy(jobs[start:start + p["limit"]]), "total_record": str(len(jobs))}


def context(client, **params):
    return RunContext(client=client, account="synthetic", project_id="project-one", tenant_id=11, project_ref=111,
                      workspaces=["never-use-this-workspace"], params=params)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.collectors.mrs_native.time.time", lambda: NOW / 1000)


def test_native_only_calls_mrs_and_preserves_same_name_concurrent_executions():
    client = NativeClient([job("a"), job("b", job_state="RUNNING", job_result="UNDEFINED", finished_time=None)])
    ctx = context(client, page_size=1)
    rows = MrsJobCollector().collect(ctx)
    assert {o.resource_id for o in rows} == {"c1/a", "c1/b"}
    assert {o.name for o in rows} == {"same"} and len(client.calls) == 8
    assert all(o.resource_type == "mrs_job" and o.attrs["source"] == "mrs_job_execution" for o in rows)
    rows.sort(key=lambda row: row.resource_id)
    assert rows[0].status_raw == "FINISHED" and rows[0].facts["job_result"] == "SUCCEEDED"
    assert rows[0].attrs["engine"] is None and "mode" not in rows[0].attrs
    assert rows[0].metrics["run_minutes"] == 1 and rows[1].metrics["run_minutes"] == 2
    assert rows[0].attrs["elapsed_time"] == 999_999 and rows[0].attrs["elapsed_time_unit"] is None
    assert not ctx.errors


@pytest.mark.parametrize("start,end", [(NOW - 60_000, NOW), (str(NOW - 60_000), str(NOW))])
def test_duration_uses_valid_millisecond_endpoints(start, end):
    row = MrsJobCollector().collect(context(NativeClient([job(started_time=start, finished_time=end)])))[0]
    assert row.metrics["run_minutes"] == 1 and row.attrs["elapsed_time_unit"] is None


@pytest.mark.parametrize("start,end", [(None, None), (0, NOW), (NOW, NOW - 1), (NOW // 1000, NOW // 1000 + 60),
                                        (True, NOW), ("NaN", NOW), (float("inf"), NOW)])
def test_invalid_duration_is_missing_even_in_mock(start, end):
    client = NativeClient([job(started_time=start, finished_time=end, elapsed_time=60_000)])
    client.mock_url = "http://mock"
    row = MrsJobCollector().collect(context(client))[0]
    assert row.metrics["run_minutes"] is None and row.attrs["elapsed_time"] == 60_000
    assert row.attrs["elapsed_time_unit"] is None


@pytest.mark.parametrize("unit,raw", [("seconds", 120), ("milliseconds", 120_000)])
def test_elapsed_conversion_requires_explicit_unit(unit, raw):
    row = MrsJobCollector().collect(context(NativeClient([job(started_time=None, finished_time=None, elapsed_time=raw)]),
                                           elapsed_time_unit=unit))[0]
    assert row.metrics["run_minutes"] == 2 and row.attrs["elapsed_time_unit"] == unit


@pytest.mark.parametrize("state,result", [("FINISHED", "FAILED"), ("FAILED", "FAILED"),
                                           ("FINISHED", "SUCCEEDED"), ("RUNNING", "UNDEFINED")])
def test_execution_state_and_result_remain_separate(state, result):
    row = MrsJobCollector().collect(context(NativeClient([job(job_state=state, job_result=result)])))[0]
    assert row.status_raw == state and row.facts["job_result"] == result
    assert inspection_evidence.state_known(row) and inspection_evidence.execution_id(row) == "exec-1"


@pytest.mark.parametrize("state,result", [("SUCCEEDED", "SUCCEEDED"), ("FINISHED", None), (None, "SUCCEEDED"),
                                           ("FUTURE", "SUCCEEDED"), ("FINISHED", "UNDEFINED")])
def test_unknown_or_incomplete_state_never_recovers(state, result):
    ctx = context(NativeClient([job(job_state=state, job_result=result)]))
    row = MrsJobCollector().collect(ctx)[0]
    assert ctx.errors and not inspection_evidence.state_known(row)


@pytest.mark.parametrize("bad", [True, {}, 3, []])
def test_non_string_state_does_not_reach_database(bad):
    ctx = context(NativeClient([job(job_state=bad)]))
    assert MrsJobCollector().collect(ctx) == [] and ctx.errors


@pytest.mark.parametrize("case", ["missing-total", "missing-list", "negative-total", "boolean-total", "early-empty",
                                    "repeated-page", "duplicate-id", "changing-total", "too-small-total"])
def test_incomplete_or_contradictory_pagination_never_yields_partial_snapshot(case):
    def respond(path, p):
        page = p["offset"]
        if case == "missing-total":
            return {"job_list": []}
        if case == "missing-list":
            return {"total_record": 0}
        if case == "negative-total":
            return {"total_record": -1, "job_list": []}
        if case == "boolean-total":
            return {"total_record": True, "job_list": []}
        if case == "early-empty":
            return {"total_record": 2, "job_list": [job()] if page == 1 else []}
        if case == "repeated-page":
            return {"total_record": 2, "job_list": [job()]}
        if case == "duplicate-id":
            return {"total_record": 2, "job_list": [job(name=f"name-{page}")]}
        if case == "changing-total":
            return {"total_record": 2 if page == 1 else 3, "job_list": [job(str(page))]}
        return {"total_record": 0, "job_list": [job()]}
    ctx = context(NativeClient(respond=respond), page_size=1, query_mode="all_history")
    assert MrsJobCollector().collect(ctx) == [] and ctx.errors


def test_short_pages_follow_total_with_one_based_record_offset():
    offsets = []

    def respond(path, p):
        offsets.append(p["offset"])
        return {"total_record": 3, "job_list": [job(str(p["offset"]))]}
    rows = MrsJobCollector().collect(context(NativeClient(respond=respond), job_offset_mode="offset", page_size=100,
                                           query_mode="all_history"))
    assert len(rows) == 3 and offsets == [1, 2, 3]


def test_one_cluster_failure_preserves_other_cluster_and_marks_partial():
    def respond(path, p):
        if "/c1/" in path:
            raise HcsError(503, "SYNTHETIC.FAILURE", "unavailable")
        return {"total_record": 1, "job_list": [job()]}
    ctx = context(NativeClient(clusters=[("c1", "one"), ("c2", "two")], respond=respond), query_mode="all_history")
    assert [row.resource_id for row in MrsJobCollector().collect(ctx)] == ["c2/exec-1"] and len(ctx.errors) == 1


@pytest.mark.parametrize("params", [{"submitted_window_hours": 0}, {"submitted_window_hours": True},
                                    {"submitted_window_hours": "NaN"},
                                    {"elapsed_time_unit": "ms"}, {"job_offset_mode": "guess"},
                                    {"max_items": 0}, {"max_pages": 201}, {"page_size": False}])
def test_invalid_config_fails_before_any_request(params):
    client = Mock()
    with pytest.raises(HcsError):
        MrsJobCollector().collect(context(client, **params))
    client.get.assert_not_called()


@pytest.mark.parametrize("params", [{"max_items": 1}, {"max_pages": 1, "page_size": 1}])
def test_budget_exhaustion_discards_cluster_snapshot(params):
    ctx = context(NativeClient([job("1"), job("2")]), **params)
    assert MrsJobCollector().collect(ctx) == [] and ctx.errors


def test_scope_cuts_clusters_before_job_calls_and_execution_ids_after_list():
    client = NativeClient([job("a"), job("b")], clusters=[("c1", "one"), ("c2", "two")])
    ctx = context(client)
    ctx.scopes = Scopes(raw={(11, "mrs_job"): {"rules": [{"effect": "include", "match": {
        "cluster": ["two"], "job": ["same"], "id": ["b"]}}]}})
    assert [row.resource_id for row in MrsJobCollector().collect(ctx)] == ["c2/b"]
    assert len(client.calls) == 7 and all("/c2/" in call[1] for call in client.calls[1:])


def test_legacy_workspace_scope_rejected_before_calls():
    client = Mock()
    ctx = context(client)
    ctx.scopes = Scopes(raw={(11, "mrs_job"): {"rules": [{"effect": "include", "match": {"workspace": ["ws"]}}]}})
    with pytest.raises(HcsError, match="旧 workspace/node"):
        MrsJobCollector().collect(ctx)
    client.get.assert_not_called()


def test_official_sql_types_included_without_fabricated_aliases(mem_db):
    nodes = [node("hive", "HiveSQL"), node("sql", "SparkSQL"), node("fake-hive", "MRSHive"),
             node("fake-sql", "MRSSparkSQL")]
    ctx = job_ctx(DataArtsClient([inst()], {("job-a", 1): nodes}))
    ctx.db = mem_db
    rows = DataArtsMrsNodeCollector().collect(ctx)
    assert {o.attrs["node_type"] for o in rows} == {"HiveSQL", "SparkSQL"}
    assert all(o.resource_type == "dgc_mrs_node" and o.attrs["source"] == "dataarts_instance_detail" for o in rows)
    assert all(o.attrs["mrs_node_manifest"]["version"] == NODE_CACHE_VERSION for o in rows)


def test_old_negative_cache_invalidated_for_same_instance(mem_db):
    mem_db.add(SysSetting(key="mrs_job_scan:11:111", value={"instances": {"ws-1/job-a": 1}}))
    mem_db.commit()
    client = DataArtsClient([inst()], {("job-a", 1): [node("hive", "HiveSQL")]})
    ctx = job_ctx(client)
    ctx.db = mem_db
    assert len(DataArtsMrsNodeCollector().collect(ctx)) == 1 and len(client.details()) == 1


def test_unknown_result_cannot_recover_native_alarm(mem_db):
    sync_default_rules(mem_db)
    ctx = context(NativeClient(), _task_code="mrs_job_status")
    first = MrsJobCollector().collect(context(NativeClient([job(job_result="FAILED")])))[0]
    now = datetime.fromtimestamp(NOW / 1000, UTC)
    pipeline.process_observations(mem_db, "mrs", [first], ctx, now)
    event = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-MRS-NATIVE-001"))
    assert event.status == "FIRING"
    first.facts["job_result"] = "FUTURE"
    pipeline.process_observations(mem_db, "mrs", [first], ctx, now + timedelta(minutes=1))
    assert mem_db.get(AlarmEvent, event.id).status == "FIRING"
    first.facts["job_result"] = "SUCCEEDED"
    pipeline.process_observations(mem_db, "mrs", [first], ctx, now + timedelta(minutes=2))
    assert mem_db.get(AlarmEvent, event.id).status == "RESOLVED"


def test_new_environment_mrs_tasks_and_switches_disabled(mem_db):
    ensure_tasks(mem_db)
    for code in ("mrs_cluster_status", "mrs_job_status", "dgc_mrs_node_status"):
        assert mem_db.get(InspectionTask, code).enabled is False
    switches = collect_switch.load(mem_db)
    assert all(not switches.enabled(None, rt) for rt in ("mrs_cluster", "mrs_job", "dgc_mrs_node"))


def test_bootstrap_split_inherits_schedule_params_and_original_authorization(mem_db):
    original = InspectionTask(code="mrs_job_status", name="MRS", service="mrs", interval_sec=777,
                              cron="*/7 * * * *", enabled=True, params={"window_hours": 24, "max_details": 23})
    mem_db.add_all([original, CollectSwitch(tenant_id=11, resource_type="mrs_job", enabled=True),
                    SysSetting(key="collect_scope:11:mrs_job", value={"rules": [
                        {"effect": "include", "match": {"workspace": ["ws-1"]}}]}, updated_by="admin")])
    mem_db.commit()
    ensure_tasks(mem_db)
    new = mem_db.get(InspectionTask, "dgc_mrs_node_status")
    assert (new.interval_sec, new.cron, new.enabled, new.params) == (777, original.cron, True, original.params)
    assert original.enabled and original.interval_sec == 777
    assert collect_switch.load(mem_db).enabled(11, "dgc_mrs_node") is True
    assert collect_switch.load(mem_db).enabled(11, "mrs_job") is False
    assert mem_db.get(SysSetting, "collect_scope:11:dgc_mrs_node").value == mem_db.get(
        SysSetting, "collect_scope:11:mrs_job").value
    new.enabled = False
    mem_db.flush()
    assert ensure_tasks(mem_db) == 0 and new.enabled is False


def test_default_and_legacy_node_window_never_filter_long_running_native_jobs():
    for params in ({}, {"window_hours": 24}):
        client = NativeClient([job(job_state="RUNNING", job_result="UNDEFINED", submitted_time=NOW - 365 * 86_400_000,
                                   finished_time=None)])
        ctx = context(client, **params)
        rows = MrsJobCollector().collect(ctx)
        assert len(rows) == 1 and not ctx.errors
        assert "submitted_time_begin" not in client.calls[1][2]["params"]
        assert rows[0].metrics["run_minutes"] == 2
        assert rows[0].attrs["duration_basis"] == "start_now_milliseconds"


def test_explicit_native_submitted_window_only_changes_recent_partition():
    client = NativeClient()
    MrsJobCollector().collect(context(client, submitted_window_hours=12))
    assert "submitted_time_begin" not in client.calls[1][2]["params"]
    assert client.calls[-1][2]["params"]["submitted_time_begin"] == NOW - 12 * 3_600_000


@pytest.mark.parametrize("state", ["NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING"])
def test_native_yarn_lifecycle_states_from_official_examples_are_known(state):
    ctx = context(NativeClient([job(job_state=state, job_result="UNDEFINED")]))
    assert inspection_evidence.state_known(MrsJobCollector().collect(ctx)[0]) and not ctx.errors


@pytest.mark.parametrize("state", ["PENDING", "STARTING"])
def test_native_does_not_promote_unconfirmed_lifecycle_aliases(state):
    ctx = context(NativeClient([job(job_state=state, job_result="UNDEFINED")]))
    assert not inspection_evidence.state_known(MrsJobCollector().collect(ctx)[0]) and ctx.errors


def test_running_duration_saves_time_basis_and_observation_milliseconds():
    ctx = context(NativeClient([job(job_state="RUNNING", job_result="UNDEFINED", finished_time=None)]))
    row = MrsJobCollector().collect(ctx)[0]
    assert row.metrics["run_minutes"] == 2 and row.attrs["duration_basis"] == "start_now_milliseconds"
    assert row.attrs["duration_observed_at"] == NOW and row.attrs["elapsed_time_unit"] is None
    assert not ctx.errors


def test_optional_unconfirmed_elapsed_time_is_diagnostic_not_partial():
    ctx = context(NativeClient([job(job_state="RUNNING", job_result="UNDEFINED", started_time=None,
                                  finished_time=None, elapsed_time=1234)]))
    row = MrsJobCollector().collect(ctx)[0]
    assert row.metrics["run_minutes"] is None and row.attrs["duration_basis"] is None
    assert ctx.stats["mrs_jobs"]["duration_unknown"] == 1 and not ctx.errors
    assert ctx.stats["jobs_duration_unit_unverified"] == 1



def test_native_guide_covers_all_runtime_fields_without_dataarts_dependency():
    from app.services.resource_guides import get_resource_guide, rule_fact_fields

    row = MrsJobCollector().collect(context(NativeClient()))[0]
    guide = get_resource_guide("mrs_job")
    assert set(row.attrs) <= {f.key for f in guide.attributes}
    assert set(row.metrics) <= {f.key for f in guide.metrics}
    assert set(row.facts) <= {f.key for f in rule_fact_fields("mrs_job", "job_status")}
    assert guide.collection_tasks == ["mrs_job_status"]
    assert get_resource_guide("dgc_mrs_node").collection_tasks == ["dgc_mrs_node_status"]


def test_bootstrap_preserves_preconfigured_node_authorization_when_native_scope_is_incompatible(mem_db):
    mem_db.add_all([
        InspectionTask(code="mrs_job_status", name="Native", service="mrs", interval_sec=300, enabled=True),
        SysSetting(key="collect_scope:11:mrs_job", value={"rules": [
            {"effect": "include", "match": {"cluster": ["c1"]}}]}),
        SysSetting(key="collect_scope:11:dgc_mrs_node", value={"rules": [
            {"effect": "include", "match": {"workspace": ["approved"]}}]}),
        CollectSwitch(tenant_id=11, resource_type="dgc_mrs_node", enabled=True, updated_by="operator"),
    ])
    mem_db.commit()
    ensure_tasks(mem_db)
    assert collect_switch.load(mem_db).enabled(11, "dgc_mrs_node")
    assert mem_db.get(SysSetting, "collect_scope:11:dgc_mrs_node").value["rules"][0]["match"] == {
        "workspace": ["approved"]}


def test_demo_mrs_opt_in_is_tenant_local_and_preserves_existing_choices(world, mem_db):
    from app.demo.seed import _tenants

    before = collect_switch.load(mem_db)
    assert all(not before.enabled(None, kind) for kind in ("mrs_cluster", "mrs_job", "dgc_mrs_node"))
    assert all(before.enabled(world["tenant-a"].id, kind) for kind in ("mrs_cluster", "mrs_job", "dgc_mrs_node"))
    assert all(not before.enabled(world["tenant-b"].id, kind) for kind in ("mrs_cluster", "mrs_job", "dgc_mrs_node"))
    row = mem_db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == world["tenant-a"].id,
                                                   CollectSwitch.resource_type == "mrs_job"))
    row.enabled, row.updated_by = False, "operator"
    mem_db.commit()
    _tenants(mem_db)
    mem_db.flush()
    assert not collect_switch.load(mem_db).enabled(world["tenant-a"].id, "mrs_job")
    assert row.updated_by == "operator"


@pytest.mark.parametrize("invalid_end", [False, True, [], {}, "invalid"])
def test_running_duration_does_not_treat_malformed_end_as_missing(invalid_end):
    ctx = context(NativeClient([job(job_state="RUNNING", job_result="UNDEFINED", finished_time=invalid_end)]))
    row = MrsJobCollector().collect(ctx)[0]
    assert row.metrics["run_minutes"] is None and row.attrs["duration_basis"] is None
    assert not ctx.errors


def test_native_queue_and_safe_page_diagnostics(caplog):
    client = NativeClient([job(queue="analytics", arguments="do-not-log-payload")])
    with caplog.at_level("INFO"):
        rows = MrsJobCollector().collect(context(client))
    assert rows[0].attrs["queue"] == "analytics"
    pages = [r for r in caplog.records if r.msg == "job_collection_page"]
    assert len(pages) == 6 and all(p.cloud_service == "mrs" for p in pages)
    assert pages[-1].query == "recent" and pages[-1].response_total == pages[-1].returned_count == 1
    assert any(r.msg == "job_collection_summary" and r.executions == 1 for r in caplog.records)
    assert "do-not-log-payload" not in caplog.text
