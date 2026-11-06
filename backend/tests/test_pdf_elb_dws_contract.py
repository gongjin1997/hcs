"""Uploaded ELB 8.5.1 / DWS 8.2.1.230 PDF contracts, physical pages from 1.

ELB 01 (2025-03-30): pp11-13, 41-48. DWS 01 (2024-10-30): pp42-51.
Every response crosses HcsClient.get through a local httpx.MockTransport.
Only synthetic JSON objects are returned; no network, credentials or DB access.
"""
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.hcs import ratelimit
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.ledger.fetchers import fetch_dws, fetch_elb
from app.tools.probe import PROBES, Runner


class LocalAuth:
    kind = "LOCAL_PDF_TEST"

    def apply(self, request, service):
        pass

    def refresh(self):
        return False


@pytest.fixture
def pdf_client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "hcs_total_qps", 100)
    monkeypatch.setattr(ratelimit, "_shared", {})
    with ExitStack() as stack:
        def build(*responses, default_endpoints=False):
            queued = deque(deepcopy(responses))
            calls = []

            def respond(request):
                calls.append(request)
                assert request.method == "GET"
                assert queued, "Unexpected extra API request"
                body = queued.popleft()
                assert isinstance(body, dict), "HcsClient requires a JSON object"
                return httpx.Response(200, json=body)

            http = stack.enter_context(httpx.Client(transport=httpx.MockTransport(respond)))
            templates = (deepcopy(Settings.model_fields["hcs_endpoints"].default) if default_endpoints
                         else {service: f"https://{service}.example.invalid" for service in ("elb", "dws")})
            resolver = EndpointResolver(templates, "region-test", "example.invalid")
            client = HcsClient(http, LocalAuth(), resolver, "project-test", max_retries=0,
                               qps={"elb": 100, "dws": 100})
            return client, calls

        yield build


def loadbalancer(**changes):
    return {"id": "lb-test", "name": "lb-test", "operating_status": "ONLINE", "provisioning_status": "ACTIVE",
            "vip_address": "192.0.2.10", "availability_zone_list": [], "guaranteed": True,
            "listeners": [{"id": "listener-one"}, {"id": "listener-two"}], "pools": [{"id": "pool-one"}],
            "eips": [{"eip_address": "198.51.100.10"}], "vpc_id": "vpc-test", **changes}


def cluster(**changes):
    return {"id": "dws-test", "name": "dws-test", "status": "ACTIVE", "version": "8.2.1.230", "port": 8000,
            "number_of_node": 3, "node_type": "dws.d1.xlarge.ultrahigh", "availability_zone": "az-test",
            "created": "2024-10-30T01:02:03Z", "nodes": [{"id": "node-test", "status": "200"}],
            "endpoints": [{"connect_info": "192.0.2.20:8000"}],
            "public_endpoints": [{"public_connect_info": "198.51.100.20:8000"}], **changes}


def run_probe(client, code):
    descriptor = next(probe for probe in PROBES if probe.code == code)
    return Runner(client, SimpleNamespace(), 37, False).run(descriptor)


def test_elb_pdf_service_name_is_vpc_at_the_actual_client_entry(pdf_client):
    # ELB physical p11, table 1-1: service_name is fixed to vpc.
    client, calls = pdf_client({"loadbalancers": []}, default_endpoints=True)
    assert fetch_elb(client, {}) == []
    assert calls[0].url.host == "vpc.region-test.example.invalid"
    assert calls[0].url.path == "/v3/project-test/elb/loadbalancers"


def test_dws_pdf_endpoint_keeps_the_documented_dws_service_name(pdf_client):
    # DWS physical p526 section 6.7: service_name examples include dws.
    client, calls = pdf_client({"clusters": [], "count": 0}, default_endpoints=True)
    assert fetch_dws(client, {}) == []
    assert calls[0].url.host == "dws.region-test.example.invalid"
    assert calls[0].url.path == "/v1.0/project-test/clusters" and not calls[0].url.params


def test_elb_pdf_probe_sends_only_declared_list_query_parameters(pdf_client):
    # Physical p41 table 5-2 declares cluster_id only; pp12-13 defer to each API.
    client, calls = pdf_client({"loadbalancers": [loadbalancer()]})
    assert run_probe(client, "elb.loadbalancers")["result"] == "OK"
    assert calls[0].url.path == "/v3/project-test/elb/loadbalancers"
    assert not calls[0].url.params


@pytest.mark.parametrize("rows,count", [([], 1), ([loadbalancer()], 2)])
def test_elb_pdf_probe_rejects_contradictory_current_count(pdf_client, rows, count):
    # Physical p47 table 5-13: current_count is the current record count, not a total.
    client, _ = pdf_client({"loadbalancers": rows, "page_info": {"current_count": count}})
    assert run_probe(client, "elb.loadbalancers")["result"] == "FAIL"


def test_elb_pdf_probe_does_not_expect_an_undocumented_member_count(pdf_client):
    client, _ = pdf_client({"loadbalancers": [loadbalancer()]})
    result = run_probe(client, "elb.loadbalancers")
    assert result["result"] == "OK"
    assert "member_count" not in result.get("missing_optional", [])


def test_elb_pdf_last_record_marker_alone_does_not_trigger_another_query(pdf_client):
    # Physical p47 defines next_marker as last record ID, without a has-more flag.
    client, calls = pdf_client({"loadbalancers": [loadbalancer()],
                              "page_info": {"current_count": 1, "previous_marker": "lb-test", "next_marker": "lb-test"}})
    context = {}
    record, = fetch_elb(client, context)
    assert len(calls) == 1 and not calls[0].url.params
    assert record.ip == "192.0.2.10" and record.attrs["eip"] == "198.51.100.10"
    assert record.attrs["listeners"] == 2 and record.attrs["members"] is None
    assert record.attrs["guaranteed"] is True and record.spec is None
    assert record.vcpus is None and record.memory_gb is None and record.storage_gb is None
    assert context["ip_owner"]["192.0.2.10"] == "负载均衡 lb-test"


def test_elb_pdf_probe_accepts_a_consistent_count_and_last_record_marker(pdf_client):
    client, calls = pdf_client({"loadbalancers": [loadbalancer()],
                              "page_info": {"current_count": 1, "previous_marker": "lb-test", "next_marker": "lb-test"}})
    assert run_probe(client, "elb.loadbalancers")["result"] == "OK"
    assert len(calls) == 1


@pytest.mark.parametrize("code,body", [
    ("elb.loadbalancers", {"loadbalancers": [], "page_info": {"current_count": 0}}),
    ("dws.clusters", {"clusters": [], "count": 0}),
])
def test_pdf_empty_list_with_zero_count_is_valid_for_probe(pdf_client, code, body):
    client, _ = pdf_client(body)
    assert run_probe(client, code)["result"] == "OK"


@pytest.mark.parametrize("status", ["ONLINE", "OFFLINE", "DEGRADED", "DISABLED", "NO_MONITOR"])
def test_elb_pdf_operating_status_is_not_replaced_by_provisioning_status(pdf_client, status):
    client, _ = pdf_client({"loadbalancers": [loadbalancer(operating_status=status)]})
    record, = fetch_elb(client, {})
    assert record.status == status


@pytest.mark.parametrize("rows,count", [([], 1), ([loadbalancer()], 2)])
def test_elb_pdf_ledger_already_rejects_contradictory_current_count(pdf_client, rows, count):
    client, _ = pdf_client({"loadbalancers": rows, "page_info": {"current_count": count}})
    with pytest.raises(HcsError):
        fetch_elb(client, {})


@pytest.mark.parametrize("rows,count", [([], 1), ([cluster()], 2)])
def test_dws_pdf_probe_rejects_a_supplied_inconsistent_cluster_total(pdf_client, rows, count):
    # DWS physical p43 table 5-28 declares count; no list pagination is declared.
    client, calls = pdf_client({"clusters": rows, "count": count})
    assert run_probe(client, "dws.clusters")["result"] == "FAIL"
    assert calls[0].url.path == "/v1.0/project-test/clusters" and not calls[0].url.params


@pytest.mark.parametrize("with_count", [True, False])
def test_dws_pdf_list_and_example_without_count_remain_compatible(pdf_client, with_count):
    # Table p43 declares count, but the pp50-51 example omits it: do not invent requiredness.
    body = {"clusters": [cluster()]}
    if with_count:
        body["count"] = 1
    client, calls = pdf_client(body, body)
    record, = fetch_dws(client, {})
    result = run_probe(client, "dws.clusters")
    assert result["result"] == "OK"
    assert record.ip == "192.0.2.20" and record.attrs["node_count"] == 3
    assert record.attrs["node_type"] == "dws.d1.xlarge.ultrahigh"
    assert record.vcpus is None and record.memory_gb is None and record.storage_gb is None
    assert all(call.url.path == "/v1.0/project-test/clusters" and not call.url.params for call in calls)


@pytest.mark.parametrize("endpoint,host", [
    ("192.0.2.20:8000", "192.0.2.20"), ("dws-private.example.invalid:8000", "dws-private.example.invalid"),
    ("[2001:db8::20]:8000", "2001:db8::20"),
])
def test_dws_pdf_private_connection_is_not_replaced_by_public_endpoint(pdf_client, endpoint, host):
    client, calls = pdf_client({"clusters": [cluster(endpoints=[{"connect_info": endpoint}])], "count": 1})
    record, = fetch_dws(client, {})
    assert record.ip == host and len(calls) == 1


def test_dws_pdf_missing_private_connection_does_not_fall_back_to_public_endpoint(pdf_client):
    client, _ = pdf_client({"clusters": [cluster(endpoints=[])], "count": 1})
    record, = fetch_dws(client, {})
    assert record.ip is None


@pytest.mark.parametrize("status", [
    "CREATING", "ACTIVE", "AVAILABLE", "FAILED", "CREATION FAILED", "DELETING", "DELETION FAILED",
    "DELETED", "STOPING", "STOPPED", "STARTING", "FROZEN",
])
def test_dws_pdf_list_states_remain_verbatim_and_do_not_use_node_status(pdf_client, status):
    client, _ = pdf_client({"clusters": [cluster(status=status)], "count": 1})
    record, = fetch_dws(client, {})
    assert record.status == status


@pytest.mark.parametrize("rows,count", [([], 1), ([cluster()], 2), ([cluster()], 0)])
def test_dws_pdf_ledger_already_rejects_a_supplied_inconsistent_total(pdf_client, rows, count):
    client, _ = pdf_client({"clusters": rows, "count": count})
    with pytest.raises(HcsError):
        fetch_dws(client, {})
