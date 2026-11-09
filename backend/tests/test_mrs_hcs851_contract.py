"""HCS 8.5.1 MRS 集群契约，以及经 DataArts 作业实例详情（数据治理中心 API §5.3.16 / §5.3.17）采集的 MRS 作业。"""
from copy import deepcopy
from unittest.mock import Mock
from urllib.parse import unquote

import pytest

from app.collectors.base import RunContext
from app.collectors.mrs import DataArtsMrsNodeCollector, MrsClusterCollector
from app.hcs import HcsError
from app.models import Resource


def cluster(cid="cluster-a", state="running"):
    # EDOC1100452559 / Querying a Cluster List：总数和节点数是字符串，状态为小写。
    return {"clusterId": cid, "clusterName": f"mrs-{cid}", "clusterState": state,
            "clusterVersion": "MRS 3.5.1-LTS", "totalNodeNum": "5", "createAt": "1710000000"}


class Client:
    def __init__(self, callback):
        self.callback, self.requests = callback, []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        return deepcopy(self.callback(service, path, kwargs))


def ctx(client, **params):
    return RunContext(client=client, account="synthetic", project_id="project-a", params=params,
                      tenant_id=11, project_ref=111)


def cluster_client(clusters):
    def fetch(service, path, kwargs):
        assert service == "mrs" and path.endswith("cluster_infos")
        return {"clusters": clusters, "clusterTotal": str(len(clusters))}
    return Client(fetch)


def test_hcs851_cluster_list_uses_current_page_and_string_total():
    rows = [cluster(f"cluster-{i}") for i in range(51)]

    def fetch(service, path, kwargs):
        assert service == "mrs" and path == "/v1.1/{project_id}/cluster_infos"
        params = kwargs["params"]
        assert set(params) == {"currentPage", "pageSize"}
        begin = (params["currentPage"] - 1) * params["pageSize"]
        return {"clusterTotal": "51", "clusters": rows[begin:begin + params["pageSize"]]}

    client = Client(fetch)
    result = MrsClusterCollector().collect(ctx(client))
    assert len(result) == 51
    assert [x[2]["params"] for x in client.requests] == [
        {"currentPage": 1, "pageSize": 50}, {"currentPage": 2, "pageSize": 50}]
    assert result[0].facts == {"cluster_state": "running"}
    assert result[0].attrs == {"version": "MRS 3.5.1-LTS", "nodes": "5"}


@pytest.mark.parametrize("state", ["starting", "running", "terminated", "failed", "abnormal", "terminating",
                                  "frozen", "scaling-out", "scaling-in"])
def test_hcs851_cluster_states_preserved_without_public_cloud_enum_conversion(state):
    result = MrsClusterCollector().collect(ctx(cluster_client([cluster(state=state)])))
    assert result[0].status_raw == state
    assert result[0].facts["cluster_state"] == state


@pytest.mark.parametrize("data", [{}, {"clusters": []}, {"clusters": [], "clusterTotal": None},
                                  {"clusters": None, "clusterTotal": "0"},
                                  {"clusters": [], "clusterTotal": "1"},
                                  {"clusters": [cluster(), cluster()], "clusterTotal": "2"},
                                  {"clusters": [{"clusterName": "missing-id"}], "clusterTotal": "1"}])
def test_invalid_or_incomplete_cluster_list_never_means_no_clusters(data):
    with pytest.raises(HcsError):
        MrsClusterCollector().collect(ctx(Client(lambda *_: data)))


def test_missing_mrs_service_is_an_error_not_an_empty_success():
    def unavailable(*_):
        raise HcsError(404, "NotFound", "synthetic service absent")
    with pytest.raises(HcsError) as error:
        MrsClusterCollector().collect(ctx(Client(unavailable)))
    assert error.value.status == 404


NOW = 1_800_000_000_000
HOUR = 3_600_000
LIST_PATH = "/v1/{project_id}/jobs/instances/detail"


def inst(name="job-a", iid=1, status="success", plan=NOW - 2 * HOUR, **kwargs):
    # 数据治理中心 API 表 5-178：毫秒时间戳，instanceId 为 Long
    return {"jobName": name, "instanceId": iid, "status": status, "planTime": plan, "submitTime": plan,
            "startTime": plan + 1000, "endTime": plan + 601000, **kwargs}


def node(name="spark", kind="MRSSpark", status="success", start=NOW - 2 * HOUR, end=NOW - HOUR, **kwargs):
    # 表 5-182：nodeName / type / status / startTime / endTime / retryTimes / logPath
    return {"nodeName": name, "type": kind, "status": status, "startTime": start, "endTime": end,
            "retryTimes": 0, "logPath": f"obs://logs/{name}", **kwargs}


class DataArtsClient:
    """合成 DataArts：实例列表 + 实例详情；记录所有请求。"""

    def __init__(self, instances, nodes=None, fail=()):
        self.instances, self.nodes, self.fail = instances, nodes or {}, set(fail)
        self.requests = []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        assert service == "dataarts"
        if path == LIST_PATH:
            return {"total": len(self.instances), "instances": deepcopy(self.instances)}
        name, _, iid = path.split("/jobs/", 1)[1].rpartition("/instances/")
        name, iid = unquote(name), int(iid)
        if (name, iid) in self.fail:
            raise HcsError(503, "SYNTHETIC.ERROR", "synthetic failure")
        row = next(i for i in self.instances if i["jobName"] == name and i["instanceId"] == iid)
        return {"jobName": name, "instanceId": iid, "status": row["status"], "total": len(self.nodes[(name, iid)]),
                "nodes": deepcopy(self.nodes[(name, iid)])}

    def details(self):
        return [r[1] for r in self.requests if r[1] != LIST_PATH]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.collectors.mrs.time.time", lambda: NOW / 1000)


def job_ctx(client, **params):
    return RunContext(client=client, account="synthetic", project_id="project-a", workspaces=["ws-1"], params=params,
                      tenant_id=11, project_ref=111)


def test_only_mrs_node_types_become_observations_with_node_level_identity():
    nodes = [node("hive", "SparkSQL"), node("spark", "MRSSpark"), node("mr", "MapReduce", "fail"),
             node("flink", "MRSFlinkJob", "running", end=None), node("hetu", "MRSHetuEngine", "waiting", end=None),
             node("done", "Dummy")]
    client = DataArtsClient([inst(status="fail")], {("job-a", 1): nodes})
    result = DataArtsMrsNodeCollector().collect(job_ctx(client))
    assert [o.resource_id for o in result] == ["ws-1/job-a/hive", "ws-1/job-a/spark", "ws-1/job-a/mr", "ws-1/job-a/flink",
                                               "ws-1/job-a/hetu"]
    assert all(o.resource_type == "dgc_mrs_node" and o.check_item == "job_status" for o in result)
    failed = result[2]
    assert failed.facts["node_status"] == "fail" and failed.facts["job_status"] == "fail"
    assert failed.attrs["node_type"] == "MapReduce" and failed.attrs["instance_id"] == 1
    assert failed.attrs["source"] == "dataarts_instance_detail" and failed.status_raw == "fail"
    assert failed.attrs["log_path"] == "obs://logs/mr"


def test_run_minutes_use_millisecond_timestamps_and_running_nodes_end_now():
    nodes = [node("done", start=NOW - 2 * HOUR, end=NOW - 2 * HOUR + 150_000),
             node("live", "MapReduce", "running", start=NOW - 90_000, end=None)]
    client = DataArtsClient([inst(status="running", endTime=None)], {("job-a", 1): nodes})
    done, live = DataArtsMrsNodeCollector().collect(job_ctx(client))
    assert done.facts["run_minutes"] == 2.5 == done.metrics["run_minutes"]
    assert live.facts["run_minutes"] == 1.5


def test_missing_times_are_unknown_not_zero():
    nodes = [node(start=None, end=None)]
    result = DataArtsMrsNodeCollector().collect(job_ctx(DataArtsClient([inst()], {("job-a", 1): nodes})))
    assert "run_minutes" not in result[0].facts and result[0].metrics["run_minutes"] is None


def test_query_window_is_forwarded_to_the_instance_list_and_details_use_the_workspace_header():
    client = DataArtsClient([inst()], {("job-a", 1): [node()]})
    DataArtsMrsNodeCollector().collect(job_ctx(client, window_hours=48))
    first = client.requests[0]
    assert first[1] == LIST_PATH and first[2]["headers"] == {"workspace": "ws-1"}
    assert first[2]["params"]["minPlanTime"] == NOW - 48 * HOUR and first[2]["params"]["maxPlanTime"] == NOW
    assert client.requests[1][2]["headers"] == {"workspace": "ws-1"}


def test_only_the_latest_instance_of_each_job_is_queried_for_details():
    old, new = inst(iid=1, plan=NOW - 5 * HOUR, status="fail"), inst(iid=2, plan=NOW - HOUR, status="success")
    client = DataArtsClient([old, new], {("job-a", 1): [node(status="fail")], ("job-a", 2): [node()]})
    result = DataArtsMrsNodeCollector().collect(job_ctx(client))
    assert len(result) == 1 and result[0].attrs["instance_id"] == 2 and result[0].facts["node_status"] == "success"
    assert client.details() == ["/v1/{project_id}/jobs/job-a/instances/2"]


def test_job_name_is_url_encoded_in_the_detail_path():
    client = DataArtsClient([inst(name="日报 a/b", iid=5)], {("日报 a/b", 5): [node()]})
    DataArtsMrsNodeCollector().collect(job_ctx(client))
    assert client.details() == ["/v1/{project_id}/jobs/%E6%97%A5%E6%8A%A5%20a%2Fb/instances/5"]


def test_unrecognized_node_status_is_reported_and_never_treated_as_known():
    client = DataArtsClient([inst()], {("job-a", 1): [node(status="NEW_VARIANT")]})
    context = job_ctx(client)
    result = DataArtsMrsNodeCollector().collect(context)
    assert "node_status" not in result[0].facts and result[0].status_raw == "NEW_VARIANT"
    assert context.errors and "节点状态" in context.errors[0]


@pytest.mark.parametrize("bad_nodes", [None, "x", [{"type": "MRSSpark"}], [node("a"), node("a")],
                                       [node(status=3)], [node(kind=3)]])
def test_malformed_detail_nodes_fail_that_job_only(bad_nodes):
    client = DataArtsClient([inst("bad", 1), inst("good", 2)],
                            {("bad", 1): bad_nodes, ("good", 2): [node()]})
    original = client.get

    def get(service, path, **kwargs):
        body = original(service, path, **kwargs) if path == LIST_PATH or "bad" not in path else {
            "jobName": "bad", "instanceId": 1, "nodes": bad_nodes}
        return body
    client.get = get
    context = job_ctx(client)
    result = DataArtsMrsNodeCollector().collect(context)
    assert [o.attrs["job_name"] for o in result] == ["good"] and len(context.errors) == 1


def test_detail_failure_is_partial_and_other_jobs_survive():
    client = DataArtsClient([inst("a", 1), inst("b", 2)], {("a", 1): [node()], ("b", 2): [node()]}, fail={("a", 1)})
    context = job_ctx(client)
    result = DataArtsMrsNodeCollector().collect(context)
    assert [o.attrs["job_name"] for o in result] == ["b"]
    assert len(context.errors) == 1 and context.errors[0].startswith("job=a")


def test_instance_list_failure_is_a_workspace_error_not_an_empty_success():
    class Failing:
        def get(self, *args, **kwargs):
            raise HcsError(404, "SYNTHETIC.ERROR", "synthetic")
    context = job_ctx(Failing())
    assert DataArtsMrsNodeCollector().collect(context) == [] and len(context.errors) == 1


@pytest.mark.parametrize("bad", [{}, {"instances": []}, {"instances": [], "total": 1}, {"instances": "x", "total": 0},
                                 {"instances": [inst(name="")], "total": 1},
                                 {"instances": [{**inst(), "instanceId": None}], "total": 1},
                                 {"instances": [{**inst(), "status": None}], "total": 1}])
def test_bad_instance_list_reports_error_instead_of_healthy_empty(bad):
    class Bad:
        def get(self, *args, **kwargs):
            return bad
    context = job_ctx(Bad())
    assert DataArtsMrsNodeCollector().collect(context) == []
    assert context.errors


def test_detail_cap_queries_newest_instances_first_and_reports_the_truncation():
    jobs = [inst(f"j{i}", i, plan=NOW - (10 - i) * HOUR) for i in range(1, 4)]
    client = DataArtsClient(jobs, {(f"j{i}", i): [node()] for i in range(1, 4)})
    context = job_ctx(client, max_details=2)
    result = DataArtsMrsNodeCollector().collect(context)
    assert {o.attrs["job_name"] for o in result} == {"j3", "j2"}
    assert len(context.errors) == 1 and "上限" in context.errors[0]


def test_terminal_instances_without_mrs_nodes_are_not_queried_again(mem_db):
    jobs = [inst("etl", 1, status="success"), inst("live", 2, status="running", endTime=None)]
    nodes = {("etl", 1): [node("shell", "Shell")], ("live", 2): [node("shell", "Shell", "running", end=None)]}
    first = DataArtsClient(jobs, nodes)
    context = job_ctx(first)
    context.db = mem_db
    assert DataArtsMrsNodeCollector().collect(context) == [] and sorted(first.details()) == [
        "/v1/{project_id}/jobs/etl/instances/1", "/v1/{project_id}/jobs/live/instances/2"]
    mem_db.commit()
    second = DataArtsClient(jobs, nodes)
    context = job_ctx(second)
    context.db = mem_db
    DataArtsMrsNodeCollector().collect(context)
    # 已结束的实例详情不会再变；运行中的实例每轮重新查询
    assert second.details() == ["/v1/{project_id}/jobs/live/instances/2"]
    assert context.stats["dgc_mrs_nodes"]["details_skipped"] == 1


def test_a_new_instance_of_a_scanned_job_is_queried_again(mem_db):
    nodes = {("etl", 1): [node("shell", "Shell")], ("etl", 2): [node("spark")]}
    context = job_ctx(DataArtsClient([inst("etl", 1)], nodes))
    context.db = mem_db
    DataArtsMrsNodeCollector().collect(context)
    mem_db.commit()
    second = DataArtsClient([inst("etl", 1), inst("etl", 2, plan=NOW - HOUR)], nodes)
    context = job_ctx(second)
    context.db = mem_db
    result = DataArtsMrsNodeCollector().collect(context)
    assert second.details() == ["/v1/{project_id}/jobs/etl/instances/2"] and len(result) == 1


def test_terminal_instances_with_mrs_nodes_reuse_the_stored_resource(mem_db):
    jobs = [inst("etl", 7)]
    nodes = {("etl", 7): [node("spark", status="fail", end=NOW - HOUR + 60_000)]}
    context = job_ctx(DataArtsClient(jobs, nodes))
    context.db = mem_db
    first = DataArtsMrsNodeCollector().collect(context)
    mem_db.add(Resource(service="dataarts", resource_type="dgc_mrs_node", resource_id=first[0].resource_id, name=first[0].name,
                        tenant_id=11, project_ref=111, status_raw="fail", attrs=first[0].attrs,
                        metrics=first[0].metrics))
    mem_db.commit()
    second = DataArtsClient(jobs, nodes)
    context = job_ctx(second)
    context.db = mem_db
    again = DataArtsMrsNodeCollector().collect(context)
    assert second.details() == [] and len(again) == 1
    assert again[0].resource_id == first[0].resource_id and again[0].facts["node_status"] == "fail"
    assert context.stats["dgc_mrs_nodes"]["details_reused"] == 1


@pytest.mark.parametrize("params", [{"window_hours": 0}, {"window_hours": "NaN"}, {"window_hours": True},
                                    {"window_hours": 73}, {"max_details": 0}, {"max_details": "5"}])
def test_invalid_configuration_fails_before_cloud_request(params):
    client = Mock()
    with pytest.raises(HcsError):
        DataArtsMrsNodeCollector().collect(job_ctx(client, **params))
    client.get.assert_not_called()


def test_mrs_job_collector_never_calls_the_mrs_service():
    client = DataArtsClient([inst()], {("job-a", 1): [node()]})
    DataArtsMrsNodeCollector().collect(job_ctx(client))
    assert {r[0] for r in client.requests} == {"dataarts"}
