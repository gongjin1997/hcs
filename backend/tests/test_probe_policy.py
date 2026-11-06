"""Probe 零 AOM 请求、默认零 CES 与脱敏；全部使用合成配置/MockTransport。"""
import json
from collections import OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.hcs import auth, ratelimit
from app.hcs.auth import TokenAuth, TokenProvider
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.models import HcsAccount, HcsProject
from app.tools import probe


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", False)
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    monkeypatch.setattr(auth, "_tokens", OrderedDict())
    monkeypatch.setattr(ratelimit, "_shared", {})


def descriptor(code):
    return next(item for item in probe.PROBES if item.code == code)


@pytest.fixture()
def local_probe(mem_db, monkeypatch):
    account = HcsAccount(code="probe-isolated", name="测试租户", auth_type="PASSWORD", iam_endpoint="https://iam.invalid",
                         domain_name="test", username="reader", secret_cipher=encrypt("synthetic-password"))
    mem_db.add(account)
    mem_db.flush()
    project = HcsProject(account_id=account.id, region_id="test", project_id="test-project", external_domain="invalid")
    mem_db.add(project)
    mem_db.commit()
    calls = []
    state = {"configured": False, "status": 200, "body": {"metrics": [{"namespace": "PAAS.NODE", "metricName": "test_cpu",
                                                                     "dimensions": [], "unit": "Percent"}]}}
    def respond(request):
        calls.append(request)
        if request.url.path == "/v3/auth/tokens":
            return httpx.Response(201, headers={"X-Subject-Token": "synthetic-probe-token"},
                                  json={"token": {"expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}})
        if request.url.path == "/v1/test-project/ams/metrics":
            return httpx.Response(state["status"], json=state["body"])
        if request.url.path == "/V1.0/test-project/metrics":
            return httpx.Response(state["status"], json=state["body"])
        pytest.fail("unexpected mock cloud request")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        def make_client(*_args, **_kwargs):
            templates = {"ces": "https://legacy.invalid"}
            if state["configured"]:
                templates["aom"] = "https://explicit-aom.invalid"
            tokens = TokenProvider(http, "https://iam.invalid", "test", "reader", "synthetic-password", project.project_id,
                                   tenant_id=account.id, project_ref=project.id)
            return HcsClient(http, TokenAuth(tokens), EndpointResolver(templates, "test", "invalid"), project.project_id,
                             tenant_id=account.id, project_ref=project.id, qps={"aom": 1000, "ces": 1000}, max_retries=0,
                             ces_enabled=account.metrics_source == "CES")
        @contextmanager
        def session():
            yield mem_db
        monkeypatch.setattr(probe, "SessionLocal", session)
        monkeypatch.setattr(probe, "client_for", make_client)
        yield account, project, make_client, calls, state


def test_disabled_ces_runner_and_cli_do_not_authenticate_or_send(local_probe, monkeypatch, tmp_path):
    account, project, make, calls, _state = local_probe
    monkeypatch.setattr(probe, "ecs_metrics", lambda *_a, **_kw: pytest.fail("disabled CES must not be called"))
    runner = probe.Runner(make(), project, 100, False)
    runner.ctx["ecs_id"] = "synthetic-server"
    assert runner.run(descriptor("ces.metrics"))["result"] == "SKIP"
    assert runner.ces_ecs()["result"] == "SKIP" and calls == []
    output = tmp_path / "safe-probe.json"
    assert probe.main(["--tenant", account.code, "--only", "ces", "--out", str(output)]) == 0
    report = json.loads(output.read_text())
    assert all(row["result"] == "SKIP" for row in report["results"]) and calls == []


def test_unconfigured_aom_is_skipped_even_with_mock_url_or_global_setting(local_probe, monkeypatch, tmp_path):
    account, project, make, calls, _state = local_probe
    monkeypatch.setitem(get_settings().hcs_endpoints, "aom", "https://global.invalid")
    current = make()
    current.mock_url = "http://mock"
    assert probe.Runner(current, project, 100, False).run(descriptor("aom.metrics"))["result"] == "SKIP"
    output = tmp_path / "aom-skipped.json"
    assert probe.main(["--tenant", account.code, "--only", "aom", "--out", str(output)]) == 0
    assert calls == [] and json.loads(output.read_text())["summary"] == {"SKIP": 2}


@pytest.mark.parametrize("extra", [[], ["--all"], ["--raw"], ["--all", "--raw"]])
def test_configured_aom_cli_skips_before_client_construction_and_auth(local_probe, monkeypatch, tmp_path, extra):
    account, _project, _make, calls, state = local_probe
    state["configured"] = True
    monkeypatch.setattr(probe, "client_for", lambda *_a, **_kw: pytest.fail("disabled-only probe must not build a client"))
    output = tmp_path / "disabled.json"
    assert probe.main(["--tenant", account.code, "--only", "aom", "--out", str(output), *extra]) == 0
    report = json.loads(output.read_text())
    assert report["summary"] == {"SKIP": 2} and calls == []
    row = report["results"][-1]
    assert row["evidence_scope"] == "disabled" and row["available"] is False
    assert row["product_metrics_verified"] is False
    assert "synthetic-probe-token" not in output.read_text() and "synthetic-password" not in output.read_text()


@pytest.mark.parametrize("body", [{"metrics": [None]}, {"metrics": {}}, {"metrics": [{"unknown": 1}]}])
def test_disabled_aom_does_not_read_or_warn_about_any_upstream_body(local_probe, body):
    _account, project, make, _calls, state = local_probe
    state["configured"], state["body"] = True, body
    result = probe.Runner(make(), project, 1, False).run(descriptor("aom.metrics"))
    assert result["result"] == "SKIP" and _calls == []


@pytest.mark.parametrize("raw", [False, True])
def test_even_raw_probe_samples_never_expose_credential_fields(local_probe, raw, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    _account, project, make, _calls, state = local_probe
    _account.metrics_source = "CES"
    state["configured"] = True
    state["body"]["metrics"][0].update({"password": "synthetic-unsafe-password", "token": "synthetic-unsafe-token"})
    result = probe.Runner(make(), project, 1, raw).run(descriptor("ces.metrics"))
    text = json.dumps(result)
    assert result["result"] == "OK" and _calls
    assert "synthetic-unsafe-password" not in text and "synthetic-unsafe-token" not in text


def test_upstream_error_body_never_enters_probe_report(local_probe, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    _account, project, make, _calls, state = local_probe
    _account.metrics_source = "CES"
    state["configured"], state["status"], state["body"] = True, 403, {"error_code": "AOM.403", "message": "private-reply"}
    result = probe.Runner(make(), project, 1, False).run(descriptor("ces.metrics"))
    assert result["result"] == "WARN" and "AOM.403" in result["message"] and "private-reply" not in json.dumps(result)
    assert "synthetic-private" not in probe._safe_error(HcsError(403, "AOM.403", "synthetic-private"))


def test_legacy_ces_probe_requires_explicit_platform_opt_in(local_probe, monkeypatch):
    _account, project, make, calls, _state = local_probe
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    assert probe.Runner(make(), project, 1, False).run(descriptor("ces.metrics"))["result"] == "SKIP"
    assert calls == []
    _account.metrics_source = "CES"
    result = probe.Runner(make(), project, 1, False).run(descriptor("ces.metrics"))
    assert result["result"] == "OK"
    assert [request.url.path for request in calls] == ["/v3/auth/tokens", "/V1.0/test-project/metrics"]


@pytest.mark.parametrize("body", [{"metrics": []}, {"metrics": [{"namespace": "PAAS.NODE", "metricName": "cpuUsage"}]}])
def test_legacy_aom_metadata_configuration_cannot_enable_a_probe(local_probe, body):
    account, project, make, _calls, state = local_probe
    state.update(configured=True, body=body)
    result = probe.Runner(make(), project, 1, False).run(descriptor("aom.metrics"))
    assert result["result"] == "SKIP" and result["evidence_scope"] == "disabled"
    assert _calls == []
    assert result["product_metrics_verified"] is False and "停用" in result["contract_note"]
    assert account.capabilities == {}


def test_job_names_are_one_encoded_path_segment_in_probe():
    paths = []
    current = SimpleNamespace(get=lambda _service, path, **_kw: paths.append(path) or {"status": "RUNNING"})
    runner = probe.Runner(current, SimpleNamespace(), 1, False)
    runner.ctx.update(workspace="test", dgc_job="folder/job?private=value")
    assert runner.run(descriptor("dataarts.job_status"))["result"] == "OK"
    assert paths == ["/v1/{project_id}/jobs/folder%2Fjob%3Fprivate%3Dvalue/status"]
