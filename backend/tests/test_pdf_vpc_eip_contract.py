"""Uploaded HCS 8.5.1 VPC/EIP contracts, issue 01 (2025-03-30).

Physical pages: VPC 12-14, 22-24, 183-185, 214-218; EIP 7-9, 15-17,
43-46. Every response crosses the current synchronous HcsClient.get through
httpx.MockTransport. Cloud response bodies are JSON objects, never fake lists.
"""
from collections import OrderedDict, deque
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.hcs import auth, ratelimit
from app.hcs.auth import AkSkAuth, TokenAuth, TokenProvider
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.ledger.fetchers import fetch_eip, fetch_subnet, fetch_vpc, marker_all
from app.tools.probe import PROBES, Runner

PROJECT = "pdf-project"
LISTS = [
    ("vpc.vpcs", fetch_vpc, "vpcs"),
    ("vpc.subnets", fetch_subnet, "subnets"),
    ("vpc.publicips", fetch_eip, "publicips"),
]


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
    monkeypatch.setattr(settings, "hcs_total_qps", 1000)
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(auth, "_tokens", OrderedDict())
    with ExitStack() as stack:
        def build(*responses, auth_kind="local"):
            queued = deque(deepcopy(responses))
            calls = []

            def respond(request):
                calls.append(request)
                if request.url.path == "/v3/auth/tokens":
                    assert auth_kind == "token" and request.method == "POST"
                    return httpx.Response(
                        201, headers={"X-Subject-Token": "synthetic-pdf-token"},
                        json={"token": {"expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}},
                    )
                assert request.method == "GET", "Only local query requests are expected"
                assert queued, "Unexpected extra list request"
                body = queued.popleft()
                assert isinstance(body, dict), "HcsClient.request requires a JSON object"
                return httpx.Response(200, json=body)

            http = stack.enter_context(httpx.Client(transport=httpx.MockTransport(respond)))
            resolver = EndpointResolver(deepcopy(Settings.model_fields["hcs_endpoints"].default),
                                        "pdf-region", "example.invalid")
            authentication = LocalAuth()
            if auth_kind == "aksk":
                authentication = AkSkAuth("synthetic-pdf-ak", "synthetic-pdf-sk", PROJECT, "pdf-domain-id")
            elif auth_kind == "token":
                provider = TokenProvider(http, "https://iam.example.invalid", "pdf-domain", "pdf-reader",
                                         "synthetic-pdf-password", PROJECT)
                authentication = TokenAuth(provider)
            client = HcsClient(http, authentication, resolver, PROJECT, max_retries=0,
                               qps={"vpc": 1000, "iam": 1000})
            return client, calls

        yield build


def vpc(**changes):
    return {"id": "vpc-id", "name": "network", "status": "OK", "cidr": "10.0.0.0/16",
            "description": "pdf-contract", "project_id": PROJECT, **changes}


def subnet(**changes):
    return {"id": "subnet-id", "name": "subnet", "status": "ACTIVE", "vpc_id": "vpc-id",
            "cidr": "10.0.1.0/24", "gateway_ip": "10.0.1.1", "dhcp_enable": True,
            "availability_zone": "pdf-az", "neutron_network_id": "subnet-id",
            "neutron_subnet_id": "neutron-ipv4-id", "neutron_subnet_id_v6": "neutron-ipv6-id", **changes}


def publicip(**changes):
    return {"id": "eip-id", "status": "DOWN", "type": "eip_external_network",
            "public_ip_address": "198.51.100.10", "tenant_id": PROJECT, "project_id": PROJECT,
            "create_time": "2019-03-08 02:15:30", "bandwidth_id": "bandwidth-id",
            "bandwidth_name": "bandwidth-name", "bandwidth_size": 10,
            "bandwidth_share_type": "PER", **changes}


def row(key, **changes):
    return {"vpcs": vpc, "subnets": subnet, "publicips": publicip}[key](**changes)


def run_probe(client, code, limit=37):
    descriptor = next(probe for probe in PROBES if probe.code == code)
    return Runner(client, SimpleNamespace(), limit, False).run(descriptor)


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_ledger_uses_documented_vpc_endpoint_and_project_path(pdf_client, code, fetcher, key):
    # VPC pp12/183/214; EIP pp7/43-44: tenant_id in the URI means project ID.
    bodies = [{key: [row(key)]}]
    if key == "vpcs":
        bodies.append({"subnets": []})
    client, calls = pdf_client(*bodies)
    records = fetcher(client, {})
    assert len(records) == 1
    assert calls[0].url.host == "vpc.pdf-region.example.invalid"
    assert calls[0].url.path == f"/v1/{PROJECT}/{key}"
    assert dict(calls[0].url.params) == {"limit": "100"}
    assert all(call.method == "GET" for call in calls)


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_probe_uses_same_endpoint_without_undocumented_offset(pdf_client, code, fetcher, key):
    client, calls = pdf_client({key: [row(key)]})
    result = run_probe(client, code)
    assert result["result"] == "OK" and result["count"] == 1
    assert calls[0].url.host == "vpc.pdf-region.example.invalid"
    assert calls[0].url.path == f"/v1/{PROJECT}/{key}"
    assert dict(calls[0].url.params) == {"limit": "37"}
    assert len(calls) == 1  # A probe samples one page; it does not establish a complete ledger.


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_marker_uses_last_resource_id_and_preserves_all_pages(pdf_client, code, fetcher, key):
    # VPC pp13-14/183/214; EIP pp8-9/44. Never use neutron_subnet_id as marker.
    first = [row(key, id=f"{key}-{index}") for index in range(100)]
    last = row(key, id="tail-resource")
    bodies = [{key: first}, {key: [last]}]
    if key == "vpcs":
        bodies.append({"subnets": []})
    client, calls = pdf_client(*bodies)
    records = fetcher(client, {})
    assert [record.resource_id for record in records] == [item["id"] for item in first] + [last["id"]]
    assert dict(calls[0].url.params) == {"limit": "100"}
    assert dict(calls[1].url.params) == {"limit": "100", "marker": first[-1]["id"]}
    assert len([call for call in calls if call.url.path.endswith(f"/{key}")]) == 2


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_marker_full_final_page_is_followed_by_an_empty_page(pdf_client, code, fetcher, key):
    full_page = [row(key, id=f"{key}-{index}") for index in range(100)]
    bodies = [{key: full_page}, {key: []}]
    if key == "vpcs":
        bodies.append({"subnets": []})
    client, calls = pdf_client(*bodies)
    assert len(fetcher(client, {})) == 100
    assert calls[1].url.params["marker"] == full_page[-1]["id"]


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_empty_network_lists_are_not_errors(pdf_client, code, fetcher, key):
    bodies = [{key: []}]
    if key == "vpcs":
        bodies.append({"subnets": []})
    client, _ = pdf_client(*bodies)
    assert fetcher(client, {}) == []


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_repeated_marker_is_rejected_through_real_client(pdf_client, code, fetcher, key):
    page = [row(key, id=f"{key}-{index}") for index in range(100)]
    client, calls = pdf_client({key: page}, {key: page})
    with pytest.raises(HcsError) as error:
        fetcher(client, {})
    assert error.value.code == "LOCAL.INCOMPLETE_PAGE" and len(calls) == 2


@pytest.mark.parametrize("key", ["vpcs", "subnets", "publicips"])
def test_page_budget_never_returns_a_successful_partial_snapshot(pdf_client, key):
    client, calls = pdf_client({key: [row(key, id="first")]}, {key: [row(key, id="second")]})
    with pytest.raises(HcsError) as error:
        list(marker_all(lambda params: client.get("vpc", f"/v1/{{project_id}}/{key}", params=params),
                        key, limit=1, max_pages=2))
    assert error.value.code == "LOCAL.INCOMPLETE_PAGE" and len(calls) == 2


@pytest.mark.parametrize("status", ["OK", "CREATING", "ERROR"])
def test_vpc_status_is_preserved_without_inventing_a_create_timestamp(pdf_client, status):
    # Table p184 says String, not a closed enum; p185 demonstrates OK.
    client, _ = pdf_client({"vpcs": [vpc(status=status, cidr=None)]}, {"subnets": []})
    record, = fetch_vpc(client, {})
    assert record.resource_id == "vpc-id" and record.status == status
    assert record.spec is None and record.created_time is None


def test_vpc_and_subnet_share_pages_but_use_distinct_resource_identifiers(pdf_client):
    # VPC p216 distinguishes neutron network and IPv4/IPv6 subnet IDs; p218 shows id separately.
    client, calls = pdf_client({"vpcs": [vpc()]}, {"subnets": [subnet(), subnet(id="second-subnet")]})
    context = {}
    vpc_record, = fetch_vpc(client, context)
    subnets = fetch_subnet(client, context)
    assert vpc_record.attrs["subnet_count"] == 2
    assert [item.resource_id for item in subnets] == ["subnet-id", "second-subnet"]
    assert all(item.attrs["vpc_name"] == "network" for item in subnets)
    assert len(calls) == 2
    assert subnets[0].attrs["gateway"] == "10.0.1.1" and subnets[0].attrs["dhcp"] is True
    assert subnets[0].az == "pdf-az" and subnets[0].status == "ACTIVE"


@pytest.mark.parametrize("status", ["ACTIVE", "DOWN", "ERROR", "FREEZED"])
def test_eip_preserves_native_status_and_public_address(pdf_client, status):
    # EIP p44 table 5-29 explicitly lists these four values.
    client, _ = pdf_client({"publicips": [publicip(status=status)]})
    record, = fetch_eip(client, {})
    assert record.resource_id == "eip-id" and record.status == status
    assert record.name == "198.51.100.10" and record.ip == "198.51.100.10"


@pytest.mark.parametrize("size", [0, 10])
@pytest.mark.parametrize("share_type,label", [("PER", "独享"), ("WHOLE", "共享")])
def test_eip_uses_top_level_bandwidth_without_converting_numeric_value(pdf_client, size, share_type, label):
    # EIP p45: bandwidth_size is Long; p71 defines PER/WHOLE. No unit is specified there.
    client, _ = pdf_client({"publicips": [publicip(bandwidth_size=size, bandwidth_share_type=share_type)]})
    record, = fetch_eip(client, {})
    assert record.attrs["bandwidth"] == size
    assert record.attrs["bandwidth_name"] == "bandwidth-name"
    assert record.attrs["share_type"] == label
    # Deliberately do not bless the existing spec's Mbit/s unit: this PDF does not establish it.


def test_eip_create_time_already_populates_created_time(pdf_client):
    # EIP pp45-46 list both create_time and created_at; the example gives different values.
    client, _ = pdf_client({"publicips": [publicip(created_at="2021-05-06T08:33:14",
                                                 updated_at="2021-05-06T08:33:14")]})
    record, = fetch_eip(client, {})
    # UTC is the application's parsing policy, not a timezone guarantee made by this PDF.
    assert record.created_time == datetime(2019, 3, 8, 2, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize("private_ip,owners,expected", [
    (None, {"10.0.1.10": "synthetic-server"}, None),
    ("10.0.1.10", {}, "10.0.1.10"),
    ("10.0.1.10", {"10.0.1.10": "synthetic-server"}, "synthetic-server"),
])
def test_eip_current_binding_label_uses_private_address_not_public_address(pdf_client, private_ip, owners, expected):
    client, _ = pdf_client({"publicips": [publicip(private_ip_address=private_ip)]})
    record, = fetch_eip(client, {"ip_owner": owners})
    assert record.attrs["bound_to"] == expected
    assert record.ip == "198.51.100.10"


def test_real_client_preserves_documented_port_details_and_all_three_time_fields(pdf_client):
    # EIP pp44-46: these are declared fields, despite not all being displayed in the ledger.
    binding = {"name": "port", "network_id": "network-id", "device_id": "device-id",
               "device_owner": "compute:nova", "instance_type": "ECS", "instance_id": "instance-id"}
    payload = publicip(port_id="port-id", port_details=binding, created_at="2021-05-06T08:33:14",
                       updated_at="2021-05-07T08:33:14")
    client, _ = pdf_client({"publicips": [payload]})
    assert client.get("vpc", "/v1/{project_id}/publicips")["publicips"][0] == payload


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_eip_and_vpc_aksk_scope_is_project_scope_at_real_client_entry(pdf_client, code, fetcher, key):
    # VPC p22 and EIP p15 discuss X-Project-Id. This tests local signing, not deployed authorization.
    client, calls = pdf_client({key: []}, auth_kind="aksk")
    assert client.get("vpc", f"/v1/{{project_id}}/{key}") == {key: []}
    request, = calls
    assert request.headers["X-Project-Id"] == PROJECT
    assert "X-Domain-Id" not in request.headers and "X-Auth-Token" not in request.headers
    assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
    assert request.headers["X-Sdk-Date"]


@pytest.mark.parametrize("code,fetcher,key", LISTS)
def test_token_request_selects_project_scope_and_query_uses_x_auth_token(pdf_client, code, fetcher, key):
    import json

    # VPC pp23-24 and EIP pp16-17 require a project-scoped token for service requests.
    client, calls = pdf_client({key: []}, auth_kind="token")
    assert client.get("vpc", f"/v1/{{project_id}}/{key}") == {key: []}
    token_request, query_request = calls
    assert token_request.url.path == "/v3/auth/tokens"
    assert json.loads(token_request.content)["auth"]["scope"] == {"project": {"id": PROJECT}}
    assert query_request.headers["X-Auth-Token"] == "synthetic-pdf-token"
    assert query_request.url.path == f"/v1/{PROJECT}/{key}"
