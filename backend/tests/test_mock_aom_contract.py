"""开发 Mock 的旧协议夹具保留；生产客户端一律禁止 AOM。

直接调用 TestClient 的协议断言只验证本地夹具，不代表产品或生产环境具备 AOM 能力。
"""
import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector
from app.collectors.ecs import EcsStatusCollector
from app.collectors.gaussdb import GaussDBCollector
from app.core.config import Settings
from app.hcs import HcsError, auth, build_client, signer
from app.ledger.sync import sync_ledger
from app.metrics import aom
from app.models import EcsMetricSample, InspectionTask, Resource
from app.services.pipeline import run_task
from app.services.tenancy import resolve_targets
from mock_hcs import app as mock
from mock_hcs.tenants import TENANT_A, TENANT_B

NAMESPACE = "CUSTOM.HCS_INSPECTOR_TEST"
NOW = datetime(2026, 10, 27, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


@pytest.fixture()
def mock_client(monkeypatch):
    monkeypatch.setattr(mock, "TOKENS", {})
    monkeypatch.setattr(mock, "_now_ms", lambda: NOW_MS)
    monkeypatch.setattr(auth, "_tokens", OrderedDict())
    with TestClient(mock.app, base_url="http://mock") as client:
        yield client


def token_headers(client, tenant=TENANT_A, *, project_id=None):
    response = client.post("/v3/auth/tokens", json={"auth": {
        "identity": {"methods": ["password"], "password": {"user": {
            "name": "mock-contract-only", "password": "synthetic-mock-only", "domain": {"name": tenant["domain_name"]}}}},
        "scope": {"project": {"id": project_id or tenant["project_id"]}}}})
    assert response.status_code == 201
    return {"X-Auth-Token": response.headers["X-Subject-Token"]}


def descriptor(tenant=TENANT_A, metric="memory_usage", *, camel=False):
    resource = tenant["ds"].DCS[0]
    return {"namespace": NAMESPACE, "metricName" if camel else "metric_name": metric,
            "dimensions": [{"name": "instance_id", "value": tenant["ds"].rid("dcs", resource["name"])}]}


def sample_body(tenant=TENANT_A, **overrides):
    return {"samples": [descriptor(tenant)], "period": 300,
            "statistics": ["average", "maximum", "minimum", "sum", "sampleCount"],
            "time_range": f"{NOW_MS - 900000}.{NOW_MS}.15", **overrides}


def expected_catalogue_identities(tenant):
    ds = tenant["ds"]
    identities = {(ds.rid("dcs", row["name"]), metric) for row in ds.DCS if row["status"] == "RUNNING"
                  for metric in ("cpu_usage", "memory_usage", "connected_clients")}
    identities.update((usage["id"], metric) for usage in mock.oa.ecs_usage(ds).values()
                      for metric in ("cpu_util", "mem_util", "disk_util"))
    identities.update((mock.oa.gaussdb_id(ds, row[0]), metric) for row in ds.GAUSSDB
                      for metric in ("cpu_usage", "mem_usage", "disk_usage"))
    return identities


def native_ids(tenant):
    ds = tenant["ds"]
    return ({row["id"] for row in mock.oa.ecs_servers(ds)} | {row["id"] for row in mock.oa.gaussdb_instances(ds)}
            | {row["instance_id"] for row in mock.oa.dcs_instances(ds)})


def path(suffix, tenant=TENANT_A, *, project_id=None):
    return f"/aom/{suffix.format(project_id=project_id or tenant['project_id'])}"


def signed(client, method, url, tenant, body=None, *, project_id=None):
    request = client.build_request(method, url, json=body, headers={"Content-Type": "application/json",
                                   "X-Project-Id": project_id or tenant["project_id"]})
    headers = {key: value for key, value in request.headers.items() if key in ("host", "content-type", "x-project-id")}
    request.headers.update(signer.sign(method, str(request.url), headers, request.content,
                                       tenant["access_key"], tenant["secret_key"]))
    return client.send(request)


def test_native_statistics_are_current_mb_and_cumulative_cpu_for_running_instances_only(mock_client, monkeypatch):
    ds = TENANT_A["ds"]
    rows = [dict(row) for row in ds.DCS]
    rows[1]["status"] = "STOPPED"
    monkeypatch.setattr(ds, "DCS", rows)
    headers = token_headers(mock_client)
    url = f"/v2/{TENANT_A['project_id']}/instances/statistic"
    response = mock_client.get(url, headers=headers)
    assert response.status_code == 200
    actual = {row["instance_id"]: row for row in response.json()["statistics"]}
    assert len(actual) == len(rows) - 1 and ds.rid("dcs", rows[1]["name"]) not in actual
    for row in rows:
        if row["status"] != "RUNNING":
            continue
        item = actual[ds.rid("dcs", row["name"])]
        assert set(item) == {"instance_id", "used_memory", "max_memory", "used_cpu"}
        assert item["used_memory"] == row["used"] and item["max_memory"] == row["cap"] * 1024
        assert isinstance(item["used_cpu"], str)
        assert float(item["used_cpu"]) > 100  # 官方 String 累计秒数，不能误作 CPU 百分比。
    assert mock_client.get(url, headers=headers).json() == response.json()
    assert mock_client.get(f"/v2/{TENANT_A['project_id']}/instances/stats", headers=headers).status_code == 404


def test_catalogue_advertises_explicit_mock_aom_prefix_without_mutating_service_fixtures(mock_client):
    for tenant in (TENANT_A, TENANT_B):
        previous = list(tenant["services"])
        headers = token_headers(mock_client, tenant)
        response = mock_client.get("/v3/auth/catalog", headers=headers)
        entries = [entry for entry in response.json()["catalog"] if entry["name"] == "aom"]
        assert len(entries) == 1 and entries[0]["endpoints"][0]["url"] == "http://mock/aom"
        assert tenant["services"] == previous


def test_v1_catalogue_pages_by_documented_start_and_keeps_camel_case(mock_client):
    headers = token_headers(mock_client)
    url = path("v1/{project_id}/ams/metrics")
    rows, start = [], 0
    while True:
        response = mock_client.post(url, headers=headers, params={"limit": 2, "start": start},
                                    json={"metricItems": [{"namespace": NAMESPACE}]})
        assert response.status_code == 200
        body = response.json()
        assert body["errorCode"] == "SVCSTG_AMS_2000000"
        assert len(body["metrics"]) == body["metaData"]["count"] <= 2
        assert all("metricName" in row and "dimensionvaluehash" in row and "metric_name" not in row
                   for row in body["metrics"])
        rows.extend(body["metrics"])
        following = body["metaData"]["start"]
        if following is None:
            break
        assert isinstance(following, int) and following > start
        start = following
    expected = expected_catalogue_identities(TENANT_A)
    assert len(rows) == body["metaData"]["total"] == len(expected)
    assert {(row["dimensions"][0]["value"], row["metricName"]) for row in rows} == expected
    assert len({json.dumps(row, sort_keys=True) for row in rows}) == len(rows)
    assert {row["unit"] for row in rows} == {"Percent", "Count"}
    assert all(row["namespace"] == NAMESPACE for row in rows)


def test_v1_exact_metric_and_dimensions_filter_returns_only_the_owned_descriptor(mock_client):
    query = descriptor(camel=True)
    response = mock_client.post(path("v1/{project_id}/ams/metrics"), headers=token_headers(mock_client),
                                json={"metricItems": [query, query]})
    assert response.status_code == 200
    rows = response.json()["metrics"]
    assert len(rows) == 1 and all(rows[0][key] == value for key, value in query.items())


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"start": -1}, {"start": "invalid"}])
def test_v1_catalogue_enforces_documented_paging_bounds(mock_client, params):
    response = mock_client.post(path("v1/{project_id}/ams/metrics"), headers=token_headers(mock_client), params=params,
                                json={"metricItems": [{"namespace": NAMESPACE}]})
    assert response.status_code == 400


def test_v1_start_beyond_catalogue_returns_an_empty_completed_page(mock_client):
    response = mock_client.post(path("v1/{project_id}/ams/metrics"), headers=token_headers(mock_client),
                                params={"start": 1000}, json={"metricItems": [{"namespace": NAMESPACE}]})
    body = response.json()
    assert response.status_code == 200 and body["metrics"] == []
    assert body["metaData"] == {"count": 0, "total": len(expected_catalogue_identities(TENANT_A)), "start": None}


def test_v2_series_uses_snake_case_and_does_not_invent_request_paging(mock_client):
    url = path("v2/{project_id}/series")
    headers = token_headers(mock_client)
    response = mock_client.post(url, headers=headers, json={"series": [descriptor()]})
    assert response.status_code == 200
    body = response.json()
    assert body["meta_data"] == {"count": 1, "total": 1, "offset": None}
    assert len(body["series"]) == 1 and body["series"][0]["metric_name"] == "memory_usage"
    assert "dimension_value_hash" in body["series"][0] and "metaData" not in body
    assert mock_client.post(url, headers=headers, params={"offset": 1}, json={"series": [descriptor()]}).status_code == 400


def test_v2_series_keeps_catalogue_dimension_fields_optional(mock_client):
    query = {"namespace": NAMESPACE, "metric_name": "connected_clients", "dimensions": [{"name": "instance_id"}]}
    response = mock_client.post(path("v2/{project_id}/series"), headers=token_headers(mock_client), json={"series": [query]})
    assert response.status_code == 200 and len(response.json()["series"]) == len(TENANT_A["ds"].DCS)


def test_samples_are_deterministic_development_data_with_ms_timestamps_and_correct_units(mock_client):
    headers = token_headers(mock_client)
    url = path("v2/{project_id}/samples")
    body = sample_body(samples=[descriptor(metric=name) for name in ("cpu_usage", "memory_usage", "connected_clients")])
    response = mock_client.post(url, headers=headers, params={"fill_value": "null"}, json=body)
    assert response.status_code == 200
    rows = response.json()["samples"]
    assert len(rows) == 3
    for row in rows:
        metric = row["sample"]["metric_name"]
        assert row["sample"]["namespace"] == NAMESPACE and "dataPoints" not in row
        assert [point["timestamp"] for point in row["data_points"]] == [NOW_MS - 900000, NOW_MS - 600000, NOW_MS - 300000]
        for point in row["data_points"]:
            assert point["unit"] == ("Count" if metric == "connected_clients" else "Percent")
            values = {item["statistic"]: item["value"] for item in point["statistics"]}
            assert values["sampleCount"] == 1 and values["sum"] == values["average"]
            assert values["minimum"] <= values["average"] <= values["maximum"]
    assert mock_client.post(url, headers=headers, params={"fill_value": "null"}, json=body).json() == response.json()


@pytest.mark.parametrize("time_range", ["-1.-1.5", f"-1.{NOW_MS}.5", f"{NOW_MS - 300000}.-1.5"])
def test_documented_relative_time_ranges_use_server_clock(mock_client, time_range):
    response = mock_client.post(path("v2/{project_id}/samples"), headers=token_headers(mock_client),
                                params={"fill_value": "null"}, json=sample_body(period=60, time_range=time_range))
    assert response.status_code == 200
    assert [p["timestamp"] for p in response.json()["samples"][0]["data_points"]] == list(range(NOW_MS - 300000, NOW_MS, 60000))


def test_relative_end_is_clipped_to_server_now_and_valid_1440_point_limit_is_supported(mock_client):
    headers = token_headers(mock_client)
    url = path("v2/{project_id}/samples")
    clipped = mock_client.post(url, headers=headers, params={"fill_value": "null"},
                               json=sample_body(period=60, time_range=f"{NOW_MS - 120000}.-1.5"))
    assert clipped.status_code == 200
    assert [p["timestamp"] for p in clipped.json()["samples"][0]["data_points"]] == [NOW_MS - 120000, NOW_MS - 60000]
    maximum = mock_client.post(url, headers=headers, params={"fill_value": "null"},
                               json=sample_body(period=60, time_range="-1.-1.1440"))
    assert maximum.status_code == 200 and len(maximum.json()["samples"][0]["data_points"]) == 1440


@pytest.mark.parametrize("suffix,body,key", [
    ("v1/{project_id}/ams/metrics", {"metricItems": [descriptor(TENANT_A, camel=True)]}, "metrics"),
    ("v2/{project_id}/series", {"series": [descriptor(TENANT_A)]}, "series"),
    ("v2/{project_id}/samples", sample_body(), "samples"),
])
def test_payload_cannot_select_another_tenants_series(mock_client, suffix, body, key):
    response = mock_client.post(path(suffix, TENANT_B), headers=token_headers(mock_client, TENANT_B), json=body)
    assert response.status_code == 200 and response.json()[key] == []


@pytest.mark.parametrize("suffix,body,key", [
    ("v1/{project_id}/ams/metrics", {"metricItems": [{**descriptor(camel=True), "namespace": "SYS.DCS"}]}, "metrics"),
    ("v2/{project_id}/series", {"series": [{**descriptor(), "namespace": "SYS.DCS"}]}, "series"),
    ("v2/{project_id}/samples", sample_body(samples=[{**descriptor(), "namespace": "SYS.DCS"}]), "samples"),
])
def test_sys_dcs_is_not_advertised_as_an_aom_builtin_mapping(mock_client, suffix, body, key):
    response = mock_client.post(path(suffix), headers=token_headers(mock_client), json=body)
    assert response.status_code == 200 and response.json()[key] == []


@pytest.mark.parametrize("suffix,body", [
    ("v1/{project_id}/ams/metrics", {"metricItems": [{"namespace": NAMESPACE}]}),
    ("v2/{project_id}/series", {"series": [{"namespace": NAMESPACE}]}),
    ("v2/{project_id}/samples", sample_body()),
])
def test_aom_requires_auth_and_exact_project_scope_including_unknown_projects(mock_client, suffix, body):
    headers = token_headers(mock_client)
    assert mock_client.post(path(suffix), json=body).status_code == 401
    assert mock_client.post(path(suffix, TENANT_B), headers=headers, json=body).status_code == 403
    assert mock_client.post(path(suffix, project_id="unrelated-project"), headers=headers, json=body).status_code == 403
    signed_url = path(suffix, project_id="unrelated-project")
    assert signed(mock_client, "POST", signed_url, TENANT_B, body, project_id="unrelated-project").status_code == 403


def test_native_statistics_scope_and_legacy_single_project_compatibility(mock_client):
    headers = token_headers(mock_client)
    assert mock_client.get(f"/v2/{TENANT_B['project_id']}/instances/statistic", headers=headers).status_code == 403
    assert mock_client.get("/v2/unknown-project/instances/statistic", headers=headers).status_code == 403
    legacy = token_headers(mock_client, project_id="p1")
    assert mock_client.get("/v2/p1/instances/statistic", headers=legacy).status_code == 200
    response = signed(mock_client, "GET", f"/v2/{TENANT_B['project_id']}/instances/statistic", TENANT_B)
    assert response.status_code == 200
    assert {row["instance_id"] for row in response.json()["statistics"]} == {
        TENANT_B["ds"].rid("dcs", row["name"]) for row in TENANT_B["ds"].DCS}


@pytest.mark.parametrize("suffix,body,key", [
    ("v1/{project_id}/ams/metrics", {"metricItems": [{"namespace": NAMESPACE}]}, "metrics"),
    ("v2/{project_id}/series", {"series": [{"namespace": NAMESPACE}]}, "series"),
    ("v2/{project_id}/samples", sample_body(TENANT_B), "samples"),
])
def test_aom_aksk_signature_is_verified_and_only_returns_that_projects_data(mock_client, suffix, body, key):
    response = signed(mock_client, "POST", path(suffix, TENANT_B), TENANT_B, body)
    assert response.status_code == 200 and response.json()[key]
    owned = native_ids(TENANT_B)
    for row in response.json()[key]:
        item = row.get("sample", row)
        assert {d["value"] for d in item["dimensions"] if d["name"] == "instance_id"} <= owned


@pytest.mark.parametrize("invalid", [
    {"samples": None}, {"samples": [descriptor()] * 21},
    {"samples": [descriptor(camel=True)]}, {"samples": [{**descriptor(), "dimensions": []}]},
    {"samples": [{**descriptor(), "dimensions": [{"name": "instance_id", "value": ""}]}]},
    {"period": 600}, {"period": True}, {"statistics": ["max"]}, {"statistics": []},
    {"time_range": "bad"}, {"time_range": "-1.-1.0"}, {"time_range": f"{NOW_MS}.{NOW_MS - 60000}.1"},
    {"time_range": f"{NOW_MS - 3600000}.{NOW_MS}.1"}, {"period": 60, "time_range": "-1.-1.1441"},
])
def test_samples_reject_invalid_shapes_and_unbounded_windows(mock_client, invalid):
    response = mock_client.post(path("v2/{project_id}/samples"), headers=token_headers(mock_client),
                                params={"fill_value": "null"}, json=sample_body(**invalid))
    assert response.status_code == 400


@pytest.mark.parametrize("query", [{"fillValue": "null"}, {"fill_value": "unsupported"}])
def test_v2_samples_rejects_v1_query_spelling_and_unknown_fill_value(mock_client, query):
    assert mock_client.post(path("v2/{project_id}/samples"), headers=token_headers(mock_client),
                            params=query, json=sample_body()).status_code == 400


@pytest.mark.parametrize("suffix,body", [
    ("v1/{project_id}/ams/metrics", {"metricItems": [{}]}),
    ("v1/{project_id}/ams/metrics", {"metricItems": [descriptor()]}),
    ("v2/{project_id}/series", {"series": [{"namespace": NAMESPACE, "metricName": "memory_usage"}]}),
])
def test_catalogue_requires_namespace_and_correct_version_specific_names(mock_client, suffix, body):
    assert mock_client.post(path(suffix), headers=token_headers(mock_client), json=body).status_code == 400


@pytest.mark.parametrize("content", [b"{", b"null", b"[]", b'{"series":null}'])
def test_invalid_json_and_body_shapes_return_hcs_errors_instead_of_500(mock_client, content):
    response = mock_client.post(path("v2/{project_id}/series"), headers={**token_headers(mock_client),
                                "Content-Type": "application/json"}, content=content)
    assert response.status_code == 400 and "error_code" in response.json()


def test_only_explicit_mock_aom_routes_are_published(mock_client):
    headers = token_headers(mock_client)
    assert mock_client.post(f"/v1/{TENANT_A['project_id']}/ams/metrics", headers=headers,
                            json={"metricItems": [{"namespace": NAMESPACE}]}).status_code == 404
    assert mock_client.post(path("v1/{project_id}/ams/metricdata"), headers=headers, json={}).status_code == 404


def test_production_aom_client_cannot_consume_even_valid_mock_protocol(mock_client, monkeypatch):
    settings = Settings(_env_file=None, hcs_iam_url="http://mock", hcs_domain=TENANT_A["domain_name"],
                        hcs_username="mock-contract-only", hcs_password="synthetic-mock-only",
                        hcs_project_id=TENANT_A["project_id"],
                        hcs_endpoints={"aom": "http://mock/aom", "dcs": "http://mock"})
    client = build_client(settings, http=mock_client)
    calls = []
    original = mock_client.send
    def track(request, *args, **kwargs):
        calls.append(request.url.path)
        return original(request, *args, **kwargs)
    monkeypatch.setattr(mock_client, "send", track)
    for operation in (lambda: aom.aom_probe(client),
                      lambda: aom.aom_list_metrics(client, NAMESPACE, "memory_usage"),
                      lambda: aom.aom_query(client, [descriptor()], NOW-timedelta(minutes=15), NOW),
                      lambda: client.post("aom", "/v2/{project_id}/samples", json=sample_body())):
        with pytest.raises(HcsError) as error:
            operation()
        assert error.value.code == "LOCAL.AOM_DISABLED"
    assert calls == []
    # Native DCS statistics remain usable independently of the disabled AOM fixtures.
    context = RunContext(client=client, account="mock-contract-only", project_id=TENANT_A["project_id"])
    observations = DcsCollector().collect(context)
    assert context.errors == [] and any(row.check_item == "memory" for row in observations)
    assert not any(row.check_item == "cpu" for row in observations)
    assert all(not route.startswith("/aom/") for route in calls)


@pytest.mark.parametrize("tenant,service", [(TENANT_A, "ecs"), (TENANT_B, "ecs"), (TENANT_A, "gaussdb")],
                         ids=["ecs-a", "ecs-b", "gaussdb-a"])
def test_custom_usage_series_keep_native_instance_ids_fixture_values_and_percent_units(mock_client, monkeypatch,
                                                                                      tenant, service):
    monkeypatch.setattr(mock.oa, "jitter", lambda value, amp=0.6: value)
    ds = tenant["ds"]
    headers = token_headers(mock_client, tenant)
    if service == "ecs":
        usage = next(iter(mock.oa.ecs_usage(ds).values()))
        rid = usage["id"]
        expected = {"cpu_util": usage["cpu"], "mem_util": usage["mem"], "disk_util": max(usage["disks"].values())}
        response = mock_client.get(f"/ecs/v1/{tenant['project_id']}/cloudservers/detail", headers=headers)
        assert rid in {server["id"] for server in response.json()["servers"]}
    else:
        response = mock_client.get(f"/gaussdb/v3.3/{tenant['project_id']}/instances", headers=headers)
        rid = response.json()["instances"][0]["id"]
        cpu, memory, disk = ds.GAUSSDB[0][-2]
        expected = {"cpu_usage": cpu, "mem_usage": memory, "disk_usage": disk}
        assert rid == mock.oa.gaussdb_id(ds, ds.GAUSSDB[0][0]) and rid.endswith("in14")
    queries = [{"namespace": NAMESPACE, "metric_name": metric, "dimensions": [{"name": "instance_id", "value": rid}]}
               for metric in expected]
    catalogue = mock_client.post(path("v2/{project_id}/series", tenant), headers=headers, json={"series": queries})
    assert catalogue.status_code == 200 and len(catalogue.json()["series"]) == 3
    samples = mock_client.post(path("v2/{project_id}/samples", tenant), headers=headers, params={"fill_value": "null"},
                               json=sample_body(samples=queries))
    assert samples.status_code == 200 and len(samples.json()["samples"]) == 3
    for row in samples.json()["samples"]:
        assert row["sample"]["dimensions"] == [{"name": "instance_id", "value": rid}]
        for point in row["data_points"]:
            assert point["unit"] == "Percent"
            assert next(item["value"] for item in point["statistics"] if item["statistic"] == "average") == expected[
                row["sample"]["metric_name"]]


def test_stopped_ecs_and_absent_gaussdb_do_not_get_invented_series(mock_client, monkeypatch):
    ds = TENANT_A["ds"]
    server = list(ds.ECS[0])
    server[6] = "SHUTOFF"
    monkeypatch.setattr(ds, "ECS", [tuple(server)])
    rid = ds.rid("ecs", server[0])
    queries = [{"namespace": NAMESPACE, "metric_name": metric, "dimensions": [{"name": "instance_id", "value": rid}]}
               for metric in ("cpu_util", "mem_util", "disk_util")]
    response = mock_client.post(path("v2/{project_id}/samples"), headers=token_headers(mock_client),
                                params={"fill_value": "null"}, json=sample_body(samples=queries))
    assert response.status_code == 200 and response.json()["samples"] == []
    response = mock_client.post(path("v2/{project_id}/series", TENANT_B), headers=token_headers(mock_client, TENANT_B),
                                json={"series": [{"namespace": NAMESPACE, "metric_name": "mem_usage"}]})
    assert response.status_code == 200 and response.json()["series"] == []


def test_custom_aom_catalogue_does_not_call_legacy_ces_apis_or_metric_helpers(mock_client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("custom AOM must not depend on a legacy CES API/helper")

    monkeypatch.setattr(mock, "_require_ces", forbidden)
    monkeypatch.setattr(mock.oa, "ces_values", forbidden)
    monkeypatch.setattr(mock.oa, "ces_disk_metrics", forbidden)
    response = mock_client.post(path("v2/{project_id}/series"), headers=token_headers(mock_client),
                                json={"series": [{"namespace": NAMESPACE}]})
    assert response.status_code == 200
    assert {(row["dimensions"][0]["value"], row["metric_name"]) for row in response.json()["series"]} == (
        expected_catalogue_identities(TENANT_A))


@pytest.mark.parametrize("service,collector,fields", [
    ("ecs", EcsStatusCollector, ("cpu_util", "mem_util", "disk_util")),
    ("gaussdb", GaussDBCollector, ("cpu_usage", "mem_usage", "disk_usage")),
])
def test_ecs_aom_stays_disabled_and_gaussdb_keeps_independent_native_source(
        mock_client, monkeypatch, service, collector, fields, world, mem_db):
    ds = TENANT_A["ds"]
    if service == "ecs":
        name = next(iter(mock.oa.ecs_usage(ds)))
        monkeypatch.setattr(ds, "ECS", [next(row for row in ds.ECS if row[0] == name)])
    else:
        monkeypatch.setattr(ds, "GAUSSDB", ds.GAUSSDB[:1])
    if service == "ecs":
        from app.collectors import ecs, ecs_metrics
        from app.metrics import ecs_readings
        from app.services import ecs_monitoring, pipeline

        monkeypatch.setattr(ecs_readings, "_now", lambda: NOW)
        for module in (ecs, ecs_metrics, ecs_monitoring, pipeline):
            monkeypatch.setattr(module, "utcnow", lambda: NOW)
        account = world["tenant-a"]
        account.metrics_source, account.endpoints = "AOM", {"aom": "http://mock/aom"}
        task = mem_db.get(InspectionTask, "ecs_metrics")
        task.params = {"aom": {"namespace": NAMESPACE, "dimension": "instance_id",
                               "metrics": {field: field for field in fields}}}
        mem_db.commit()
        target = next(target for target in resolve_targets(mem_db) if target.tenant_id == account.id)
        client = target.client(http=mock_client, mock_url="http://mock")
        calls, stamps = [], set()
        original = mock_client.send
        def track(request, *args, **kwargs):
            calls.append(request.url.path)
            response = original(request, *args, **kwargs)
            if request.url.path.endswith("/samples"):
                response.read()
                stamps.update(datetime.fromtimestamp(point["timestamp"]/1000, UTC)
                    for sample in response.json()["samples"] for point in sample["data_points"])
            return response
        monkeypatch.setattr(mock_client, "send", track)
        inventory = sync_ledger(mem_db, client=client, target=target, types=["ecs_server"], trigger="MANUAL")
        assert inventory.status == "SUCCESS", inventory.error_summary
        sampled = run_task(mem_db, "ecs_metrics", client=client, target=target, trigger="MANUAL")
        assert sampled.status == "PARTIAL" and "AOM 已停用" in sampled.error_summary
        assert sampled.stats["metrics_source"] == "NONE"
        assert sampled.stats["metrics_diagnostic"]["covered_resources"] == 0
        before_status = len(calls)
        status = run_task(mem_db, "ecs_status", client=client, target=target, trigger="MANUAL")
        assert status.status == "SUCCESS", status.error_summary
        assert all("/ams/" not in path and not path.endswith("/samples") and not path.startswith("/V1.0/")
                   for path in calls[before_status:])
        resource = mem_db.scalar(select(Resource).where(Resource.tenant_id == account.id,
            Resource.project_ref == target.project_ref, Resource.resource_type == "ecs_server"))
        assert all(resource.metrics[field] is None for field in fields)
        history = list(mem_db.scalars(select(EcsMetricSample)))
        assert history == [] and stamps == set()
        assert all(not path.startswith("/aom/") for path in calls)
        assert task.params["aom"]["namespace"] == NAMESPACE
        assert all(not path.startswith("/V1.0/") for path in calls)
        return
    settings = Settings(_env_file=None, hcs_iam_url="http://mock", hcs_domain=TENANT_A["domain_name"],
                        hcs_username="mock-contract-only", hcs_password="synthetic-mock-only",
                        hcs_project_id=TENANT_A["project_id"], hcs_endpoints={"aom": "http://mock/aom",
                                                                                      service: "http://mock/ecs"
                                                                                      if service == "ecs" else "http://mock"})
    client = build_client(settings, http=mock_client)
    account = SimpleNamespace(metrics_source="AOM", endpoints={"aom": "http://mock/aom"}, capabilities={})
    context = RunContext(client=client, account="mock-contract-only", project_id=TENANT_A["project_id"], tenant=account,
                         params={"aom": {"namespace": NAMESPACE, "dimension": "instance_id",
                                         "metrics": {field: field for field in fields}}})
    observations = collector().collect(context)
    assert context.errors == [] and context.stats["metrics_source"] == "GAUSSDB"
    assert "aom_matched_resources" not in context.stats
    assert {row.check_item for row in observations} == {"instance_status", "disk", "components"}
    assert observations[0].metrics["cpu_usage"] is observations[0].metrics["mem_usage"] is None
    assert observations[1].metrics["disk_used_gb"] is not None
    assert observations[1].metrics["disk_total_gb"] > 0


def test_explicit_legacy_ces_mock_still_works_and_remains_tenant_scoped(mock_client, monkeypatch):
    monkeypatch.setitem(TENANT_A, "services", list(dict.fromkeys([*TENANT_A["services"], "ces"])))
    headers = token_headers(mock_client)
    url = f"/V1.0/{TENANT_A['project_id']}/metrics"
    assert mock_client.get(url, headers=headers, params={"namespace": "SYS.ECS"}).status_code == 200
    assert mock_client.get(f"/V1.0/{TENANT_B['project_id']}/metrics", headers=headers).status_code == 403
