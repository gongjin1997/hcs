"""GaussDB开发Mock只读契约回归；合成数据不证明正式HCS的接口能力。

上传GaussDB25.1.30/01：§4.2.1正文25–27页的used/total为String、GB。
测试仅调用进程内ASGI应用，不连接正式HCS、数据库或通知服务。
"""
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mock_hcs import app as mock
from mock_hcs.tenants import TENANT_A, TENANT_B

NOW_MS = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(mock, "TOKENS", {"gauss-native-test-a": TENANT_A["project_id"],
                                         "gauss-native-test-b": TENANT_B["project_id"],
                                         "gauss-native-test-legacy": "p1"})
    monkeypatch.setattr(mock, "_now_ms", lambda: NOW_MS)
    with TestClient(mock.app, base_url="http://mock") as current:
        current.headers["X-Auth-Token"] = "gauss-native-test-a"
        yield current


def instances(tenant=TENANT_A):
    return mock.oa.gaussdb_instances(tenant["ds"])


def path(instance_id, suffix, tenant=TENANT_A):
    return f"/gaussdb/v3/{tenant['project_id']}/instances/{instance_id}/{suffix}"


def test_volume_usage_returns_string_gb_for_every_active_development_instance(client):
    ds = TENANT_A["ds"]
    active = [row for row in instances() if row["status"] == "ACTIVE"]
    assert active
    for instance in active:
        response = client.get(path(instance["id"], "volume-usage"))
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"used", "total"}
        assert isinstance(body["used"], str) and isinstance(body["total"], str)
        source = next(row for row in ds.GAUSSDB if mock.oa.gaussdb_id(ds, row[0]) == instance["id"])
        assert float(body["total"]) == source[3]
        assert float(body["used"]) == round(source[3] * source[5][2] / 100, 2)


def test_volume_usage_unknown_instance_is_not_an_arbitrary_default(client):
    response = client.get(path("unknown-instance", "volume-usage"))
    assert response.status_code == 404 and response.json()["error_code"] == "APIGW.0101"


def test_volume_usage_rejects_cross_project_credentials(client):
    iid = instances()[0]["id"]
    response = client.get(path(iid, "volume-usage"), headers={"X-Auth-Token": "gauss-native-test-b"})
    assert response.status_code == 403


def test_volume_usage_legacy_mock_project_matches_its_authenticated_scope(client):
    iid = instances()[0]["id"]
    response = client.get(f"/gaussdb/v3/p1/instances/{iid}/volume-usage",
                          headers={"X-Auth-Token": "gauss-native-test-legacy"})
    assert response.status_code == 200
    assert client.get(path(iid, "volume-usage"),
                      headers={"X-Auth-Token": "gauss-native-test-legacy"}).status_code == 403


def nodes(client, instance_id):
    response = client.get(path(instance_id, "components"))
    assert response.status_code == 200
    return response.json()["nodes"]


def slow_body(client, iid, **changes):
    return {"instance_id": iid, "node_ids": [node["id"] for node in nodes(client, iid)],
            "start_time": NOW_MS - 600_000, "end_time": NOW_MS, "threshold": 1, **changes}


def session_body(client, instance_id, **changes):
    node = nodes(client, instance_id)[0]
    return {"node_id": node["id"], "component_id": node["components"][0]["id"],
            "is_statistic_open": "false", "limit": 100, **changes}


def record_path(tenant=TENANT_A, record_id=None):
    base = f"/gaussdb/v3/{tenant['project_id']}/inspections/records"
    return f"{base}/{record_id}" if record_id is not None else base


def test_components_page_by_node_offset_and_keep_nested_component_identity(client):
    iid = instances()[0]["id"]
    all_nodes = nodes(client, iid)
    assert len(all_nodes) == 3
    found = []
    for offset in range(len(all_nodes) + 1):
        response = client.get(path(iid, "components"), params={"offset": offset, "limit": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["total_count"] == len(all_nodes)
        found.extend(body["nodes"])
    assert found == all_nodes and len({node["id"] for node in found}) == len(all_nodes)
    for node in all_nodes:
        assert node["components"]
        for component in node["components"]:
            assert component["id"] and component["type"] == "DN" and component["role"] in ("master", "slave")
    assert client.get(path(iid, "components"), params={"component_type": "CN"}).json() == {
        "nodes": [], "total_count": 0}
    assert client.get(path(iid, "components"), params={"component_type": "DN"}).json()["nodes"] == all_nodes


def test_slow_nodes_match_component_nodes_and_only_accept_slow_action(client):
    iid = instances()[0]["id"]
    response = client.post(path(iid, "slow-sql-execute-node"), json={"action": "slow"})
    assert response.status_code == 200
    rows = response.json()["nodes"]
    expected = {(node["id"], component["type"], component["role"])
                for node in nodes(client, iid) for component in node["components"]}
    assert {(row["node_id"], row["component_type"], row["role"]) for row in rows} == expected
    assert all(row["instance_id"] == iid for row in rows)
    for body in ({}, {"action": "execute"}, {"action": "terminate"}):
        assert client.post(path(iid, "slow-sql-execute-node"), json=body).status_code == 400


def test_slow_sql_uses_requested_nodes_milliseconds_and_second_threshold(client):
    iid = instances()[0]["id"]
    body = slow_body(client, iid)
    response = client.post(path(iid, "slow-sql-list"), json=body)
    assert response.status_code == 200
    rows = response.json()["slow_sql_infos"]
    assert response.json()["total"] == len(rows) == 3
    assert all(isinstance(row["avg_exec_time"], str) and float(row["avg_exec_time"]) >= 1_000_000 for row in rows)
    selected = [body["node_ids"][0]]
    one = client.post(path(iid, "slow-sql-list"), json={**body, "node_ids": selected}).json()
    assert one["total"] == 1 and one["slow_sql_infos"][0]["node_id"] in selected
    higher = client.post(path(iid, "slow-sql-list"), json={**body, "threshold": 2}).json()
    assert higher["total"] == 2
    assert all(float(row["avg_exec_time"]) >= 2_000_000 for row in higher["slow_sql_infos"])
    for changes in ({"threshold": 99}, {"start_time": NOW_MS - 3_600_000, "end_time": NOW_MS - 1_800_000},
                    {"sql_id": "nonexistent-sql-query-filter"}):
        assert client.post(path(iid, "slow-sql-list"), json={**body, **changes}).json() == {
            "slow_sql_infos": [], "total": 0}


def test_slow_sql_detail_uses_selected_sql_and_native_microsecond_total_time(client):
    iid = instances()[0]["id"]
    body = slow_body(client, iid)
    original = client.post(path(iid, "slow-sql-list"), json=body).json()["slow_sql_infos"][0]
    query = {**body, "node_ids": [original["node_id"]], "sql_id": original["sql_id"]}
    query.pop("threshold")
    response = client.post(path(iid, "slow-sql-detail"), json=query)
    assert response.status_code == 200 and response.json()["total"] == 1
    detail, = response.json()["slow_sql_details"]
    assert detail["sql_id"] == original["sql_id"] and detail["node_id"] == original["node_id"]
    assert detail["total_time"] == original["avg_exec_time"] and "duration_time" not in detail
    assert detail["start_time"].endswith("+0000") and detail["finish_time"].endswith("+0000")
    assert client.post(path(iid, "slow-sql-detail"), json={**query, "threshold": 99}).json()["total"] == 0


@pytest.mark.parametrize("changes", [{"start_time": NOW_MS // 1000}, {"end_time": str(NOW_MS)},
                                    {"start_time": True}, {"end_time": NOW_MS - 900_000},
                                    {"threshold": 0}, {"threshold": 100}, {"threshold": True},
                                    {"threshold": "1"}, {"node_ids": []}, {"node_ids": "node"},
                                    {"node_ids": [None]}, {"instance_id": "another-instance"}])
def test_slow_sql_invalid_query_is_not_replaced_with_a_valid_default(client, changes):
    iid = instances()[0]["id"]
    response = client.post(path(iid, "slow-sql-list"), json=slow_body(client, iid, **changes))
    assert response.status_code == 400


def test_foreign_and_unknown_slow_nodes_are_not_silently_dropped(client):
    iid, other = [row["id"] for row in instances()[:2]]
    foreign = nodes(client, other)[0]["id"]
    for node_ids in ([foreign], ["unknown-node"]):
        response = client.post(path(iid, "slow-sql-list"), json=slow_body(client, iid, node_ids=node_ids))
        assert response.status_code == 404


def test_sessions_require_matching_node_component_and_disable_statistics(client):
    iid = instances()[0]["id"]
    body = session_body(client, iid)
    before = deepcopy(TENANT_A["ds"].GAUSSDB)
    response = client.post(path(iid, "real-time-session"), json=body)
    assert response.status_code == 200
    rows = response.json()["sessions"]
    assert len(rows) == 2 and all(not {"trans_num", "rollback_num", "sql_num"} & row.keys() for row in rows)
    assert all(isinstance(row["back_end_start"], int) and isinstance(row["exec_time"], str) for row in rows)
    assert client.post(path(iid, "real-time-session"), json=body).json() == response.json()
    assert TENANT_A["ds"].GAUSSDB == before
    mismatched = nodes(client, iid)[1]["components"][0]["id"]
    for changes in ({"node_id": "unknown-node"}, {"component_id": "unknown-component"},
                    {"component_id": mismatched}):
        assert client.post(path(iid, "real-time-session"), json={**body, **changes}).status_code == 404


def test_sessions_apply_query_info_and_limit_without_enabling_counters(client):
    iid = instances()[0]["id"]
    body = session_body(client, iid)
    one = client.post(path(iid, "real-time-session"), json={**body, "limit": 1}).json()["sessions"]
    assert len(one) == 1
    filtered = client.post(path(iid, "real-time-session"), json={**body, "query_info": {
        "database_name": "postgres", "wait": "t", "client_ip": "192.0.2.11"}}).json()["sessions"]
    assert len(filtered) == 1 and filtered[0]["wait"] == "t"
    assert client.post(path(iid, "real-time-session"), json={**body, "query_info": {
        "user_name": "not-a-mock-user"}}).json() == {"sessions": []}


@pytest.mark.parametrize("changes", [{"is_statistic_open": "true"}, {"is_statistic_open": True},
                                    {"is_statistic_open": None}, {"limit": 0}, {"limit": 201}, {"limit": True},
                                    {"query_info": []}, {"query_info": {"wait": "yes"}},
                                    {"query_info": {"terminate": "true"}}])
def test_session_invalid_or_stateful_query_is_rejected(client, changes):
    iid = instances()[0]["id"]
    assert client.post(path(iid, "real-time-session"), json=session_body(client, iid, **changes)).status_code == 400


def test_inspection_records_are_completed_project_instances_and_have_stable_pagination(client):
    query = {"limit": 100, "offset": 0, "status": "completed"}
    response = client.post(record_path(), json=query)
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == len(instances())
    assert {record["instance_id"] for record in body["records"]} == {row["id"] for row in instances()}
    assert all(record["status"] == "completed" and record["progress"] == "100" for record in body["records"])
    found = []
    for offset in range(body["total_count"] + 1):
        page = client.post(record_path(), json={**query, "limit": 1, "offset": offset}).json()
        assert page["total_count"] == body["total_count"]
        found.extend(page["records"])
    assert found == body["records"]
    assert client.post(record_path(), json={**query, "status": "running"}).json() == {"records": [], "total_count": 0}
    first = body["records"][0]
    filtered = client.post(record_path(), json={**query, "instance_name": first["instance_name"],
        "inspection_start_time_range": {"start_time": first["start_time"] - 1,
                                         "end_time": first["end_time"]}}).json()
    assert filtered["records"] == [first] and filtered["total_count"] == 1
    empty = client.post(record_path(), json={**query, "inspection_start_time_range": {
        "start_time": NOW_MS, "end_time": NOW_MS + 1000}}).json()
    assert empty == {"records": [], "total_count": 0}


def test_inspection_detail_keeps_owner_and_official_parallel_time_metric_arrays(client):
    record = client.post(record_path(), json={"limit": 1, "offset": 0}).json()["records"][0]
    response = client.get(record_path(record_id=record["record_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["instance_id"] == record["instance_id"]
    assert (body["start_time"], body["end_time"]) == (record["start_time"], record["end_time"])
    assert body["total_count"] == len(body["inspection_results"]) == 3
    assert {item["name"] for item in body["inspection_results"]} == {"CPU使用率", "内存使用率", "磁盘使用率"}
    for check in body["inspection_results"]:
        assert "Mock" in check["description"]
        for indicator in check["indicators"]:
            assert indicator["unit"] == "%"
            for metric in indicator["metrics"]:
                assert isinstance(metric["data"], dict) and set(metric["data"]) == {"time", "metrics"}
                times, values = metric["data"]["time"], metric["data"]["metrics"]
                assert len(times) == len(values) == 3 and times == sorted(times)
                assert all(type(stamp) is int and 10**12 <= stamp < 10**13 for stamp in times)
                assert all(isinstance(value, (int, float)) for value in values)
                assert metric["max"] == max(values) and metric["min"] == min(values)
    assert client.get(record_path(record_id="unknown-record")).status_code == 404


def test_records_and_details_cannot_leak_to_another_project(client):
    record = client.post(record_path(), json={"limit": 1, "offset": 0}).json()["records"][0]
    headers = {"X-Auth-Token": "gauss-native-test-b"}
    assert client.post(record_path(), headers=headers, json={"limit": 1, "offset": 0}).status_code == 403
    response = client.post(record_path(TENANT_B), headers=headers, json={"limit": 100, "offset": 0})
    assert response.status_code == 200 and not response.json()["records"]
    assert client.get(record_path(TENANT_B, record["record_id"]), headers=headers).status_code == 404
    iid = instances()[0]["id"]
    assert client.get(path(iid, "volume-usage", TENANT_B), headers=headers).status_code == 404


@pytest.mark.parametrize("suffix,method,body", [
    ("volume-usage", "GET", None), ("components", "GET", None),
    ("slow-sql-execute-node", "POST", {"action": "slow"}),
    ("slow-sql-list", "POST", {}), ("slow-sql-detail", "POST", {}), ("real-time-session", "POST", {}),
])
def test_all_instance_native_routes_check_authentication_and_instance_ownership(client, suffix, method, body):
    assert client.request(method, path("unknown-instance", suffix), json=body).status_code == 404
    iid = instances()[0]["id"]
    assert client.request(method, path(iid, suffix), json=body,
                          headers={"X-Auth-Token": "gauss-native-test-b"}).status_code == 403
    assert client.request(method, path(iid, suffix), json=body,
                          headers={"X-Auth-Token": "invalid-token"}).status_code == 401


@pytest.mark.parametrize("suffix", ["slow-sql-list", "slow-sql-detail", "slow-sql-execute-node", "real-time-session"])
@pytest.mark.parametrize("body", [[], "not-an-object", None])
def test_native_post_rejects_non_object_bodies(client, suffix, body):
    iid = instances()[0]["id"]
    assert client.post(path(iid, suffix), json=body).status_code == 400


@pytest.mark.parametrize("suffix", ["action", "volume", "resize", "kill-session", "terminate-session",
                                  "execute-sql", "limit-task"])
def test_native_development_mock_has_no_write_or_terminate_routes(client, suffix):
    iid = instances()[0]["id"]
    assert client.post(path(iid, suffix), json={}).status_code in (404, 405)


@pytest.mark.parametrize("query", [{"limit": 0, "offset": 0}, {"limit": True, "offset": 0},
                                  {"limit": 1, "offset": -1}, {"limit": 101, "offset": 0},
                                  {"limit": 1, "offset": 0, "inspection_start_time_range": {
                                      "start_time": NOW_MS // 1000}},
                                  {"limit": 1, "offset": 0, "sort": "nonexistent"}])
def test_invalid_inspection_pagination_or_time_is_not_silently_ignored(client, query):
    assert client.post(record_path(), json=query).status_code == 400
