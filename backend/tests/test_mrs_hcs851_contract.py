"""HCS 8.5.1 原生集群契约；未在 851 目录核实的 V2 作业仅测试显式兼容配置。"""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.collectors.base import RunContext
from app.collectors.mrs import MrsClusterCollector, MrsJobCollector
from app.hcs import HcsError


def cluster(cid="cluster-a", state="running"):
    # EDOC1100452559 / Querying a Cluster List：总数和节点数是字符串，状态为小写。
    return {"clusterId": cid, "clusterName": f"mrs-{cid}", "clusterState": state,
            "clusterVersion": "MRS 3.5.1-LTS", "totalNodeNum": "5", "createAt": "1710000000"}


def job(name="job-a", submitted=1710000000000, elapsed=120000, **kwargs):
    return {"job_name": name, "job_id": f"exec-{name}", "submitted_time": submitted,
            "job_state": "RUNNING", "job_result": "UNDEFINED", "job_type": "SparkSubmit",
            "elapsed_time": elapsed, "started_time": "1710000001000", "finished_time": "0", **kwargs}


class Client:
    def __init__(self, callback):
        self.callback, self.requests = callback, []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        return deepcopy(self.callback(service, path, kwargs))


def ctx(client, **params):
    return RunContext(client=client, account="synthetic", project_id="project-a", params=params,
                      tenant_id=11, project_ref=111)


def job_client(rows, *, clusters=None):
    def fetch(service, path, kwargs):
        assert service == "mrs"
        if path.endswith("cluster_infos"):
            return {"clusters": clusters or [cluster()], "clusterTotal": str(len(clusters or [cluster()]))}
        assert path.endswith("job-executions")
        return {"job_list": rows, "total_record": len(rows)}
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
    result = MrsClusterCollector().collect(ctx(job_client([], clusters=[cluster(state=state)])))
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


def test_job_duration_without_site_unit_is_missing_and_collection_reports_limitation():
    context = ctx(job_client([job()]))
    result = MrsJobCollector().collect(context)
    assert result[0].facts["elapsed_min"] is None
    assert result[0].metrics["elapsed_min"] is None
    assert result[0].attrs["elapsed_time"] == 120000
    assert result[0].attrs["elapsed_time_unit"] is None
    assert context.stats["job_api_contract"] == "unverified_hcs851_compatibility"
    assert len(context.errors) == 1 and "单位" in context.errors[0]


@pytest.mark.parametrize(("unit", "raw", "expected"), [("milliseconds", "120000", 2.0),
                                                        ("seconds", "120", 2.0),
                                                        ("milliseconds", 0, 0.0), ("seconds", 0, 0.0)])
def test_compatibility_duration_requires_explicit_units(unit, raw, expected):
    context = ctx(job_client([job(elapsed=raw)]), elapsed_time_unit=unit)
    result = MrsJobCollector().collect(context)[0]
    assert result.metrics["elapsed_min"] == expected == result.facts["elapsed_min"]
    assert result.attrs["start_time"] == "1710000001000"
    assert result.attrs["end_time"] == "0"
    assert context.errors == []


@pytest.mark.parametrize("raw", [None, "", "missing", True, -1, "NaN", "inf", [], {}])
def test_missing_or_invalid_elapsed_is_never_zero(raw):
    context = ctx(job_client([job(elapsed=raw)]), elapsed_time_unit="milliseconds")
    result = MrsJobCollector().collect(context)[0]
    assert result.metrics["elapsed_min"] is None
    assert result.facts["elapsed_min"] is None
    assert bool(context.errors) is (raw is not None)


def test_development_mock_unit_is_explicit_synthetic_contract_only():
    client = job_client([job()])
    client.mock_url = "http://synthetic-mock"
    context = ctx(client)
    assert MrsJobCollector().collect(context)[0].metrics["elapsed_min"] == 2.0
    assert not context.errors


def test_numeric_string_submitted_time_selects_latest_without_string_ordering():
    context = ctx(job_client([job(submitted="9", job_id="old"), job(submitted="11", job_id="new")]),
                  elapsed_time_unit="milliseconds")
    result = MrsJobCollector().collect(context)
    assert len(result) == 1 and result[0].attrs["job_id"] == "new"


@pytest.mark.parametrize("state", ["ACCEPTED", "RUNNING", "FINISHED", "FAILED", "KILLED", "PENDING", "NEW_VARIANT"])
def test_compatibility_job_state_and_result_are_separate_raw_values(state):
    context = ctx(job_client([job(job_state=state, job_result="UNDEFINED")]), elapsed_time_unit="milliseconds")
    result = MrsJobCollector().collect(context)[0]
    assert result.status_raw == state and result.facts["job_state"] == state
    assert result.facts["job_result"] == "UNDEFINED"


@pytest.mark.parametrize("mode,offsets", [("page", [1, 2, 3]), ("offset", [1, 3, 5])])
def test_explicit_compatibility_paging_does_not_drop_last_record(mode, offsets, monkeypatch):
    monkeypatch.setattr("app.collectors.mrs.time.time", lambda: 1800000000)
    rows = [job(name=f"j{i}") for i in range(5)]

    def fetch(service, path, kwargs):
        if path.endswith("cluster_infos"):
            return {"clusters": [cluster()], "clusterTotal": "1"}
        p = kwargs["params"]
        begin = (p["offset"] - 1) * 2 if mode == "page" else p["offset"] - 1
        # 服务端缩小每页条数；有 total_record 时不能以短页提早结束。
        assert p["submitted_time_begin"] == 1800000000000 - 6 * 3600000
        return {"job_list": rows[begin:begin + 2], "total_record": 5}

    client = Client(fetch)
    context = ctx(client, elapsed_time_unit="milliseconds", job_offset_mode=mode)
    assert len(MrsJobCollector().collect(context)) == 5
    assert [r[2]["params"]["offset"] for r in client.requests[1:]] == offsets
    assert all(r[2]["params"]["limit"] == 100 for r in client.requests[1:])


@pytest.mark.parametrize("bad", [{}, {"job_list": []}, {"job_list": [], "total_record": 1},
                                 {"job_list": "invalid", "total_record": 0},
                                 {"job_list": [job(submitted=None)], "total_record": 1},
                                 {"job_list": [job(job_name="")], "total_record": 1}])
def test_bad_job_list_reports_partial_instead_of_healthy_empty(bad):
    client = job_client([])
    original = client.callback
    client.callback = lambda service, path, kw: bad if path.endswith("job-executions") else original(service, path, kw)
    context = ctx(client, elapsed_time_unit="milliseconds")
    assert MrsJobCollector().collect(context) == []
    assert context.errors


def test_later_job_page_failure_discards_incomplete_cluster_but_preserves_other_cluster():
    def fetch(service, path, kwargs):
        if path.endswith("cluster_infos"):
            return {"clusters": [cluster("a"), cluster("b")], "clusterTotal": "2"}
        if "/a/" in path:
            if kwargs["params"]["offset"] == 1:
                return {"job_list": [job(name="partial")], "total_record": 2}
            raise HcsError(503, "Unavailable", "synthetic failure")
        return {"job_list": [job(name="complete")], "total_record": 1}
    context = ctx(Client(fetch), elapsed_time_unit="milliseconds")
    result = MrsJobCollector().collect(context)
    assert [row.name for row in result] == ["complete"]
    assert len(context.errors) == 1


@pytest.mark.parametrize("params", [{"window_hours": 0}, {"window_hours": "NaN"}, {"window_hours": True},
                                    {"window_hours": 745}, {"elapsed_time_unit": "guess"},
                                    {"job_offset_mode": "guess"}])
def test_invalid_compatibility_configuration_fails_before_cloud_request(params):
    client = Mock()
    with pytest.raises(HcsError):
        MrsJobCollector().collect(ctx(client, **params))
    client.get.assert_not_called()
