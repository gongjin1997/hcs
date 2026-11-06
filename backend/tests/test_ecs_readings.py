"""Timestamped ECS source contracts; synthetic httpx transports only."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.collectors.base import RunContext
from app.core.config import get_settings
from app.hcs import HcsClient
from app.hcs import ratelimit as limits
from app.hcs.endpoints import EndpointResolver
from app.hcs.ratelimit import request_budget
from app.metrics import ecs_readings as adapter
from app.metrics.prometheus import DEFAULT_QUERIES

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
AT = NOW - timedelta(seconds=60)
MAPPING = {"namespace": "CUSTOM.Site", "dimension": "server_ref", "metrics": {
    "cpu_util": "site_cpu", "mem_util": "site_memory", "disk_util": "site_disk", "boot_time": "site_boot"}}


def vector(value="42.5", at=AT, instance="10.0.0.1:9100", **labels):
    return {"metric": {"instance": instance, **labels}, "value": [at.timestamp(), value]}


class Cloud:
    def __init__(self):
        self.requests = []
        self.prom = {}
        self.fail = set()
        self.ces_catalogue = []
        self.ces_rows = None

    def respond(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "prom.invalid":
            assert request.method == "GET" and path == "/api/v1/query"
            query = request.url.params["query"]
            alias = next((key for key, value in DEFAULT_QUERIES.items() if value == query), query)
            if alias in self.fail:
                return httpx.Response(403, json={"message": "sensitive-upstream-body"})
            return httpx.Response(200, json={"status": "success", "data": {
                "resultType": "vector", "result": self.prom.get(alias, [])}})
        if path.endswith("/batch-query-metric-data"):
            assert request.method == "POST"
            body = json.loads(request.content)
            if any(row["metric_name"] in self.fail for row in body["metrics"]):
                return httpx.Response(403, json={"error_code": "CES.FORBIDDEN", "message": "sensitive-upstream-body"})
            rows = self.ces_rows if self.ces_rows is not None else [
                {**row, "datapoints": [{"timestamp": int(AT.timestamp() * 1000), "average": 42.5}]}
                for row in body["metrics"]]
            return httpx.Response(200, json={"metrics": rows})
        if path.endswith("/metrics") and request.url.host == "ces.invalid":
            assert request.method == "GET"
            return httpx.Response(200, json={"metrics": self.ces_catalogue, "meta_data": {}})
        pytest.fail(f"Unexpected cloud route: {request.method} {path}")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "hcs_ces_enabled", False)
    monkeypatch.setattr(settings, "hcs_mock_url", None)
    monkeypatch.setattr(settings, "hcs_endpoints", {})
    monkeypatch.setattr(settings, "hcs_total_qps", 100000)
    monkeypatch.setattr(settings, "hcs_acquire_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "env", "test")
    monkeypatch.setattr(limits, "_shared", {})
    monkeypatch.setattr(adapter, "_now", lambda: NOW)


@pytest.fixture
def cloud():
    return Cloud()


@pytest.fixture
def context(cloud):
    clients = []

    def make(source="PROMETHEUS", params=None, tenant_id=1, project_ref=11):
        http = httpx.Client(transport=httpx.MockTransport(cloud.respond))
        clients.append(http)
        tenant = SimpleNamespace(id=tenant_id, metrics_source=source, capabilities={},
                                 endpoints={"aom": "https://aom.invalid", "ces": "https://ces.invalid"},
                                 prometheus_url="https://prom.invalid")
        auth = SimpleNamespace(kind="TEST", apply=lambda *_: None, refresh=lambda: False,
                               access_key="test-ak", _secret_key="test-sk")
        client = HcsClient(http, auth, EndpointResolver(tenant.endpoints, "test", "invalid"),
                           f"project-{project_ref}", tenant_id=tenant_id, project_ref=project_ref,
                           max_retries=0, qps={"aom": 100000, "ces": 100000})
        return RunContext(client, "test", client.project_id, tenant=tenant, tenant_id=tenant_id,
                          project_ref=project_ref, params=params if params is not None else {"aom": MAPPING})

    yield make
    for client in clients:
        client.close()


def collect(ctx, metrics=None, ids=None, index=None, **config):
    return adapter.collect_readings(ctx, ids or ["one"], index or {"10.0.0.1": "one"},
                                    metrics=metrics or ["cpu_util"], config=config or None)


@pytest.mark.parametrize("mapping", [None, {}])
def test_auto_aom_endpoint_alone_is_not_evidence_of_ecs_metrics(cloud, context, mapping):
    ctx = context("AUTO", params={"aom": mapping})
    cloud.prom["cpu_util"] = [vector()]
    result = collect(ctx)
    assert result.source == "PROMETHEUS"
    assert result.readings["one"][0].source == "PROMETHEUS"
    assert cloud.requests and all(request.url.host == "prom.invalid" for request in cloud.requests)


def test_auto_without_confirmed_ecs_source_does_not_query_aom_or_guess_ces(cloud, context):
    ctx = context("AUTO", params={})
    ctx.tenant.prometheus_url = None
    result = collect(ctx)
    assert result.source == "NONE" and result.readings == {} and cloud.requests == []
    assert result.diagnostic["metrics"]["cpu_util"]["reason"] == "source_not_configured"


def test_scope_mismatch_fails_before_requests(cloud, context):
    ctx = context("PROMETHEUS")
    ctx.client.project_ref = 999
    result = collect(ctx)
    assert result.diagnostic["metrics"]["cpu_util"]["reason"] == "scope_mismatch"
    assert cloud.requests == [] and ctx.errors


def test_prometheus_preserves_timestamp_zero_stale_and_ipv4_ipv6_association(cloud, context):
    old = NOW - timedelta(hours=1)
    cloud.prom["cpu_util"] = [vector("0", at=old), vector("30", instance="[2001:db8::1]:9100"),
                              vector("99", instance="10.0.0.9:9100")]
    result = collect(context("PROMETHEUS"), ids=["one", "two"],
                     index={"10.0.0.1": "one", "2001:0db8::1": "two", "10.0.0.9": "foreign"}, stale_after_sec=120)
    assert result.readings["one"][0].observed_at == old and result.readings["one"][0].value == 0
    assert result.readings["two"][0].observed_at == AT and result.readings["two"][0].value == 30
    assert result.readings["two"][0].period_sec == 0
    assert result.diagnostic["metrics"]["cpu_util"]["stale_points"] == 1
    assert result.diagnostic["coverage"] == 1 and len(cloud.requests) == 1


@pytest.mark.parametrize("raw_timestamp", [True, False, None, "NaN", "inf", -1, 0, NOW.timestamp()+1, 1e100])
def test_prometheus_rejects_invalid_and_future_timestamp(cloud, context, raw_timestamp):
    cloud.prom["cpu_util"] = [{"metric": {"instance": "10.0.0.1:9100"}, "value": [raw_timestamp, "15"]}]
    assert collect(context("PROMETHEUS")).readings == {}


@pytest.mark.parametrize("value", [True, False, "NaN", "inf", -1, 101, None])
def test_prometheus_rejects_invalid_percent(cloud, context, value):
    cloud.prom["cpu_util"] = [vector(value)]
    assert collect(context("PROMETHEUS")).readings == {}


def test_prometheus_isolates_http_query_errors_without_logging_promql_or_values(cloud, context, caplog):
    cloud.fail.add("private-promql-marker")
    cloud.prom["mem_util"] = [vector("25")]
    ctx = context("PROMETHEUS")
    result = collect(ctx, metrics=["cpu_util", "mem_util"], prometheus={
        "queries": {"cpu_util": "private-promql-marker"}, "units": {"cpu_util": "%"}})
    assert [row.metric for row in result.readings["one"]] == ["mem_util"]
    assert result.diagnostic["status"] == "partial" and len(cloud.requests) == 2
    output = str(result.diagnostic) + str(ctx.errors) + caplog.text
    assert "private-promql-marker" not in output and "sensitive-upstream-body" not in output


def test_prometheus_duplicate_ip_aliases_are_unambiguous_only(cloud, context):
    cloud.prom["cpu_util"] = [vector(instance="[2001:db8::1]:9100")]
    result = collect(context("PROMETHEUS"), ids=["one", "two"],
                     index={"2001:db8::1": "one", "2001:0db8::1": "two"})
    assert result.readings == {} and result.diagnostic["status"] == "no_series"


@pytest.mark.parametrize("reverse", [False, True])
def test_prometheus_disk_max_is_per_timestamp_and_ties_choose_mount_deterministically(cloud, context, reverse):
    rows = [vector("99", AT-timedelta(seconds=60), mountpoint="/old"), vector("80", mountpoint="/z"),
            vector("80", mountpoint="/a"), vector("10", mountpoint="/low")]
    cloud.prom["disk_util"] = list(reversed(rows)) if reverse else rows
    result = collect(context("PROMETHEUS"), metrics=["disk_util"])
    assert [(row.value, row.dimensions["mountpoint"]) for row in result.readings["one"]] == [(99, "/old"), (80, "/a")]


@pytest.mark.parametrize("alias", ["cpu_util", "mem_util"])
def test_prometheus_conflicting_series_never_randomly_overwrite(cloud, context, alias):
    cloud.prom[alias] = [vector("10", job="first"), vector("90", job="second")]
    result = collect(context("PROMETHEUS"), metrics=[alias])
    assert result.readings == {} and result.diagnostic["metrics"][alias]["rejected_points"] == 1


def test_duplicate_identical_samples_are_idempotent(cloud, context):
    cloud.prom["cpu_util"] = [vector("10"), vector("10")]
    result = collect(context("PROMETHEUS"))
    assert len(result.readings["one"]) == 1 and result.diagnostic["status"] == "ready"


def test_boot_time_is_seconds_with_actual_observation_time(cloud, context):
    boot = NOW - timedelta(days=5)
    cloud.prom["boot_time"] = [vector(str(boot.timestamp()))]
    result = collect(context("PROMETHEUS"), metrics=["boot_time"])
    reading = result.readings["one"][0]
    assert reading.value == boot.timestamp() and reading.observed_at == AT and reading.unit == "s"
    cloud.prom["boot_time"] = [vector(str(NOW.timestamp()))]
    assert collect(context("PROMETHEUS"), metrics=["boot_time"]).readings == {}


@pytest.mark.parametrize("source,gate", [("CES", False), ("AUTO", True), ("NONE", True)])
def test_ces_requires_both_legacy_gate_and_explicit_tenant_mode(cloud, context, monkeypatch, source, gate):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", gate)
    ctx = context(source, params={})
    ctx.tenant.endpoints = {}
    ctx.tenant.prometheus_url = None
    result = collect(ctx)
    assert result.source == "NONE" and result.diagnostic["status"] == "no_source"
    assert cloud.requests == []


def test_ces_preserves_real_timestamps_and_isolates_cpu_error(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    cloud.fail.add("cpu_util")
    ctx = context("CES")
    result = collect(ctx, metrics=["cpu_util", "mem_util"])
    reading = result.readings["one"][0]
    assert reading.metric == "mem_util" and reading.observed_at == AT and reading.period_sec == 1
    assert result.diagnostic["status"] == "partial" and len(cloud.requests) == 2
    assert ctx.errors and "sensitive-upstream-body" not in str(ctx.errors)


def test_ces_response_descriptor_and_actual_datapoints_are_validated(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    valid = {"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [{"name": "instance_id", "value": "one"}]}
    samples = [{"timestamp": int(AT.timestamp()*1000), "average": 0},
               {"timestamp": int((AT-timedelta(seconds=60)).timestamp()*1000), "average": 50},
               {"timestamp": True, "average": 99},
               {"timestamp": int((NOW+timedelta(seconds=1)).timestamp()*1000), "average": 99},
               {"timestamp": int(AT.timestamp()*1000), "average": True}]
    cloud.ces_rows = [{**valid, "datapoints": samples},
                      {**valid, "namespace": "FOREIGN.ECS", "datapoints": samples},
                      {**valid, "dimensions": [{"name": "instance_id", "value": "foreign"}], "datapoints": samples},
                      {**valid, "dimensions": valid["dimensions"]*2, "datapoints": samples}]
    result = collect(context("CES"))
    assert [(row.observed_at, row.value) for row in result.readings["one"]] == [(AT-timedelta(seconds=60), 50), (AT, 0)]
    assert result.diagnostic["status"] == "partial"


def test_ces_disk_uses_only_discovered_owned_descriptors(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    def disk(sid, mount):
        return {"namespace": "AGT.ECS", "metric_name": "disk_usedPercent", "dimensions": [
            {"name": "instance_id", "value": sid}, {"name": "mount_point", "value": mount}]}
    cloud.ces_catalogue = [disk("one", "/z"), disk("one", "/a"), disk("foreign", "/foreign")]
    result = collect(context("CES"), metrics=["disk_util"])
    assert result.readings["one"][0].dimensions == {"instance_id": "one", "mount_point": "/a"}
    assert len(cloud.requests) == 2
    assert len(json.loads(cloud.requests[-1].content)["metrics"]) == 2


def test_parent_budget_is_honoured_and_never_causes_extra_calls(cloud, context):
    ctx = context("PROMETHEUS")
    cloud.prom["cpu_util"] = [vector()]
    cloud.prom["mem_util"] = [vector("25")]
    with request_budget(attempts=1, seconds=5):
        result = collect(ctx, metrics=["cpu_util", "mem_util"])
    assert len(cloud.requests) == 1 and result.diagnostic["status"] == "partial"
    assert [(row.metric, row.value) for row in result.readings["one"]] == [("cpu_util", 42.5)]
    assert ctx.errors


def test_config_only_overrides_present_keys_and_ignores_other_policy_fields(cloud, context):
    cloud.prom["cpu_util"] = [vector()]
    result = collect(context(), metrics=["cpu_util"], enabled=True, interval_sec=120, stale_after_sec=180)
    assert result.readings["one"][0].value == 42.5 and result.diagnostic["stale_after_sec"] == 180
    assert collect(context(), aom=MAPPING).readings["one"][0].source == "PROMETHEUS"


def test_empty_resource_list_makes_no_requests(cloud, context):
    result = adapter.collect_readings(context(), [], {}, metrics=["cpu_util"])
    assert result.readings == {} and result.diagnostic["status"] == "no_data" and cloud.requests == []


@pytest.mark.parametrize("source,reason", [("AOM", "aom_disabled"), ("PROMETHEUS", "prometheus_not_configured"),
                                        ("CES", "legacy_ces_disabled"), ("NONE", "metrics_source_disabled")])
def test_source_configuration_missing_is_granular_and_makes_no_calls(cloud, context, source, reason):
    ctx = context(source)
    ctx.tenant.endpoints = {}
    ctx.tenant.prometheus_url = None
    result = collect(ctx)
    assert result.diagnostic["metrics"]["cpu_util"]["reason"] == reason
    assert result.diagnostic["status"] == "no_source" and cloud.requests == []


@pytest.mark.parametrize("config", [{"cpu_util": ""}, {"queries": {"cpu_util": None}}])
def test_prometheus_missing_query_does_not_issue_a_default_query(cloud, context, config):
    result = collect(context("PROMETHEUS"), prometheus=config)
    assert result.diagnostic["metrics"]["cpu_util"]["status"] == "mapping_missing" and cloud.requests == []


@pytest.mark.parametrize("config", ["bad-config", {"queries": "bad-config"}, {"period_sec": True}])
def test_prometheus_invalid_config_fails_closed_without_exposing_config(cloud, context, config):
    ctx = context("PROMETHEUS")
    result = collect(ctx, prometheus=config)
    assert result.diagnostic["status"] == "error" and cloud.requests == []
    assert "bad-config" not in str(ctx.errors) + str(result.diagnostic)


def test_prometheus_actual_http_transport_failure_is_per_key(cloud, context):
    ctx = context("PROMETHEUS")
    cloud.prom["mem_util"] = [vector("12.25")]
    original = cloud.respond
    def respond(request):
        if request.url.params["query"] == DEFAULT_QUERIES["cpu_util"]:
            raise httpx.ReadTimeout("sensitive-network-exception", request=request)
        return original(request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        ctx.client.http = http
        result = collect(ctx, metrics=["cpu_util", "mem_util"])
    assert [row.metric for row in result.readings["one"]] == ["mem_util"]
    assert result.diagnostic["status"] == "partial" and "sensitive-network-exception" not in str(ctx.errors)


def test_prometheus_canonical_ip_collision_with_unrequested_resource_is_not_attached(cloud, context):
    cloud.prom["cpu_util"] = [vector(instance="[2001:db8::1]:9100")]
    result = collect(context("PROMETHEUS"), index={"2001:db8::1": "one", "2001:0db8::1": "foreign"})
    assert result.readings == {} and result.diagnostic["status"] == "no_series" and cloud.requests == []


def test_prometheus_conflicting_duplicate_mount_does_not_hide_valid_other_mount(cloud, context):
    cloud.prom["disk_util"] = [vector("90", mountpoint="/a"), vector("10", mountpoint="/a"),
                               vector("20", mountpoint="/b")]
    result = collect(context("PROMETHEUS"), metrics=["disk_util"])
    assert result.readings["one"][0].dimensions["mountpoint"] == "/b"
    assert result.diagnostic["status"] == "partial"


def test_ces_stale_points_and_timestamp_units_preserved(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    cloud.ces_rows = [{"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [
        {"name": "instance_id", "value": "one"}], "datapoints": [
            {"timestamp": int((NOW-timedelta(seconds=500)).timestamp()*1000), "average": 15},
            {"timestamp": int(AT.timestamp()), "average": 99},
            {"timestamp": int(AT.timestamp()*1000), "average": 19, "unit": "Count"}]}]
    result = collect(context("CES"), stale_after_sec=120)
    assert [row.value for row in result.readings["one"]] == [15]
    assert result.diagnostic["metrics"]["cpu_util"]["stale_points"] == 1
    assert result.diagnostic["status"] == "partial"


def test_unsupported_legacy_boot_metric_never_guesses_an_endpoint(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    result = collect(context("CES"), metrics=["boot_time"])
    assert result.diagnostic["metrics"]["boot_time"]["status"] == "mapping_missing" and cloud.requests == []


def test_prometheus_uses_instance_label_from_tenant_policy(cloud, context):
    cloud.prom["cpu_util"] = [{"metric": {"node_address": "[2001:db8::1]:9100"}, "value": [AT.timestamp(), "30"]}]
    result = collect(context("PROMETHEUS"), index={"2001:db8::1": "one"},
                     prometheus={"queries": {}, "instance_label": "node_address"})
    assert result.readings["one"][0].observed_at == AT
    assert result.readings["one"][0].value == 30


@pytest.mark.parametrize("source,expected,gate", [("NONE", "NONE", True), ("PROMETHEUS", "PROMETHEUS", True),
                                                 ("CES", "NONE", False), ("CES", "CES", True)])
def test_ecs_source_preserves_explicit_modes_even_when_aom_endpoint_is_configured(
        cloud, context, monkeypatch, source, expected, gate):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", gate)
    ctx = context(source)
    assert adapter.ecs_source(ctx.tenant, {"aom": MAPPING}, ctx.client) == expected and cloud.requests == []


def test_ecs_source_never_invents_aom_endpoint_from_mapping_or_enables_auto_ces(cloud, context, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    ctx = context("AUTO")
    ctx.tenant.endpoints, ctx.tenant.prometheus_url = {}, None
    assert adapter.ecs_source(ctx.tenant, {"aom": MAPPING}, ctx.client) == "NONE" and cloud.requests == []


@pytest.mark.parametrize("mapping", [None, {}, MAPPING, {"namespace": "broken"}, "bad-config"])
@pytest.mark.parametrize("has_prometheus", [False, True])
def test_explicit_aom_is_disabled_without_fallback_or_requests(cloud, context, mapping, has_prometheus):
    ctx = context("AOM", params={"aom": mapping})
    ctx.tenant.prometheus_url = "https://prom.invalid" if has_prometheus else None
    cloud.prom["cpu_util"] = [vector("99")]
    result = collect(ctx, metrics=["cpu_util", "mem_util", "disk_util", "boot_time"], aom=mapping)
    assert result.source == "NONE" and result.readings == {} and cloud.requests == []
    assert result.diagnostic["status"] == "no_source"
    assert {entry["reason"] for entry in result.diagnostic["metrics"].values()} == {"aom_disabled"}
    assert ctx.tenant.capabilities == {}


@pytest.mark.parametrize("mapping", [None, {}, MAPPING, {"namespace": "broken"}, "bad-config"])
@pytest.mark.parametrize("has_prometheus", [False, True])
def test_auto_ignores_all_legacy_aom_mappings(cloud, context, mapping, has_prometheus):
    ctx = context("AUTO", params={"aom": mapping})
    ctx.tenant.prometheus_url = "https://prom.invalid" if has_prometheus else None
    cloud.prom["cpu_util"] = [vector("0")]
    result = collect(ctx)
    assert result.source == ("PROMETHEUS" if has_prometheus else "NONE")
    if has_prometheus:
        reading = result.readings["one"][0]
        assert reading.source == "PROMETHEUS" and reading.value == 0 and reading.observed_at == AT
        assert len(cloud.requests) == 1 and cloud.requests[0].url.host == "prom.invalid"
    else:
        assert result.readings == {} and cloud.requests == []


@pytest.mark.parametrize("source", ["PROMETHEUS", "CES"])
def test_legacy_aom_mapping_cannot_override_explicit_working_source(cloud, context, monkeypatch, source):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", source == "CES")
    cloud.prom["cpu_util"] = [vector()]
    result = collect(context(source), aom=MAPPING)
    assert result.source == source and result.readings["one"][0].observed_at == AT
    assert all(row.source == source for row in result.readings["one"])
    assert cloud.requests and all(request.url.host != "aom.invalid" for request in cloud.requests)


def test_prometheus_retains_every_real_point_including_stale_and_zero(cloud, context):
    old = NOW-timedelta(seconds=800)
    cloud.prom["cpu_util"] = [vector("0", old), vector("70", AT), vector("9", AT-timedelta(seconds=60))]
    ctx = context("PROMETHEUS")
    result = collect(ctx, stale_after_sec=120)
    readings = result.readings["one"]
    assert [row.observed_at for row in readings] == [old, AT-timedelta(seconds=60), AT]
    assert [row.value for row in readings] == [0, 9, 70]
    assert all(row.unit == "%" and row.source == "PROMETHEUS" and row.period_sec == 0 for row in readings)
    assert result.diagnostic["metrics"]["cpu_util"]["stale_points"] == 1
    assert result.diagnostic["metrics"]["cpu_util"]["fresh_points"] == 2
    assert result.diagnostic["latest_by_resource"] == {"one": {"cpu_util": AT.isoformat()}}
    assert result.diagnostic["coverage"] == 1 and ctx.errors == [] and len(cloud.requests) == 1


def test_prometheus_requests_only_unique_supported_requested_aliases(cloud, context):
    cloud.prom["cpu_util"] = [vector()]
    result = collect(context("PROMETHEUS"), metrics=["cpu_util", "cpu_util", "unsupported"])
    assert [row.metric for row in result.readings["one"]] == ["cpu_util"]
    assert len(cloud.requests) == 1
    assert cloud.requests[0].url.params["query"] == DEFAULT_QUERIES["cpu_util"]


@pytest.mark.parametrize("value", [True, False, "NaN", "inf", "-inf", -1, 100.01, None, "bad"])
def test_ces_invalid_values_never_become_zero(cloud, context, monkeypatch, value):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    cloud.ces_rows = [{"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [
        {"name": "instance_id", "value": "one"}], "datapoints": [
        {"timestamp": int(AT.timestamp()*1000), "average": value}]}]
    result = collect(context("CES"))
    assert result.readings == {} and result.diagnostic["metrics"]["cpu_util"]["point_count"] == 0
    assert len(cloud.requests) == 1 and cloud.requests[0].url.host == "ces.invalid"
