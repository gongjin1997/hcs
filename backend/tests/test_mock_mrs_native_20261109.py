"""MRS native Mock 路由：仅 ASGI 合成数据与项目鉴权，保留 DataArts 路由。"""
import pytest
from fastapi.testclient import TestClient

from app.collectors.base import RunContext
from app.collectors.mrs import MrsJobCollector
from mock_hcs import app as mock
from mock_hcs.tenants import TENANT_A, TENANT_B


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(mock, "TOKENS", {"mrs-a": TENANT_A["project_id"], "mrs-b": TENANT_B["project_id"]})
    with TestClient(mock.app, base_url="http://mock") as http:
        http.headers["X-Auth-Token"] = "mrs-a"
        yield http


def path(cid, pid=None):
    return f"/v2/{pid or TENANT_A['project_id']}/clusters/{cid}/job-executions"


def cid():
    return mock.oa.mrs_clusters(TENANT_A["ds"])[0]["clusterId"]


def test_native_mock_page_count_and_separate_same_name_executions(client):
    first = client.get(path(cid()), params={"offset": 1, "limit": 1}).json()
    second = client.get(path(cid()), params={"offset": 2, "limit": 1}).json()
    assert first["total_record"] == second["total_record"] == 2
    assert first["job_list"][0]["job_name"] == second["job_list"][0]["job_name"]
    assert first["job_list"][0]["job_id"] != second["job_list"][0]["job_id"]
    assert first["job_list"][0]["job_state"] == "FINISHED"
    assert first["job_list"][0]["job_result"] == "SUCCEEDED"


def test_native_mock_rejects_unknown_cluster_and_cross_project(client):
    assert client.get(path("absent")).status_code == 404
    assert client.get(path(cid()), headers={"X-Auth-Token": "mrs-b"}).status_code == 403
    assert client.get(path(cid())).status_code == 200
    assert client.get(path(cid()), params={"offset": 0}).status_code == 422
    assert client.post(path(cid())).status_code == 405


def test_native_collector_with_asgi_mock_needs_no_dataarts(client):
    calls = []

    class Local:
        def get(self, service, path, **kwargs):
            assert service == "mrs"
            calls.append(path)
            response = client.get(path.replace("{project_id}", TENANT_A["project_id"]), **kwargs)
            assert response.status_code == 200
            return response.json()

    ctx = RunContext(client=Local(), account="synthetic", project_id=TENANT_A["project_id"])
    rows = MrsJobCollector().collect(ctx)
    assert rows and not ctx.errors
    assert len({r.resource_id for r in rows}) == len(rows)
    assert all(r.attrs["source"] == "mrs_job_execution" for r in rows)
    assert all("cluster_infos" in p or "job-executions" in p for p in calls)
