"""独立官方形状 fixture：EDOC1100450764，HCS 8.5.1，§0086/0093/0094。

2026-10-27 公网页实际修订为 DataArts 2.11.1.4/05；不把它冒充搜索缓存里的 2.11.1.3/04。
URI、参数、可选节点状态及 executeTime 单位冲突来自正文；名称/范围及批量数据为合成测试值。
"""
from copy import deepcopy
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select

from app.collectors import dataarts
from app.collectors.base import RunContext
from app.core.config import get_settings
from app.hcs import ratelimit
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.models import Resource
from app.rules.loader import sync_default_rules
from app.services.pipeline import run_task
from app.services.tenancy import Target

NOW = 1551671600000


def job(name="job_sms", **overrides):
    return {"id": "synthetic-id-" + name, "name": name, "jobType": "REAL_TIME", "owner": "test_user",
            "priority": 0, "path": "/", "createUser": "test_user", "createTime": NOW - 3600000, **overrides}


def status(name="job_sms", **overrides):
    # 官方 §0093 成功样例的节点没有 status/type，不能擅自推定节点 NORMAL。
    return {"name": name, "status": "NORMAL", "nodes": [{"name": "MRS_Flink_Job_8635", "bufferRecords": 0,
            "jobInstanceId": 0, "LastInstanceStatus": "waiting", "speed": 0,
            "totalGetBytes": 0, "totalGetRecords": 0, "totalPutBytes": 0, "totalPutRecords": 0}], **overrides}


def instance(name="job_batch", **overrides):
    # §0094 示例的 18 秒起止差，与 executeTime=0.3 / 字段表“毫秒”存在冲突。
    return {"jobName": name, "jobId": 608627, "jobInstanceName": name, "instanceId": 34765, "instanceType": 2,
            "planTime": 1551671580000, "startTime": 1551671580000, "endTime": 1551671598000,
            "executeTime": 0.3, "submitTime": 1550910278706, "status": "success", "version": 1, **overrides}


class ContractClient:
    """仅接受三条 v1 查询，offset 必须是从零开始的页号；不调用现有 Mock。"""
    def __init__(self, *, jobs=None, instances=None, statuses=None, page_size=2, list_body=None):
        self.jobs = [job()] if jobs is None else jobs
        self.instances = [instance()] if instances is None else instances
        self.statuses, self.page_size, self.list_body = statuses or {}, page_size, list_body
        self.calls = []
        self.definition_types = {}

    def get(self, service, path, **kwargs):
        assert service == "dataarts"
        self.calls.append((path, deepcopy(kwargs)))
        headers = kwargs.get("headers")
        assert headers is None or set(headers) == {"workspace"}
        if path in ("/v1/{project_id}/jobs", "/v1/{project_id}/jobs/instances/detail"):
            params = kwargs["params"]
            assert params["limit"] == 100 and isinstance(params["offset"], int) and params["offset"] >= 0
            if path.endswith("/jobs"):
                assert set(params) == {"jobType", "limit", "offset"} and params["jobType"] in ("REAL_TIME", "BATCH")
                rows = self.jobs if params["jobType"] == "REAL_TIME" else [
                    {"name": name, "jobType": "BATCH"} for name in dict.fromkeys(
                        i["jobName"] for i in self.instances if isinstance(i.get("jobName"), str) and i["jobName"])]
                key = "jobs"
                self.definition_types.update({row["name"]: params["jobType"] for row in rows})
            else:
                assert set(params) == {"minPlanTime", "maxPlanTime", "limit", "offset"}
                assert params["maxPlanTime"] == NOW and params["minPlanTime"] == NOW - 24 * 3600 * 1000
                assert params["maxPlanTime"] - params["minPlanTime"] <= 3 * 24 * 3600 * 1000
                key, rows = "instances", self.instances
            if self.list_body is not None:
                return deepcopy(self.list_body)
            start = params["offset"] * self.page_size
            return {"total": len(rows), key: deepcopy(rows[start:start + self.page_size])}
        for row in [*self.jobs, *[{"name": i["jobName"], "jobType": "BATCH"} for i in self.instances
                                  if isinstance(i.get("jobName"), str) and i["jobName"]]]:
            if path == f"/v1/{{project_id}}/jobs/{quote(row['name'], safe='')}":
                return {"name": row["name"], "processType": self.definition_types.get(row["name"], row["jobType"]), "nodes": []}
        for row in self.jobs:
            if path == f"/v1/{{project_id}}/jobs/{quote(row['name'], safe='')}/status":
                assert set(kwargs) == {"headers"}
                value = self.statuses.get(row["name"], status(row["name"]))
                if isinstance(value, Exception):
                    raise value
                return deepcopy(value)
        pytest.fail("undocumented or incorrectly encoded DataArts request")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(dataarts.time, "time", lambda: NOW / 1000)


def context(client, workspaces=None):
    return RunContext(client=client, account="synthetic", project_id="synthetic-project", tenant_id=101, project_ref=1001,
                      workspaces=["workspace-one"] if workspaces is None else workspaces)


def test_official_realtime_example_keeps_missing_node_status_unknown_and_priority_zero():
    ctx = context(ContractClient())
    rows = dataarts.DataArtsRealtimeCollector().collect(ctx)
    assert len(rows) == 1 and not ctx.errors
    row = rows[0]
    assert row.resource_id == "workspace-one/job_sms" and row.status_raw == "NORMAL"
    assert row.facts == {"status": "NORMAL"} and row.metrics == {"nodes": 1, "bad_nodes": None}
    assert row.attrs["priority"] == 0 and row.attrs["engine"] is None
    assert row.attrs["nodes"][0]["status"] is None


@pytest.mark.parametrize("kind", ["realtime", "batch"])
def test_both_lists_use_page_numbers_not_record_offsets_and_keep_one_time_window(kind):
    client = ContractClient(jobs=[job(str(i)) for i in range(5)],
                            instances=[instance(str(i), instanceId=i) for i in range(5)])
    ctx = context(client)
    rows = (dataarts.DataArtsRealtimeCollector() if kind == "realtime" else dataarts.DataArtsBatchCollector()).collect(ctx)
    path = "/v1/{project_id}/jobs" if kind == "realtime" else "/v1/{project_id}/jobs/instances/detail"
    pages = [kw["params"]["offset"] for p, kw in client.calls if p == path]
    assert pages == [0, 1, 2] and len(rows) == 5 and not ctx.errors


def test_workspace_is_on_every_request_and_duplicate_config_does_not_repeat_collection():
    client = ContractClient(jobs=[job("folder/中文 ?x=1")])
    ctx = context(client, ["ws-a", "ws-a", "ws-b"])
    rows = dataarts.DataArtsRealtimeCollector().collect(ctx)
    assert {r.resource_id for r in rows} == {"ws-a/folder/中文 ?x=1", "ws-b/folder/中文 ?x=1"}
    assert len(client.calls) == 6
    assert [kw["headers"]["workspace"] for _p, kw in client.calls] == ["ws-a", "ws-a", "ws-a", "ws-b", "ws-b", "ws-b"]


def test_default_workspace_header_is_omitted_as_documented():
    client = ContractClient()
    ctx = context(client, [])
    assert dataarts.DataArtsRealtimeCollector().collect(ctx)[0].resource_id == "job_sms"
    assert all(kw["headers"] is None for _p, kw in client.calls)


@pytest.mark.parametrize("body", [{}, {"total": None, "jobs": []}, {"total": 1}, {"total": 1, "jobs": [None]},
                                 {"total": True, "jobs": []}, {"total": 1, "jobs": []},
                                 {"total": 0, "jobs": [], "error_code": "DLF.3051"}])
def test_bad_or_incomplete_job_pages_cannot_become_successful_empty_snapshot(body):
    ctx = context(ContractClient(list_body=body))
    assert dataarts.DataArtsRealtimeCollector().collect(ctx) == [] and ctx.errors


@pytest.mark.parametrize("body", [{"name": "other", "status": "NORMAL"}, {"nodes": "invalid"},
                                 {"nodes": [None]}, {"nodes": [{"name": "one", "status": {}}]},
                                 {"nodes": [{"name": "same"}, {"name": "same"}]}, {"error_code": "DLF.0100"}])
def test_invalid_job_status_is_partial_without_fabricated_health(body):
    ctx = context(ContractClient(statuses={"job_sms": body}))
    rows = dataarts.DataArtsRealtimeCollector().collect(ctx)
    assert len(rows) == 1 and rows[0].attrs["job_status"] is None and len(ctx.errors) == 1


def test_status_failure_keeps_other_jobs_and_does_not_publish_failure_as_stopped():
    client = ContractClient(jobs=[job("healthy"), job("unreadable")], statuses={
        "healthy": status("healthy", nodes=[]), "unreadable": HcsError(403, "DLF.DENIED", "synthetic denied")})
    ctx = context(client)
    rows = dataarts.DataArtsRealtimeCollector().collect(ctx)
    assert [r.name for r in rows] == ["healthy", "unreadable"] and rows[0].metrics["bad_nodes"] == 0
    assert rows[1].attrs["job_status"] is None and rows[1].facts == {}
    assert len(ctx.errors) == 1


@pytest.mark.parametrize("nodes,count,bad", [(None, None, None), ([], 0, 0),
    ([{"name": "one", "status": "EXCEPTION"}, {"name": "two", "status": "STOPPED"}], 2, 2),
    ([{"name": "one", "status": "EXCEPTION"}, {"name": "two"}], 2, None)])
def test_optional_node_states_are_not_substituted_with_zero(nodes, count, bad):
    ctx = context(ContractClient(statuses={"job_sms": status(nodes=nodes)}))
    row = dataarts.DataArtsRealtimeCollector().collect(ctx)[0]
    assert row.metrics == {"nodes": count, "bad_nodes": bad}
    assert ("bad_nodes" in row.facts) is (bad is not None)


def test_node_types_are_separate_from_unverified_job_engine_extension():
    client = ContractClient(statuses={"job_sms": status(nodes=[{"name": "one", "status": "NORMAL", "type": "MRS Spark"}])})
    rows = dataarts.DataArtsRealtimeCollector().collect(context(client))
    assert rows[0].attrs["engine"] is None and rows[0].attrs["nodes"][0]["type"] == "MRS Spark"
    assert rows[1].facts == {"job_status": "NORMAL", "node_status": "NORMAL"}


def test_optional_parent_status_missing_is_unknown_and_not_a_normal_status():
    current = status(nodes=[])
    current.pop("status")
    rows = dataarts.DataArtsRealtimeCollector().collect(context(ContractClient(statuses={"job_sms": current})))
    assert rows[0].status_raw == "UNKNOWN" and "status" not in rows[0].facts


def test_documented_batch_example_uses_timestamps_not_ambiguous_execute_time_or_invented_p90():
    ctx = context(ContractClient())
    rows = dataarts.DataArtsBatchCollector().collect(ctx)
    assert not ctx.errors and len(rows) == 1
    assert rows[0].facts == {"last_status": "success", "run_minutes": 0.3}
    assert rows[0].attrs["instance_type"] == 2 and rows[0].attrs["instance_id"] == 34765
    assert rows[0].attrs["job_type"] == "BATCH" and rows[0].attrs["engine"] is None


@pytest.mark.parametrize("state,start,end,expected", [
    ("running", NOW - 600000, None, 10), ("running-exception", NOW - 600000, 0, 10),
    ("success", NOW - 600000, None, None), ("waiting", 0, None, None),
    ("pause", NOW - 600000, None, None), ("manual-stop", NOW - 600000, None, None),
    ("fail", "bad", NOW, None), ("running", NOW - 600000, "bad", None),
    ("running", NOW + 60000, None, None), ("success", NOW, NOW - 60000, None),
    ("success", float("nan"), NOW, None), ("success", float("inf"), NOW, None),
    ("success", True, NOW, None), ("success", 10**1000, NOW, None)])
def test_duration_requires_real_start_and_end_or_a_current_running_state(state, start, end, expected):
    ctx = context(ContractClient(instances=[instance(status=state, startTime=start, endTime=end, executeTime=999999)]))
    row = dataarts.DataArtsBatchCollector().collect(ctx)[0]
    assert row.metrics["run_minutes"] == expected
    assert row.facts.get("run_minutes") == expected and ("run_minutes" in row.facts) is (expected is not None)
    assert row.facts["last_status"] == state


def test_latest_planned_instance_tie_uses_actual_submission_and_keeps_workspace_identity():
    rows = [instance(instanceId=1, submitTime=NOW - 1000, status="success"),
            instance(instanceId=2, submitTime=NOW, status="fail"),
            instance(instanceId=3, planTime=NOW - 3600000, submitTime=NOW + 1, status="running")]
    ctx = context(ContractClient(instances=rows))
    row = dataarts.DataArtsBatchCollector().collect(ctx)[0]
    assert row.facts["last_status"] == "fail" and row.attrs["instance_id"] == 2
    assert row.resource_id == "workspace-one/job_batch"


@pytest.mark.parametrize("field,value", [("jobName", None), ("planTime", None), ("planTime", True),
                                       ("instanceId", None), ("instanceId", ""), ("instanceId", -1),
                                       ("instanceId", True), ("status", {})])
def test_incomplete_batch_identity_cannot_choose_an_old_healthy_instance(field, value):
    ctx = context(ContractClient(instances=[instance(), instance(**{"instanceId": 2, field: value})]))
    rows = dataarts.DataArtsBatchCollector().collect(ctx)
    assert rows and all(r.attrs["instance_status"] is None for r in rows) and ctx.errors


def test_duplicate_job_or_instance_pages_are_rejected_not_double_counted():
    rt = context(ContractClient(jobs=[job(), job()]))
    batch = context(ContractClient(instances=[instance(), instance()]))
    assert dataarts.DataArtsRealtimeCollector().collect(rt) == [] and rt.errors
    rows = dataarts.DataArtsBatchCollector().collect(batch)
    assert rows and all(r.attrs["instance_status"] is None for r in rows) and batch.errors


@pytest.mark.parametrize("priority,expected", [(None, None), (0, 0), (1, 1), (2, 2),
                                             (False, None), ("0", None), (3, None), (-1, None)])
def test_priority_uses_documented_zero_through_two_without_reversing_urgency(priority, expected):
    rows = dataarts.DataArtsRealtimeCollector().collect(context(ContractClient(jobs=[job(priority=priority)])))
    assert rows[0].attrs["priority"] == expected


@pytest.mark.parametrize("value", [None, 0, 30, 2.5, True, float("inf"), 10**1000])
def test_undocumented_p90_extension_is_never_a_measured_local_baseline(value):
    rows = dataarts.DataArtsBatchCollector().collect(context(ContractClient(instances=[instance(p90_minutes=value)])))
    assert "p90_minutes" not in rows[0].facts and "p90_minutes" not in rows[0].metrics
    assert rows[0].attrs["batch_baseline"]["sample_count"] == 1
    assert rows[0].attrs["batch_baseline"]["status"] == "INSUFFICIENT_SAMPLES"


@pytest.mark.parametrize("value,expected", [("2026-10-27T08:00:00Z", "2026-10-27T08:00:00Z"),
                                          (NOW, NOW), (None, None), (True, None), (10**1000, None)])
def test_realtime_date_is_preserved_without_assuming_batch_millisecond_type(value, expected):
    rows = dataarts.DataArtsRealtimeCollector().collect(context(ContractClient(statuses={
        "job_sms": status(startTime=value)})))
    assert rows[0].attrs["start_time"] == expected


def test_actual_http_request_carries_workspace_correct_query_and_percent_encoded_job_name(monkeypatch):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    name, requests = "中文 空格/folder?x=1", []
    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.headers["workspace"] == "workspace-one"
        if request.url.path == "/v1/synthetic-project/jobs":
            assert dict(request.url.params) == {"offset": "0", "limit": "100", "jobType": "REAL_TIME"}
            return httpx.Response(200, json={"total": 1, "jobs": [job(name)]})
        if request.url.raw_path.decode() == f"/v1/synthetic-project/jobs/{quote(name, safe='')}":
            assert not request.url.query
            return httpx.Response(200, json={"name": name, "processType": "REAL_TIME", "nodes": []})
        assert request.url.raw_path.decode() == f"/v1/synthetic-project/jobs/{quote(name, safe='')}/status"
        assert not request.url.query
        return httpx.Response(200, json=status(name, nodes=[]))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                            EndpointResolver({"dataarts": "https://dataarts.invalid"}, "test", "invalid"),
                            "synthetic-project", qps={"dataarts": 1000})
        ctx = context(current)
        assert dataarts.DataArtsRealtimeCollector().collect(ctx)[0].facts["status"] == "NORMAL"
    assert len(requests) == 3 and not ctx.errors


def test_actual_batch_http_request_uses_collection_detail_path_and_millisecond_window(monkeypatch):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.headers["workspace"] == "workspace-one"
        if request.url.path == "/v1/synthetic-project/jobs":
            assert dict(request.url.params) == {"jobType": "BATCH", "limit": "100", "offset": "0"}
            return httpx.Response(200, json={"total": 1, "jobs": [{"name": "job_batch", "jobType": "BATCH"}]})
        if request.url.path == "/v1/synthetic-project/jobs/job_batch":
            return httpx.Response(200, json={"name": "job_batch", "processType": "BATCH", "nodes": []})
        assert request.url.path == "/v1/synthetic-project/jobs/instances/detail"
        assert dict(request.url.params) == {"offset": "0", "limit": "100",
                                            "minPlanTime": str(NOW - 24 * 3600 * 1000), "maxPlanTime": str(NOW)}
        return httpx.Response(200, json={"total": 1, "instances": [instance()]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                            EndpointResolver({"dataarts": "https://dataarts.invalid"}, "test", "invalid"),
                            "synthetic-project", qps={"dataarts": 1000})
        ctx = context(current)
        assert dataarts.DataArtsBatchCollector().collect(ctx)[0].metrics["run_minutes"] == 0.3
    assert len(requests) == 3 and not ctx.errors


@pytest.mark.parametrize("failed_path,error_code", [("/jobs", "DLF.3051"), ("/jobs/job_sms/status", "DLF.0100")])
def test_official_http_400_error_is_not_an_empty_success_and_raw_message_is_not_exposed(monkeypatch, caplog,
                                                                                    failed_path, error_code):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    requests = []
    raw_message = "synthetic-private-upstream-message"

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.headers["workspace"] == "workspace-one"
        if request.url.path == "/v1/synthetic-project" + failed_path:
            return httpx.Response(400, json={"error_code": error_code, "error_msg": raw_message})
        if request.url.path == "/v1/synthetic-project/jobs/job_sms":
            return httpx.Response(200, json={"name": "job_sms", "processType": "REAL_TIME", "nodes": []})
        assert request.url.path == "/v1/synthetic-project/jobs"
        return httpx.Response(200, json={"total": 1, "jobs": [job()]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                            EndpointResolver({"dataarts": "https://dataarts.invalid"}, "test", "invalid"),
                            "synthetic-project", qps={"dataarts": 1000})
        ctx = context(current)
        rows = dataarts.DataArtsRealtimeCollector().collect(ctx)
        assert rows == [] if failed_path == "/jobs" else len(rows) == 1 and rows[0].facts == {}
    assert len(ctx.errors) == 1 and error_code in ctx.errors[0]
    assert raw_message not in str(ctx.errors) and raw_message not in caplog.text
    assert len(requests) == (1 if failed_path == "/jobs" else 3)


@pytest.mark.parametrize("kind,task_code,metric,per_run_calls", [
    ("realtime", "dgc_realtime_status", "bad_nodes", 3), ("batch", "dgc_batch_instance", "run_minutes", 3)])
def test_real_pipeline_writes_unknown_current_measurement_over_prior_value_and_counts_actual_requests(
        mem_db, monkeypatch, kind, task_code, metric, per_run_calls):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    sync_default_rules(mem_db)
    target = Target(tenant_id=None, code="synthetic", name="Synthetic", short_name="Synthetic", project_ref=None,
                    project_id="synthetic-project", project_name="Synthetic", region="test", workspaces=["workspace-one"])
    phase, requests = [0], []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.headers["workspace"] == "workspace-one"
        if request.url.path == "/v1/synthetic-project/jobs":
            rows = [job()] if kind == "realtime" else [{"name": "job_batch", "jobType": "BATCH"}]
            return httpx.Response(200, json={"total": 1, "jobs": rows})
        if request.url.path in ("/v1/synthetic-project/jobs/job_sms", "/v1/synthetic-project/jobs/job_batch"):
            return httpx.Response(200, json={"name": request.url.path.rsplit("/", 1)[1],
                                           "processType": "REAL_TIME" if kind == "realtime" else "BATCH", "nodes": []})
        if request.url.path == "/v1/synthetic-project/jobs/job_sms/status":
            assert kind == "realtime"
            return httpx.Response(200, json=status(nodes=[]) if phase[0] == 0 else status())
        assert kind == "batch" and request.url.path == "/v1/synthetic-project/jobs/instances/detail"
        return httpx.Response(200, json={"total": 1, "instances": [instance(endTime=NOW) if phase[0] == 0
                                                                   else instance(endTime=None)]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                            EndpointResolver({"dataarts": "https://dataarts.invalid"}, "test", "invalid"),
                            "synthetic-project", qps={"dataarts": 1000})
        first = run_task(mem_db, task_code, client=current, target=target)
        assert first.status == "SUCCESS" and first.resource_total == 1 and first.api_calls == per_run_calls
        resource = mem_db.scalar(select(Resource))
        assert resource is not None and resource.metrics[metric] is not None
        phase[0] = 1
        second = run_task(mem_db, task_code, client=current, target=target)
        assert second.status == "SUCCESS" and second.resource_total == 1 and second.api_calls == per_run_calls
        mem_db.refresh(resource)
        assert resource.metrics[metric] is None
        assert resource.resource_id == "workspace-one/" + ("job_sms" if kind == "realtime" else "job_batch")
    assert len(requests) == 2 * per_run_calls
