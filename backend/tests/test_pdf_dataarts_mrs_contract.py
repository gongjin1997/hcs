"""上传 PDF 的响应边界回归，不连接 HCS，也不依赖现有 Mock 的字段定义。

DataArts 2.11.1.3/03 §5.3.15，PDF 实际页357–359（印刷页337–339）：
节点 status 可省略；存在时仅有 STARTING/NORMAL/EXCEPTION/STOPPING/STOPPED。
MRS 3.5.1-LTS/01 §6.1.3，实际页92（印刷页88）：响应对象含 clusters、
clusterTotal，clusterState 为 String。V2 作业未在该 PDF 列出；不为它补造
产品契约或时间单位。非对象响应已有共享 HcsClient 拦截，不重复在采集器实现。
"""
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsRealtimeCollector
from app.collectors.mrs import MrsClusterCollector
from app.core.config import get_settings
from app.hcs import HcsError, ratelimit
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.models import Resource
from app.services.pipeline import run_task
from app.services.tenancy import Target


class Client:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def get(self, service, path, **kwargs):
        self.calls.append((service, path, deepcopy(kwargs)))
        return deepcopy(self.respond(service, path, kwargs))


def context(client):
    return RunContext(client=client, account="synthetic", project_id="synthetic-project",
                      workspaces=["synthetic-workspace"])


@pytest.mark.parametrize("unknown", ["RUNNING", "FAILED", "success", "", "FUTURE_VARIANT"])
def test_unknown_dataarts_node_state_reports_contract_drift_without_losing_known_nodes(unknown):
    def respond(service, path, kwargs):
        assert service == "dataarts"
        assert kwargs["headers"] == {"workspace": "synthetic-workspace"}
        if path == "/v1/{project_id}/jobs":
            return {"total": 1, "jobs": [{"name": "stream", "jobType": "REAL_TIME"}]}
        assert path == "/v1/{project_id}/jobs/stream/status"
        return {"name": "stream", "status": "NORMAL", "nodes": [
            {"name": "known", "status": "EXCEPTION"}, {"name": "unknown", "status": unknown}]}

    ctx = context(Client(respond))
    rows = DataArtsRealtimeCollector().collect(ctx)
    assert len(ctx.errors) == 1 and "节点" in ctx.errors[0]
    parent, known = rows
    assert parent.status_raw == "NORMAL" and parent.metrics == {"nodes": 2, "bad_nodes": None}
    assert "bad_nodes" not in parent.facts
    assert parent.attrs["nodes"][1]["status"] == unknown
    assert known.sub_object == "known" and known.facts["node_status"] == "EXCEPTION"


@pytest.mark.parametrize("state", [[], {}, 0, True, 1.5])
def test_non_string_mrs_cluster_state_never_reaches_resource_or_rule_facts(state):
    client = Client(lambda *_: {"clusterTotal": "1", "clusters": [
        {"clusterId": "one", "clusterName": "one", "clusterState": state}]})
    with pytest.raises(HcsError) as caught:
        MrsClusterCollector().collect(context(client))
    assert caught.value.code == "LOCAL.INVALID_RESPONSE"


@pytest.mark.parametrize("service,task,expected", [
    ("dataarts", "dgc_realtime_status", "PARTIAL"), ("mrs", "mrs_cluster_status", "FAILED")])
def test_http_success_with_invalid_status_does_not_become_a_successful_inspection(
        mem_db, monkeypatch, service, task, expected):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        if service == "mrs":
            assert request.url.path == "/v1.1/synthetic-project/cluster_infos"
            assert "workspace" not in request.headers
            return httpx.Response(200, json={"clusterTotal": "1", "clusters": [
                {"clusterId": "one", "clusterName": "one", "clusterState": []}]})
        assert request.headers["workspace"] == "synthetic-workspace"
        if request.url.path == "/v1/synthetic-project/jobs":
            return httpx.Response(200, json={"total": 1, "jobs": [{"name": "stream", "jobType": "REAL_TIME"}]})
        assert request.url.path == "/v1/synthetic-project/jobs/stream/status"
        return httpx.Response(200, json={"name": "stream", "status": "NORMAL", "nodes": [
            {"name": "unknown", "status": "RUNNING"}]})

    target = Target(tenant_id=None, code="synthetic", name="Synthetic", short_name="Synthetic", project_ref=None,
                    project_id="synthetic-project", project_name="Synthetic", region="test",
                    workspaces=["synthetic-workspace"])
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                           EndpointResolver({service: "https://service.invalid"}, "test", "invalid"),
                           "synthetic-project", qps={service: 1000})
        run = run_task(mem_db, task, client=client, target=target)
    assert run.status == expected and run.error_summary
    assert run.api_calls == len(requests)
    resources = mem_db.scalars(select(Resource)).all()
    if service == "mrs":
        assert resources == []
    else:
        assert len(resources) == 1 and resources[0].metrics["bad_nodes"] is None
