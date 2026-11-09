"""ManageOne 8.5.1 read-only API contract using isolated synthetic HTTP only."""
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.crypto import encrypt
from app.metrics.ecs_diagnostics import diagnostic_message
from app.metrics.ecs_readings import METRICS
from app.metrics.manageone import INDICATORS, collect_manageone


class Context:
    def __init__(self, responder, *, count=3):
        self.tenant_id, self.project_ref, self.project_id = 7, 77, "synthetic-project"
        self.tenant = SimpleNamespace(manageone_config={
            "enabled": True, "endpoint": "https://manageone.integration.invalid/base/",
            "username": "synthetic-northbound", "organization": "synthetic-org",
            "secret_cipher": encrypt("synthetic-independent-password"),
        }, metrics_source="MANAGEONE")
        self.client = SimpleNamespace()
        self.client.http = httpx.Client(transport=httpx.MockTransport(responder))
        self.calls: list[tuple] = []
        self.client._record = lambda service, **kwargs: self.calls.append((service, kwargs))
        self.errors: list[str] = []
        self.rows = [{"nativeId": f"ecs-{i}", "projectId": self.project_id, "resId": f"object-{i}",
                      "ownerId": "synthetic-source", "ownerType": "compute"}
                     for i in range(count)]
        self.requests: list[httpx.Request] = []


def result_shell(aliases=None):
    aliases = aliases or ["cpu_util", "mem_util", "disk_util"]
    diagnostic = {"status": "no_data", "requested_resources": 0, "stale_after_sec": 900,
        "metrics": {key: {"status": "no_data", "reason": None, "series_count": 0, "point_count": 0,
            "resource_count": 0, "fresh_points": 0, "stale_points": 0, "errors": 0, "rejected_points": 0}
            for key in aliases}}
    return diagnostic, {}


def response(ctx, request):
    ctx.requests.append(request)
    path = request.url.path
    if path.endswith("/sessions"):
        assert request.method == "PUT"
        body = json.loads(request.content)
        assert body == {"grantType": "password", "userName": "synthetic-northbound",
                        "value": "synthetic-independent-password", "organizationName": "synthetic-org"}
        return httpx.Response(200, json={"accessSession": "synthetic-session", "roaRand": "synthetic-roa-rand"})
    assert request.headers["x-auth-token"] == "synthetic-session"
    if path.endswith("/resources/CLOUD_VM"):
        assert request.method == "GET" and request.url.params["limit"] == "1000"
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"objList": ctx.rows[offset:offset+1000], "totalNum": len(ctx.rows)})
    if path.endswith("/history-data/action/query"):
        assert request.method == "POST" and request.headers["roarand"] == "synthetic-roa-rand"
        body = json.loads(request.content)
        assert body["obj_type_id"] == 562958543355904
        assert len(body["obj_ids"]) * len(body["indicator_ids"]) <= 100
        assert "projectIds" not in body
        assert body["interval"] == "MINUTE" and body["range"] == "LAST_5_MINUTE"
        now_ms = int(time.time() * 1000)
        metrics = {}
        for alias, value in (("cpu_util", "0"), ("mem_util", "48.5"), ("disk_util", "93")):
            metric_id = INDICATORS[alias]
            if metric_id in body["indicator_ids"]:
                metrics[str(metric_id)] = {"series": [{str(now_ms - 60000): value}]}
        data = {object_id: metrics for object_id in body["obj_ids"]}
        return httpx.Response(200, json={"status_code": 200, "error_code": 0, "error_msg": "ignored", "data": data})
    pytest.fail(f"unexpected ManageOne route {request.method} {path}")


def test_reads_scoped_cvm_usage_and_actual_timestamps_only():
    ctx = Context(lambda request: response(ctx, request))
    diagnostic, out = result_shell()
    collect_manageone(ctx, [f"ecs-{i}" for i in range(3)], list(METRICS), diagnostic, out)
    assert len(ctx.requests) == 3
    assert [request.method for request in ctx.requests] == ["PUT", "GET", "POST"]
    assert ctx.requests[0].url.path == "/base/rest/plat/smapp/v1/sessions"
    assert ctx.requests[1].url.path == "/base/rest/tenant-resource/v1/tenant/resources/CLOUD_VM"
    assert ctx.requests[2].url.path == "/base/rest/performance/v1/data-svc/history-data/action/query"
    assert set(out) == {f"ecs-{i}" for i in range(3)}
    assert {point.value for point in out["ecs-0"] if point.metric == "cpu_util"} == {0}
    assert {point.value for point in out["ecs-0"] if point.metric == "mem_util"} == {48.5}
    disk = next(point for point in out["ecs-0"] if point.metric == "disk_util")
    assert disk.value == 93 and disk.unit == "%" and disk.period_sec == 60
    assert disk.observed_at < datetime.now(UTC)-timedelta(seconds=30)
    assert disk.dimensions == {"semantics": "cloud_volume"}
    assert all(point.source == "MANAGEONE" for rows in out.values() for point in rows)
    assert len(ctx.calls) == 3
    assert all(service == "manageone" and entry["calls"] == 1 and entry["errors"] == 0
               for service, entry in ctx.calls)
    assert all(ctx.client.http is not None for _ in ctx.requests)


def test_skips_other_projects_and_ambiguous_native_id_mappings():
    ctx = Context(lambda request: response(ctx, request))
    ctx.rows = [
        {"nativeId": "ecs-one", "projectId": "other-project", "resId": "wrong-object",
         "ownerId": "synthetic-source", "ownerType": "compute"},
        {"nativeId": "ecs-one", "projectId": ctx.project_id, "resId": "right-object",
         "ownerId": "synthetic-source", "ownerType": "compute"},
        {"nativeId": "ecs-many", "projectId": ctx.project_id, "resId": "object-1",
         "ownerId": "synthetic-source", "ownerType": "compute"},
        {"nativeId": "ecs-many", "projectId": ctx.project_id, "resId": "object-2",
         "ownerId": "synthetic-source", "ownerType": "compute"},
    ]
    diagnostic, out = result_shell(["cpu_util"])

    def inspect_request(request):
        ctx.requests.append(request)
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"accessSession": "synthetic-session", "roaRand": "synthetic-roa-rand"})
        assert request.url.path.endswith("/resources/CLOUD_VM")
        return httpx.Response(200, json={"objList": ctx.rows, "totalNum": len(ctx.rows)})

    ctx.client.http.close()
    ctx.client.http = httpx.Client(transport=httpx.MockTransport(inspect_request))
    ctx.client._record = lambda service, **kwargs: None
    collect_manageone(ctx, ["ecs-one", "ecs-many", "not-present"], ["cpu_util"], diagnostic, out)
    assert [request.url.path.rsplit("/", 1)[-1] for request in ctx.requests] == ["sessions", "CLOUD_VM"]
    assert out == {}
    assert diagnostic["metrics"]["cpu_util"]["reason"] == "scope_mismatch"
    assert any("其他项目或重复对象" in message for message in ctx.errors)


def test_optional_owner_type_is_not_assumed_to_name_the_source_system():
    ctx = Context(lambda request: response(ctx, request), count=1)
    ctx.rows = [{"nativeId": "ecs-0", "projectId": ctx.project_id, "resId": "object-0",
                 "ownerId": "synthetic-source-id", "ownerType": None}]
    diagnostic, out = result_shell(["cpu_util"])

    def optional_owner(request):
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"accessSession": "synthetic-session", "roaRand": "synthetic-roa-rand"})
        ctx.requests.append(request)
        if request.url.path.endswith("/resources/CLOUD_VM"):
            return httpx.Response(200, json={"objList": ctx.rows, "totalNum": len(ctx.rows)})
        return response(ctx, request)

    ctx.client.http.close()
    ctx.client.http = httpx.Client(transport=httpx.MockTransport(optional_owner))
    collect_manageone(ctx, ["ecs-0"], ["cpu_util"], diagnostic, out)
    assert {point.value for point in out["ecs-0"]} == {0}


@pytest.mark.parametrize("series", [[], [{"bad": "48"}], [{"9999999999999": "48"}],
                                   [{"1760000000000": "NaN"}], [{"1760000000000": "101"}]])
def test_invalid_or_out_of_window_points_never_become_usage(series):
    metric_id = INDICATORS["cpu_util"]
    ctx = Context(lambda request: httpx.Response(200, json={
        "accessSession": "synthetic-session", "roaRand": "synthetic-roa-rand"}) if request.url.path.endswith("/sessions")
        else httpx.Response(200, json={"objList": ctx.rows, "totalNum": 1}) if request.url.path.endswith("resources/CLOUD_VM")
        else httpx.Response(200, json={"data": {"object-0": {str(metric_id): {"series": series}}}}))
    ctx.rows = [{"nativeId": "ecs-0", "projectId": ctx.project_id, "resId": "object-0",
                 "ownerId": "synthetic-source", "ownerType": "compute"}]
    diagnostic, out = result_shell(["cpu_util"])
    collect_manageone(ctx, ["ecs-0"], ["cpu_util"], diagnostic, out)
    assert out == {}


def test_no_credentials_or_config_never_sends_requests():
    ctx = Context(lambda _request: pytest.fail("missing tenant config must not make a request"))
    ctx.tenant.manageone_config = {"enabled": False}
    diagnostic, out = result_shell(["cpu_util"])
    collect_manageone(ctx, ["ecs-0"], ["cpu_util"], diagnostic, out)
    assert ctx.requests == [] and out == [] if isinstance(out, list) else ctx.requests == [] and out == {}
    assert diagnostic["metrics"]["cpu_util"]["reason"] == "manageone_disabled"
    assert "密码" not in diagnostic_message(diagnostic)


def test_native_mapping_marks_boot_time_unsupported_without_querying_it():
    ctx = Context(lambda request: response(ctx, request), count=1)
    diagnostic, out = result_shell(["cpu_util", "boot_time"])
    collect_manageone(ctx, ["ecs-0"], ["cpu_util", "boot_time"], diagnostic, out)
    body = json.loads(ctx.requests[-1].content)
    assert body["indicator_ids"] == [INDICATORS["cpu_util"]]
    assert diagnostic["metrics"]["boot_time"]["status"] == "mapping_missing"
    assert diagnostic["metrics"]["boot_time"]["reason"] == "manageone_metric_unsupported"


def test_aksk_signs_each_exact_request_without_sessions_or_inherited_auth(caplog):
    from app.hcs import signer

    ak, sk = 'synthetic-ops-access-key', 'synthetic-ops-secret-key'
    history_ids = []

    def signed_response(request):
        ctx.requests.append(request)
        assert signer.verify(request.method, request.url.path, request.url.params.multi_items(), dict(request.headers),
                             request.content, lambda key: sk if key == ak else None) == ak
        assert 'x-auth-token' not in request.headers and 'roarand' not in request.headers
        assert 'cookie' not in request.headers and 'x-inherited-private' not in request.headers
        assert 'x-project-id' not in request.headers  # 不继承 HCS 客户端的项目认证头。
        if request.url.path.endswith('/resources/CLOUD_VM'):
            assert request.method == 'GET' and dict(request.url.params) == {'limit': '1000', 'offset': '0'}
            return httpx.Response(200, json={'objList': ctx.rows, 'totalNum': len(ctx.rows)})
        assert request.url.path.endswith('/history-data/action/query') and request.method == 'POST'
        body = json.loads(request.content)
        assert set(body) == {'obj_type_id', 'indicator_ids', 'obj_ids', 'interval', 'range'}
        history_ids.extend(body['obj_ids'])
        now = str(int(time.time() * 1000) - 60000)
        return httpx.Response(200, json={'data': {sid: {str(metric): {'series': [{now: '36.5'}]}
            for metric in body['indicator_ids']} for sid in body['obj_ids']}})

    ctx = Context(signed_response, count=3)
    ctx.tenant.manageone_config = {'enabled': True, 'endpoint': 'https://manageone.integration.invalid:8443/gateway',
                                  'auth_type': 'AKSK', 'access_key': ak, 'sk_cipher': encrypt(sk)}
    ctx.client.http.headers.update({'x-auth-token': 'unrelated-token', 'x-inherited-private': 'private-default'})
    ctx.client.http.auth = httpx.BasicAuth('unrelated-user', 'unrelated-password')
    ctx.client.http.cookies.set('unrelated', 'secret-cookie')
    diagnostic, out = result_shell()
    collect_manageone(ctx, ['ecs-1'], ['cpu_util', 'mem_util', 'disk_util'], diagnostic, out)
    assert [request.method for request in ctx.requests] == ['GET', 'POST']
    assert all(request.url.host == 'manageone.integration.invalid' and request.url.port == 8443
               and request.url.path.startswith('/gateway/rest/') for request in ctx.requests)
    assert history_ids == ['object-1'] and set(out) == {'ecs-1'}
    assert {point.value for point in out['ecs-1']} == {36.5}
    assert ctx.errors == []
    assert ak not in caplog.text and sk not in caplog.text and 'unrelated' not in caplog.text


@pytest.mark.parametrize('code', [401, 403, 302])
def test_aksk_failure_does_not_fallback_or_follow_redirect(code, caplog):
    def rejected(request):
        ctx.requests.append(request)
        return httpx.Response(code, headers={'location': 'https://other.invalid/steal'},
                              json={'message': 'untrusted upstream secret-value'})
    ctx = Context(rejected)
    ctx.tenant.manageone_config.update(auth_type='AKSK', access_key='synthetic-ak', sk_cipher=encrypt('synthetic-sk'))
    diagnostic, out = result_shell()
    collect_manageone(ctx, ['ecs-0'], ['cpu_util'], diagnostic, out)
    assert len(ctx.requests) == 1 and ctx.requests[0].method == 'GET' and out == {}
    assert ctx.errors and diagnostic['metrics']['cpu_util']['reason'] == 'query_failed'
    assert 'synthetic-ak' not in caplog.text and 'synthetic-sk' not in caplog.text and 'secret-value' not in caplog.text


@pytest.mark.parametrize('updates', [
    {'sk_cipher': ''}, {'access_key': ''}, {'sk_cipher': 'plaintext-sk'}, {'auth_type': 'UNKNOWN'},
    {'sk_cipher': 'ENC(AES256GCM:invalid)'}, {'access_key': 'bad\nkey'},
])
def test_invalid_aksk_config_never_reuses_password_or_iam(updates):
    ctx = Context(lambda request: pytest.fail('invalid AK/SK must not issue requests'))
    ctx.tenant.manageone_config.update(auth_type='AKSK', access_key='synthetic-ak', sk_cipher=encrypt('synthetic-sk'))
    ctx.tenant.manageone_config.update(updates)
    diagnostic, out = result_shell(['cpu_util'])
    collect_manageone(ctx, ['ecs-0'], ['cpu_util'], diagnostic, out)
    assert out == {} and ctx.errors


def test_empty_selected_instances_do_not_authenticate_or_query():
    ctx = Context(lambda request: pytest.fail('empty selection must not query ManageOne'))
    ctx.tenant.manageone_config.update(auth_type='AKSK', access_key='synthetic-ak', sk_cipher=encrypt('synthetic-sk'))
    diagnostic, out = result_shell(['cpu_util'])
    collect_manageone(ctx, [], ['cpu_util'], diagnostic, out)
    assert out == {} and ctx.requests == [] and ctx.errors == []
