"""HCS访问层/ECS性能来源复核：仅合成传输，不能证明正式API可用。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import get_settings
from app.hcs import HcsClient, HcsError
from app.hcs.auth import TokenAuth, TokenProvider
from app.hcs.endpoints import EndpointResolver
from app.hcs.factory import client_for
from app.hcs.query_cache import QueryCache
from app.hcs.ratelimit import request_budget
from app.metrics import guarded_ces
from app.metrics.ces import ces_query
from app.metrics.prometheus import DEFAULT_QUERIES, PromClient, PromError

from .test_ecs_readings import AT, NOW, collect, vector
from .test_ecs_readings import cloud as cloud
from .test_ecs_readings import context as context
from .test_ecs_readings import isolated as isolated


def access(http, *, project="project-a", auth=None, **kwargs):
    auth = auth or SimpleNamespace(kind="TEST", apply=lambda *_: None, refresh=lambda: False)
    return HcsClient(http, auth, EndpointResolver({"dcs": "https://dcs.invalid", "ces": "https://ces.invalid",
                                                "gaussdb": "https://gaussdb.invalid"},
                                                "region", "invalid"), project, max_retries=0,
                     qps={"dcs": 100000, "ces": 100000}, **kwargs)


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "DELETE"])
@pytest.mark.parametrize("service", ["aom", "AOM", " aom "])
def test_every_aom_entry_stops_before_auth_and_network(method, service):
    calls = []
    auth = SimpleNamespace(kind="TEST", apply=lambda *_: calls.append("auth"), refresh=lambda: False)
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append("http") or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError) as error:
            access(http, auth=auth).request(service, method, "/v2/{project_id}/series")
    assert error.value.code == "LOCAL.AOM_DISABLED" and calls == []


@pytest.mark.parametrize("global_enabled,source", [(False, "CES"), (True, "AUTO"), (True, "NONE"),
                                                 (True, "PROMETHEUS"), (True, "AOM")])
def test_ces_requires_two_explicit_switches_at_guarded_entry(monkeypatch, global_enabled, source):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", global_enabled)
    calls = []
    ctx = SimpleNamespace(tenant=SimpleNamespace(metrics_source=source), errors=[])
    assert guarded_ces(ctx, lambda: calls.append(1) or {}, {}) == {} and calls == []


@pytest.mark.parametrize("service", ["ces", "CES", " ces "])
def test_direct_ces_client_cannot_bypass_tenant_opt_in(monkeypatch, service):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError) as error:
            access(http).get(service, "/V1.0/{project_id}/metrics")
    assert error.value.code == "LOCAL.CES_DISABLED" and calls == []


def test_doubly_authorized_guarded_ces_can_make_one_query(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        client = access(http)
        ctx = SimpleNamespace(client=client, tenant=SimpleNamespace(metrics_source="CES", capabilities={}), errors=[])
        assert guarded_ces(ctx, lambda: client.get("ces", "/V1.0/{project_id}/metrics"), {}) == {}
    assert len(calls) == 1


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "X-Method-Override", "X-HTTP-Method"])
def test_read_only_cannot_be_bypassed_with_method_override_headers(header):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError) as error:
            access(http).get("dcs", "/v2/{project_id}/instances", headers={header: "DELETE"})
    assert error.value.code == "LOCAL.READ_ONLY" and calls == []


@pytest.mark.parametrize("project", ["other/project", "a?scope=other", "a#fragment", "a%2fb", "a\\b", " a "])
def test_project_substitution_cannot_change_url_structure(project):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError):
            access(http, project=project).get("dcs", "/v2/{project_id}/instances")
    assert calls == []


@pytest.mark.parametrize("path", ["/v2/{project_id}/../other/instances", "/v2/{project_id}/%2e%2e/other"])
def test_project_path_traversal_is_rejected_before_network(path):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError):
            access(http).get("dcs", path)
    assert calls == []


def test_project_identity_header_cannot_override_client_scope():
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        with pytest.raises(HcsError):
            access(http).get("dcs", "/v2/{project_id}/instances", headers={"X-Project-Id": "project-b"})
    assert calls == []


@pytest.mark.parametrize("cluster", ["native/path", "native?query", "native#fragment", "native@other", "native\\path"])
def test_endpoint_variables_cannot_escape_their_component(cluster):
    resolver = EndpointResolver({"cce": "https://{cluster_id}.cce.{region}.{domain}"}, "region", "invalid")
    with pytest.raises(ValueError):
        resolver.base("cce", cluster_id=cluster)


@pytest.mark.parametrize("iam", ["https://iam.invalid?secret=synthetic", "https://reader:synthetic@iam.invalid",
                                "https://iam.invalid#fragment"])
def test_iam_endpoint_is_validated_before_sending_password(iam):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(
            201, headers={"X-Subject-Token": "synthetic"}, json={}))) as http:
        provider = TokenProvider(http, iam, "domain", "reader", "synthetic", "project-a")
        with pytest.raises(HcsError) as error:
            provider._request({"auth": {}})
    assert error.value.code == "LOCAL.ENDPOINT" and calls == []


def test_iam_explicit_foreign_project_token_is_not_cached(monkeypatch):
    provider = TokenProvider(None, "https://iam.invalid", "domain", "reader", "synthetic", "project-a")
    response = httpx.Response(201, headers={"X-Subject-Token": "synthetic"}, json={"token": {
        "expires_at": (datetime.now(UTC)+timedelta(hours=1)).isoformat(), "project": {"id": "project-b"}}})
    monkeypatch.setattr(provider, "_request", lambda _: response)
    with pytest.raises(HcsError) as error:
        provider._load_token()
    assert error.value.code == "LOCAL.PROJECT_MISMATCH"


@pytest.mark.parametrize("body", [
    {"status": "success", "data": {"resultType": "matrix", "result": []}},
    {"status": "success", "data": {"resultType": "scalar", "result": []}},
    {"status": "success", "warnings": ["private-warning"], "data": {"resultType": "vector", "result": []}},
])
def test_prometheus_nonvector_or_partial_success_does_not_look_complete(body, caplog):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        with pytest.raises(PromError):
            PromClient("https://prom.invalid", http=http).query("private-query")
    assert "private-warning" not in caplog.text and "private-query" not in caplog.text


def test_default_cpu_query_preserves_identity_labels_instead_of_grouping_only_by_ip():
    assert "without (cpu, mode)" in DEFAULT_QUERIES["cpu_util"]


@pytest.mark.parametrize("alias", ["cpu_util", "mem_util", "disk_util"])
def test_equal_value_from_two_jobs_is_still_ambiguous(cloud, context, alias):
    cloud.prom[alias] = [vector("20", job="first"), vector("20", job="second")]
    result = collect(context(), metrics=[alias])
    assert result.readings == {} and result.diagnostic["status"] != "ready"


def test_all_stale_readings_do_not_mark_source_ready_or_capability_healthy(cloud, context):
    cloud.prom["cpu_util"] = [vector("0", NOW-timedelta(hours=1))]
    ctx = context()
    result = collect(ctx, stale_after_sec=120)
    assert result.readings["one"][0].value == 0
    assert result.diagnostic["status"] != "ready"
    assert ctx.tenant.capabilities.get("prometheus", {}).get("available") is not True


def test_prometheus_configured_label_dimensions_must_match(cloud, context):
    cloud.prom["cpu_util"] = [vector("20", project="foreign")]
    result = collect(context(), prometheus={"label_filters": {"project": "mine"}})
    assert result.readings == {} and result.diagnostic["status"] != "ready"


def test_retries_share_one_request_time_budget(monkeypatch):
    from app.hcs import client as client_module

    times, calls = [0.0], []
    monkeypatch.setattr(client_module.time, "monotonic", lambda: times[0])
    monkeypatch.setattr(client_module.time, "sleep", lambda delay: times.__setitem__(0, times[0]+delay))
    def respond(request):
        calls.append(request)
        times[0] += 0.6
        return httpx.Response(503, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, request_budget(seconds=1):
        client = access(http)
        client.max_retries = 3
        client.backoff = 0
        with pytest.raises(HcsError):
            client.get("dcs", "/instances")
    assert len(calls) <= 2


def test_factory_refuses_foreign_project_before_credentials_or_http(monkeypatch):
    from app.hcs import factory

    monkeypatch.setattr(factory, "_http", lambda _: pytest.fail("must not allocate transport"))
    monkeypatch.setattr(factory, "decrypt", lambda _: pytest.fail("must not decrypt credentials"))
    with pytest.raises(HcsError) as error:
        client_for(SimpleNamespace(id=101), SimpleNamespace(account_id=202))
    assert error.value.code == "LOCAL.PROJECT_MISMATCH"


@pytest.mark.parametrize("key,value", [("tenant_id", 202), ("project_ref", 22), ("project_id", "other"),
    ("password", "rotated"), ("endpoint_scope", "other-region"), ("iam_url", "https://other-iam.invalid")])
def test_token_cache_identity_separates_tenants_projects_endpoints_and_rotation(key, value):
    args = dict(http=None, iam_url="https://iam.invalid", domain="domain", username="reader", password="synthetic",
                project_id="project-a", tenant_id=101, project_ref=11, endpoint_scope="region-a")
    original = TokenProvider(**args).cache_identity()
    args[key] = value
    assert TokenProvider(**args).cache_identity() != original


@pytest.mark.parametrize("alias,unit", [("cpu_util", "ratio"), ("mem_util", "bytes"), ("boot_time", "ms")])
def test_custom_prometheus_query_requires_verified_units(cloud, context, alias, unit):
    cloud.prom["custom"] = [vector("20")]
    result = collect(context(), metrics=[alias], prometheus={"queries": {alias: "custom"}, "units": {alias: unit}})
    assert result.readings == {} and result.diagnostic["status"] != "ready"


def test_custom_prometheus_query_without_unit_is_not_guessed_as_percent(cloud, context):
    cloud.prom["custom"] = [vector("0.4")]
    result = collect(context(), prometheus={"queries": {"cpu_util": "custom"}})
    assert result.readings == {} and not cloud.requests


def test_explicit_source_timestamp_query_preserves_observation_instead_of_evaluation_time(cloud, context):
    cloud.prom["cpu_util"] = [vector("20", NOW)]
    cloud.prom["source-time"] = [vector(str(AT.timestamp()), NOW)]
    result = collect(context(), prometheus={"timestamp_queries": {"cpu_util": "source-time"}})
    assert result.readings["one"][0].observed_at == AT
    assert len(cloud.requests) == 2
    assert len({request.url.params["time"] for request in cloud.requests}) == 1


@pytest.mark.parametrize("raw", [str(NOW.timestamp()+1), "NaN", "0", "-1"])
def test_invalid_source_timestamp_cannot_fall_back_to_fresh_evaluation_time(cloud, context, raw):
    cloud.prom["cpu_util"] = [vector("20", NOW)]
    cloud.prom["source-time"] = [vector(raw, NOW)]
    result = collect(context(), prometheus={"timestamp_queries": {"cpu_util": "source-time"}})
    assert result.readings == {}


def test_query_time_budget_covers_queue_wait(monkeypatch):
    from app.hcs.ratelimit import remaining_time
    from app.metrics import prometheus

    def acquire(*args, **kwargs):
        assert remaining_time(60) <= 0.05
        raise HcsError(0, "LOCAL.RATE_LIMIT", "safe failure")

    monkeypatch.setattr(prometheus, "acquire_cloud", acquire)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no request"))) as http:
        with pytest.raises(PromError):
            PromClient("https://prom.invalid", http=http, timeout=0.05).query("up")


@pytest.mark.parametrize("offset", [-10000, 60])
def test_legacy_ces_rejects_future_and_out_of_window_points(monkeypatch, offset):
    from app.metrics import ces

    monkeypatch.setattr(ces.time, "time", lambda: NOW.timestamp())
    metric = {"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [{"name": "instance_id", "value": "one"}]}
    row = {**metric, "datapoints": [{"timestamp": (NOW.timestamp()+offset)*1000, "average": 0}]}
    client = SimpleNamespace(post=lambda *a, **kw: {"metrics": [row]})
    assert ces_query(client, [metric], window_sec=600) == []


@pytest.mark.parametrize("change", [{"metric_name": "foreign"},
    {"dimensions": [{"name": "instance_id", "value": "foreign"}]}, {"namespace": "FOREIGN.ECS"},
    {"dimensions": [{"name": "instance_id", "value": "one"}, {"name": "instance_id", "value": "foreign"}]}])
def test_legacy_ces_accepts_only_requested_unambiguous_descriptors(monkeypatch, change):
    from app.metrics import ces

    monkeypatch.setattr(ces.time, "time", lambda: NOW.timestamp())
    metric = {"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [{"name": "instance_id", "value": "one"}]}
    row = {**metric, **change, "datapoints": [{"timestamp": AT.timestamp()*1000, "average": 0}]}
    assert ces_query(SimpleNamespace(post=lambda *a, **kw: {"metrics": [row]}), [metric]) == []


def test_legacy_ces_current_zero_is_not_dropped(monkeypatch):
    from app.metrics import ces

    monkeypatch.setattr(ces.time, "time", lambda: NOW.timestamp())
    metric = {"namespace": "SYS.ECS", "metric_name": "cpu_util", "dimensions": [{"name": "instance_id", "value": "one"}]}
    row = {**metric, "datapoints": [{"timestamp": AT.timestamp()*1000, "average": 0}]}
    assert ces_query(SimpleNamespace(post=lambda *a, **kw: {"metrics": [row]}), [metric]) == [
        ({"instance_id": "one"}, "cpu_util", 0)]


def test_token_auth_client_scope_must_match_auth_provider_before_iam():
    provider = TokenProvider(None, "https://iam.invalid", "domain", "reader", "synthetic", "project-b")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no request"))) as http:
        with pytest.raises(HcsError) as error:
            access(http, auth=TokenAuth(provider)).get("dcs", "/v2/{project_id}/instances")
    assert error.value.code == "LOCAL.PROJECT_MISMATCH"


@pytest.mark.parametrize("instance", ["[2001:db8::1x", "[2001:db8::1]:bad-port", "[2001:db8::1]:9100/private"])
def test_malformed_instance_label_cannot_be_truncated_into_another_ecs_ip(cloud, context, instance):
    cloud.prom["cpu_util"] = [vector("20", instance=instance)]
    result = collect(context(), index={"2001:db8::1": "one"})
    assert result.readings == {}


@pytest.mark.parametrize("at", [NOW+timedelta(seconds=1), None])
def test_explicit_timestamp_query_does_not_hide_invalid_evaluation_timestamp(cloud, context, at):
    cloud.prom["cpu_util"] = [{"metric": {"instance": "10.0.0.1:9100"},
                               "value": [at.timestamp() if at else None, "20"]}]
    cloud.prom["source-time"] = [vector(str(AT.timestamp()), NOW)]
    assert collect(context(), prometheus={"timestamp_queries": {"cpu_util": "source-time"}}).readings == {}


def test_iam_token_reuse_is_scoped_and_credential_rotation_is_not_shared(monkeypatch):
    from collections import OrderedDict

    from app.hcs import auth

    monkeypatch.setattr(auth, "_tokens", OrderedDict())
    tokens, queries = [], []

    def respond(request):
        if request.url.path.endswith("/auth/tokens"):
            token = f"synthetic-{len(tokens)}"
            tokens.append(token)
            return httpx.Response(201, headers={"X-Subject-Token": token}, json={"token": {
                "expires_at": "2030-01-01T00:00:00Z", "project": {"id": "project-a"}}})
        queries.append(request.headers["X-Auth-Token"])
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        for tenant, password in [(101, "first"), (101, "first"), (202, "first"), (101, "rotated")]:
            provider = TokenProvider(http, "https://iam.invalid", "domain", "reader", password, "project-a")
            access(http, auth=TokenAuth(provider), tenant_id=tenant, project_ref=11).get("dcs", "/instances")
    assert len(tokens) == 3 and queries[0] == queries[1] and len(set(queries)) == 3


@pytest.mark.parametrize("suffix", ["slow-sql-execute-node", "slow-sql-list", "slow-sql-detail", "real-time-session"])
def test_pdf_documented_query_posts_remain_usable_without_allowing_actions(suffix):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        assert access(http).post("gaussdb", f"/gaussdb/v3/{{project_id}}/instances/native-id/{suffix}", json={}) == {}
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.parametrize("method,path", [("POST", "/gaussdb/v3/{project_id}/instances/native-id/kill-session"),
    ("POST", "/gaussdb/v3/{project_id}/instances/native-id/actions"), ("POST", "/v2/{project_id}/instances"),
    ("DELETE", "/gaussdb/v3/{project_id}/instances/native-id"),
    ("PATCH", "/gaussdb/v3/{project_id}/instances/native-id"),
    ("PUT", "/gaussdb/v3/{project_id}/instances/native-id")])
def test_mutating_cloud_calls_stop_before_authentication(method, path):
    auth = SimpleNamespace(kind="TEST", apply=lambda *_: pytest.fail("no auth"), refresh=lambda: False)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no network"))) as http:
        with pytest.raises(HcsError) as error:
            access(http, auth=auth).request("gaussdb", method, path, json={})
    assert error.value.code == "LOCAL.READ_ONLY"


def test_local_query_cache_scoped_keys_and_returned_objects_do_not_cross_tenants():
    cache = QueryCache()
    for tenant in (101, 202):
        value, source = cache.get(f"tenant:{tenant}:project:11:auth:synthetic", lambda tenant=tenant: {
            "failed": False, "rows": {"tenant": tenant}})
        assert source == "miss" and value["rows"]["tenant"] == tenant
        value["rows"]["tenant"] = "mutated"
    for tenant in (101, 202):
        value, source = cache.get(f"tenant:{tenant}:project:11:auth:synthetic", lambda: pytest.fail("no reload"))
        assert source == "hit" and value["rows"]["tenant"] == tenant


def test_timestamp_query_failure_cannot_fall_back_to_evaluation_time(cloud, context):
    cloud.prom["cpu_util"] = [vector("0", NOW)]
    cloud.fail.add("source-time")
    result = collect(context(), prometheus={"timestamp_queries": {"cpu_util": "source-time"}})
    assert result.readings == {} and result.diagnostic["status"] == "error"


def test_missing_timestamp_series_cannot_fall_back_to_evaluation_time(cloud, context):
    cloud.prom["cpu_util"] = [vector("0", NOW)]
    result = collect(context(), prometheus={"timestamp_queries": {"cpu_util": "source-time"}})
    assert result.readings == {} and result.diagnostic["status"] != "ready"


def test_guarded_ces_grant_cannot_authorize_another_tenant_client(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        first, other = access(http, tenant_id=101), access(http, tenant_id=202)
        ctx = SimpleNamespace(client=first, tenant=SimpleNamespace(metrics_source="CES", capabilities={}), errors=[])
        assert guarded_ces(ctx, lambda: other.get("ces", "/V1.0/{project_id}/metrics"), {}) == {}
    assert calls == [] and ctx.errors


def test_explicit_foreign_project_label_is_not_accepted_by_ip_alone(cloud, context):
    cloud.prom["cpu_util"] = [vector("0", project_id="foreign-project")]
    result = collect(context())
    assert result.readings == {} and result.diagnostic["status"] != "ready"


def test_legacy_factory_constructs_without_tenant_or_project_variables():
    from app.core.config import Settings
    from app.hcs.factory import build_client

    settings = Settings(hcs_project_id="project-a", hcs_iam_url="https://iam.invalid")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("constructor must not request IAM"))) as http:
        client = build_client(settings, http)
        assert client.project_id == "project-a" and client.ces_enabled is False


@pytest.mark.parametrize("source", ["CES", "AUTO", "NONE", "PROMETHEUS", "AOM"])
def test_tenant_factory_binds_ces_opt_in_to_selected_tenant(monkeypatch, source):
    from app.hcs import factory

    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    auth = SimpleNamespace(kind="TEST", apply=lambda *_: None, refresh=lambda: False)
    monkeypatch.setattr(factory, "tenant_auth", lambda *a, **kw: auth)
    tenant = SimpleNamespace(id=101, code="review", metrics_source=source,
                             iam_endpoint="https://iam.invalid", endpoints={"ces": "https://ces.invalid"})
    project = SimpleNamespace(id=11, account_id=101, project_id="project-a", region_id="region", external_domain="invalid")
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))) as http:
        client = client_for(tenant, project, http)
        if source == "CES":
            assert client.get("ces", "/V1.0/{project_id}/metrics") == {}
        else:
            with pytest.raises(HcsError) as error:
                client.get("ces", "/V1.0/{project_id}/metrics")
            assert error.value.code == "LOCAL.CES_DISABLED"
    assert len(calls) == int(source == "CES")
