"""上传现场日志的回归：永久 DNS/TLS 错误、服务端点和重复 CCE 探测。"""
import json
import socket
import ssl

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.diagnostics import logical_path, network_error
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.models import HcsProject
from app.services.tenant_check import check_tenant

from .conftest import MOCK_URL


@pytest.mark.parametrize("path", ["/v1/{project_id}/cloudservers/detail", "/v1.1/{project_id}/cluster_infos",
                                  "/gaussdb/v3.3/{project_id}/instances"])
def test_documented_static_routes_remain_identifiable_in_logs(path):
    assert logical_path(path) == path
    assert "private-name" not in logical_path(path + "/private-name")


@pytest.mark.parametrize("cause,retryable", [
    (socket.gaierror(socket.EAI_NONAME, "private-host"), False),
    (socket.gaierror(socket.EAI_AGAIN, "private-host"), True),
    (ssl.SSLCertVerificationError(1, "private-certificate"), False),
    (httpx.ReadTimeout("private-url"), True),
])
def test_network_reason_has_retry_policy_without_private_exception(cause, retryable):
    outer = httpx.ConnectError("private-url")
    outer.__cause__ = cause
    error = network_error(outer)
    assert error.retryable is retryable
    assert error.reason in {"dns", "certificate", "read_timeout"}
    assert "private-" not in str(error)


@pytest.mark.parametrize("cause,attempts", [
    (socket.gaierror(socket.EAI_NONAME, "private-host"), 1),
    (socket.gaierror(socket.EAI_AGAIN, "private-host"), 3),
    (ssl.SSLCertVerificationError(1, "private-certificate"), 1),
])
def test_client_only_retries_transient_network_failures(cause, attempts):
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ConnectError("private-url", request=request) from cause

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, AkSkAuth("test-ak", "test-sk"),
                            EndpointResolver({"ecs": "https://ecs.example.invalid"}, "r", "example.invalid"),
                            "project", max_retries=2, backoff=0)
        with pytest.raises(HcsError):
            current.get("ecs", "/v1/{project_id}/cloudservers/detail")
    assert len(calls) == attempts


@pytest.mark.parametrize("template", [
    "https://user:private-password@host.invalid",
    "https://host.invalid/?token=private-token",
    "https://host.invalid/#private-fragment",
    "ftp://host.invalid", "https://host.invalid:bad", "https://host.invalid/has space",
])
def test_invalid_endpoint_is_a_safe_local_error_before_sending(template):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as http:
        current = HcsClient(http, AkSkAuth("test-ak", "test-sk"),
                            EndpointResolver({"ecs": template}, "r", "example.invalid"), "project")
        with pytest.raises(HcsError) as captured:
            current.get("ecs", "/v1/{project_id}/cloudservers/detail")
    assert captured.value.code == "LOCAL.ENDPOINT"
    assert "private-" not in str(captured.value)
    assert not calls


def test_successful_cce_probe_is_reused_for_capabilities(world, mem_db, mock_http, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    calls = []
    # HcsClient uses send(), so observe the public response hook instead of replacing get().
    def hook(response):
        calls.append(str(response.request.url))
    mock_http.event_hooks.setdefault("response", []).append(hook)
    try:
        result = check_tenant(mem_db, world["tenant-b"], http=mock_http, mock_url=MOCK_URL)
    finally:
        mock_http.event_hooks["response"].remove(hook)
    assert result["ok"]
    assert len([url for url in calls if url.endswith("/clusters")]) == 1


def test_failed_service_checks_report_resolved_endpoint_and_do_not_reprobe_cce(world, mem_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    tenant = world["tenant-b"]
    tenant.iam_endpoint = "https://iam.example.invalid"
    tenant.endpoints = {service: f"https://{service}.{{region}}.{{domain}}" for service in
                        ("ecs", "cce", "dcs", "gaussdb", "mrs", "dataarts")}
    tenant.metrics_source = "NONE"
    tenant.prometheus_url = None
    for project in mem_db.scalars(select(HcsProject).where(HcsProject.account_id == tenant.id)):
        project.region_id = "region-1_team"
        project.project_name = "team"
        project.external_domain = "console.example.invalid"
        project.workspaces = []
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "iam.example.invalid":
            return httpx.Response(200, json={"projects": []})
        raise httpx.ConnectError("private-detail") from socket.gaierror(socket.EAI_NONAME, "private-host")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = check_tenant(mem_db, tenant, http=http)
    assert result["ok"] is False
    assert len(result["configuration_warnings"]) == 2
    services = [item for item in result["checks"] if item["kind"] == "service"]
    assert len(services) == 5
    for item in services:
        assert item["network_reason"] == "dns" and item["retryable"] is False
        assert item["endpoint"].startswith("https://")
        assert ".region-1_team.console.example.invalid" in item["endpoint"]
        assert item["endpoint_source"] == "tenant"
        assert item["cost_ms"] >= 0
    assert len(calls) == 6  # One IAM request plus one request per enabled service.
    assert "private-" not in json.dumps(result)
