"""8.5.1 官方结构契约：不导入 Mock HCS，不连接真实云或使用数据库 fixture。

来源与逐项核对见 docs/HCS8.5.1台账接口核对.md；标识和地址均为独立测试值。
"""
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.hcs import HcsError
from app.ledger import fetchers


class OfficialResponses:
    def __init__(self, responses):
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls = []

    def get(self, service, path, *, params=None, **kwargs):
        self.calls.append((service, path, dict(params or {})))
        pages = self.responses[(service, path)]
        assert pages, f"Unexpected extra query: {service} {path}"
        response = pages.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def dws_cluster(**changes):
    # EDOC1100475350, 2b0530af#ZH-CN_TOPIC_0000002270497945，表7-32/33。
    return {"id": "dws-contract", "name": "contract", "status": "AVAILABLE", "version": "9.1.0.218",
            "created": "2026-01-02T03:04:05Z", "port": 8000, "number_of_node": 3,
            "node_type": "dws.d1.xlarge.ultrahigh", "availability_zone": "az-test",
            "endpoints": [{"connect_info": "192.0.2.10:8000",
                           "jdbc_url": "jdbc:postgresql://192.0.2.10:8000/<YOUR_DATABASE_NAME>"}],
            "public_endpoints": [{"public_connect_info": "198.51.100.10:8000"}], **changes}


@pytest.mark.parametrize("include_count", [False, True])
def test_dws_851_private_connection_comes_from_endpoints_without_extra_requests(include_count):
    body = {"clusters": [dws_cluster()]}
    if include_count:
        body["count"] = 1
    client = OfficialResponses({("dws", "/v1.0/{project_id}/clusters"): [body]})
    record, = fetchers.fetch_dws(client, {})
    assert record.ip == "192.0.2.10"
    assert record.attrs["node_count"] == 3 and record.attrs["node_type"] == "dws.d1.xlarge.ultrahigh"
    assert record.vcpus is None and record.memory_gb is None and record.storage_gb is None
    assert client.calls == [("dws", "/v1.0/{project_id}/clusters", {})]


def test_dws_missing_private_endpoint_is_not_replaced_by_a_public_address():
    client = OfficialResponses({("dws", "/v1.0/{project_id}/clusters"): [{"clusters": [dws_cluster(endpoints=[])]}]})
    record, = fetchers.fetch_dws(client, {})
    assert record.ip is None


def test_dws_declared_incomplete_list_is_an_error_not_an_empty_or_partial_snapshot():
    client = OfficialResponses({("dws", "/v1.0/{project_id}/clusters"): [{"clusters": [dws_cluster()], "count": 2}]})
    with pytest.raises(HcsError, match="完整"):
        fetchers.fetch_dws(client, {})


def test_ecs_851_flavor_ram_is_mb_and_flavor_disk_is_not_real_attached_capacity():
    # ECS EDOC1100452338 ca0d1efd#EN-US_TOPIC_0000002124260517。
    server = {"id": "ecs-contract", "name": "contract", "status": "ACTIVE",
              "flavor": {"id": "flavor-contract", "vcpus": "4", "ram": "8192", "disk": "1"},
              "addresses": {"subnet-contract": [{"addr": "192.0.2.20", "OS-EXT-IPS:type": "fixed"}]},
              "metadata": {"vpc_id": "vpc-contract"}, "created": "2026-01-02T03:04:05Z"}
    volumes = [{"id": "system-volume", "size": 20, "attachments": [{"server_id": server["id"]}]},
               {"id": "data-volume", "size": 40, "attachments": [{"server_id": server["id"]}]}]
    client = OfficialResponses({("ecs", "/v1/{project_id}/cloudservers/detail"): [{"count": 1, "servers": [server]}],
                                ("evs", "/v2/{project_id}/cloudvolumes/detail"): [{"volumes": volumes}]})
    context = {}
    record, = fetchers.fetch_ecs(client, context)
    assert record.vcpus == 4 and record.memory_gb == 8 and record.storage_gb == 60
    assert record.ip == "192.0.2.20"
    assert client.calls[0] == ("ecs", "/v1/{project_id}/cloudservers/detail", {"offset": 1, "limit": 100})
    fetchers.fetch_evs(client, context)
    assert len(client.calls) == 2  # EVS 依赖完整缓存共享，不为每台 ECS / EVS 重复查询。


@pytest.mark.parametrize("link", ["https://unfollowed.example.invalid/next", {"href": "https://unfollowed.example.invalid/next",
                                                                           "rel": "next"}])
def test_evs_851_cloudvolumes_uses_marker_and_explicit_partial_links_without_count(link):
    # 普通 EVS 8.5.1 EDOC1100453160 cd60dae4#ZH-CN_TOPIC_0000002240926657。
    client = OfficialResponses({("evs", "/v2/{project_id}/cloudvolumes/detail"): [
        {"volumes": [{"id": "vol-one", "size": 20}], "volumes_links": [link]},
        {"volumes": [{"id": "vol-two", "size": 40}], "volumes_links": []}]})
    context = {}
    records = fetchers.fetch_evs(client, context)
    assert [record.resource_id for record in records] == ["vol-one", "vol-two"]
    assert [params for _, _, params in client.calls] == [{"limit": 100}, {"limit": 100, "marker": "vol-one"}]
    assert all(service == "evs" and path == "/v2/{project_id}/cloudvolumes/detail" for service, path, _ in client.calls)
    assert len(context["evs_raw"]) == 2


def test_evs_empty_partial_page_fails_without_caching_a_partial_inventory():
    client = OfficialResponses({("evs", "/v2/{project_id}/cloudvolumes/detail"): [
        {"volumes": [], "volumes_links": [{"rel": "next", "href": "https://unfollowed.example.invalid"}]}]})
    context = {}
    with pytest.raises(HcsError, match="完整"):
        fetchers.fetch_evs(client, context)
    assert "evs_raw" not in context


@pytest.mark.parametrize("value,expected", [(True, "是"), ("true", "是"), (False, "否"), ("false", "否"), (None, None)])
def test_evs_bootable_bool_string_variants_and_unsupported_encryption_are_not_false_claims(value, expected):
    client = OfficialResponses({("evs", "/v2/{project_id}/cloudvolumes/detail"): [
        {"volumes": [{"id": "vol-one", "size": 20, "bootable": value, "encrypted": False}]}]})
    record, = fetchers.fetch_evs(client, {})
    assert record.attrs["bootable"] == expected
    assert record.attrs["encrypted"] is None and record.attrs["encrypted_reported"] is False


def rds_instance(identity, **changes):
    # RDS EDOC1100458141 51d21a18#EN-US_TOPIC_0000002342919457；实例级，不乘节点数。
    return {"id": identity, "name": identity, "status": "ACTIVE", "cpu": "4", "mem": "16",
            "volume": {"type": "CLOUDSSD", "size": 40}, "datastore": {"type": "MySQL", "version": "8.0"},
            "nodes": [{"availability_zone": "az-1"}, {"availability_zone": "az-2"}],
            "private_ips": ["192.0.2.30"], "type": "Ha", "created": "2026-01-02T03:04:05+0000", **changes}


def test_rds_851_cpu_ram_and_volume_are_instance_level_units_and_offset_counts_records():
    client = OfficialResponses({("rds", "/v3/{project_id}/instances"): [
        {"instances": [rds_instance("rds-one")], "total_count": 3},
        {"instances": [rds_instance("rds-two"), rds_instance("rds-three")], "total_count": 3}]})
    records = fetchers.fetch_rds(client, {})
    assert len(records) == 3
    assert all(record.vcpus == 4 and record.memory_gb == 16 and record.storage_gb == 40 for record in records)
    assert [params for _, _, params in client.calls] == [{"offset": 0, "limit": 100}, {"offset": 1, "limit": 100}]


def kafka_instance(index):
    # DMS EDOC1100450009 bd0a2e6b#ListInstances：engine=kafka；storage_* 为 GB。
    return {"instance_id": f"kafka-{index}", "name": f"kafka-{index}", "status": "RUNNING",
            "storage_space": 2000, "used_storage_space": 500, "engine_version": "2.7",
            "available_zones": ["az-1"], "connect_address": "192.0.2.40:9092,192.0.2.41:9092",
            "created_at": "1767225600000", "partition_num": "1000", "specification": "contract-spec"}


def test_kafka_851_uses_maximum_50_record_offset_and_does_not_trust_instance_num_as_total():
    client = OfficialResponses({("dms", "/v2/{project_id}/instances"): [
        {"instances": [kafka_instance(i) for i in range(50)], "instance_num": 50},
        {"instances": [kafka_instance(i) for i in range(50, 52)], "instance_num": 2}]})
    records = fetchers.fetch_kafka(client, {})
    assert len(records) == 52 and len({record.resource_id for record in records}) == 52
    assert all(record.storage_gb == 2000 and record.attrs["storage_usage"] == 25 for record in records)
    assert all(record.ip == "192.0.2.40" for record in records)
    assert [params for _, _, params in client.calls] == [
        {"engine": "kafka", "offset": 0, "limit": 50}, {"engine": "kafka", "offset": 50, "limit": 50}]


def test_kafka_exact_full_page_requires_a_following_empty_page_instead_of_count_guessing():
    client = OfficialResponses({("dms", "/v2/{project_id}/instances"): [
        {"instances": [kafka_instance(i) for i in range(50)], "instance_num": 50},
        {"instances": [], "instance_num": 0}]})
    assert len(fetchers.fetch_kafka(client, {})) == 50 and len(client.calls) == 2


def test_kafka_later_page_failure_does_not_return_a_partial_inventory():
    client = OfficialResponses({("dms", "/v2/{project_id}/instances"): [
        {"instances": [kafka_instance(i) for i in range(50)], "instance_num": 50},
        HcsError(503, "DMS.UNAVAILABLE", "unavailable")]})
    with pytest.raises(HcsError):
        fetchers.fetch_kafka(client, {})


def gaussdb_instance(**changes):
    # EDOC1100457467 k001 §4.3.4，PDF 物理页91–107，表4-56/57没有内存/磁盘单位。
    return {"id": "gauss-contractin14", "name": "contract", "status": "ACTIVE", "type": "centralization_standard",
            "datastore": {"type": "GaussDB", "version": "V2.0-8.218"}, "created": "2026-01-02 03:04:05 UTC",
            "flavor_ref": "contract-flavor", "flavor_info": {"vcpu": 8, "mem": 64},
            "volume": {"type": "ULTRAHIGH", "size": 40}, "private_ips": ["192.0.2.50"],
            "nodes": [{"id": "node-1", "availability_zone": "az-1"},
                      {"id": "node-2", "availability_zone": "az-2"}], **changes}


def test_gaussdb_851_vcpu_and_node_zones_are_read_but_unknown_size_units_are_not_gb(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", "v3.3")
    client = OfficialResponses({("gaussdb", "/gaussdb/v3.3/{project_id}/instances"): [
        {"instances": [gaussdb_instance()], "total_count": 1}]})
    record, = fetchers.fetch_gaussdb(client, {})
    assert record.vcpus == 8 and record.az == "az-1,az-2" and record.ip == "192.0.2.50"
    assert record.memory_gb is None and record.storage_gb is None and " GB" not in record.spec
    assert record.attrs["memory_size"] == 64 and record.attrs["volume_size"] == 40
    assert record.attrs["volume_gb"] is None
    assert record.attrs["memory_unit_verified"] is False and record.attrs["volume_unit_verified"] is False
    assert client.calls == [("gaussdb", "/gaussdb/v3.3/{project_id}/instances", {"offset": 0, "limit": 100})]


@pytest.mark.parametrize("variant,expected", [
    ("v3.3", "/gaussdb/v3.3/{project_id}/instances"), ("v3.2", "/gaussdb/v3.2/{project_id}/instances"),
    ("v3.1", "/gaussdb/v3.1/{project_id}/instances"), ("opengauss_v3", "/opengauss/v3/{project_id}/instances"),
])
def test_gaussdb_uses_the_explicit_shared_variant_without_probing_other_paths(monkeypatch, variant, expected):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    client = OfficialResponses({("gaussdb", expected): [{"instances": [], "total_count": 0}]})
    assert fetchers.fetch_gaussdb(client, {}) == []
    assert client.calls == [("gaussdb", expected, {"offset": 0, "limit": 100})]


def elb_instance(**changes):
    # ELB EDOC1100452378 bd0a2e6b#ZH-CN_TOPIC_0000002112112189。
    return {"id": "elb-contract", "name": "contract", "operating_status": "ONLINE", "provisioning_status": "ACTIVE",
            "vip_address": "192.0.2.60", "vpc_id": "vpc-contract", "created_at": "2026-01-02T03:04:05",
            "guaranteed": True, "listeners": [], "availability_zone_list": [],
            "eips": [{"eip_address": "198.51.100.60"}], **changes}


@pytest.mark.parametrize("page_info", [None, {"current_count": 1, "previous_marker": "", "next_marker": "elb-contract"}])
def test_elb_native_v3_uses_no_undocumented_marker_request_and_reserved_flag_is_not_physical_type(page_info):
    body = {"loadbalancers": [elb_instance()]}
    if page_info is not None:
        body["page_info"] = page_info
    client = OfficialResponses({("elb", "/v3/{project_id}/elb/loadbalancers"): [body]})
    record, = fetchers.fetch_elb(client, {})
    assert client.calls == [("elb", "/v3/{project_id}/elb/loadbalancers", {})]
    assert record.ip == "192.0.2.60" and record.attrs["eip"] == "198.51.100.60"
    assert record.spec is None and record.attrs["guaranteed"] is True
    assert record.attrs["listeners"] == 0 and record.attrs["members"] is None


def test_elb_contradictory_current_count_does_not_return_an_inventory():
    client = OfficialResponses({("elb", "/v3/{project_id}/elb/loadbalancers"): [
        {"loadbalancers": [elb_instance()], "page_info": {"current_count": 2}}]})
    with pytest.raises(HcsError, match="完整"):
        fetchers.fetch_elb(client, {})


def test_vpc_subnet_851_uses_native_id_not_neutron_id_and_reuses_the_dependency_list():
    # VPC EDOC1100452400 e654702c#EN-US_TOPIC_0000002076345310 / 2076500050。
    client = OfficialResponses({("vpc", "/v1/{project_id}/vpcs"): [
        {"vpcs": [{"id": "vpc-one", "name": "contract-vpc", "cidr": "192.0.2.0/24", "status": "OK"}]}],
        ("vpc", "/v1/{project_id}/subnets"): [{"subnets": [
            {"id": "native-subnet", "neutron_subnet_id": "different-neutron-subnet", "vpc_id": "vpc-one",
             "name": "contract-subnet", "status": "ACTIVE", "cidr": "192.0.2.0/25", "gateway_ip": "192.0.2.1",
             "availability_zone": "az-1", "dhcp_enable": True}]}]})
    context = {}
    vpc, = fetchers.fetch_vpc(client, context)
    subnet, = fetchers.fetch_subnet(client, context)
    assert vpc.attrs["subnet_count"] == 1 and subnet.resource_id == "native-subnet"
    assert subnet.attrs["vpc_name"] == "contract-vpc" and subnet.attrs["dhcp"] is True
    assert all(params == {"limit": 100} for _, _, params in client.calls) and len(client.calls) == 2


def test_eip_native_v1_bandwidth_is_mbit_per_second_without_neutron_field_substitution():
    # EIP EDOC1100452355 ca0d1efd#ZH-CN_TOPIC_0000002112123313；带宽章节单位为 Mbit/s。
    client = OfficialResponses({("vpc", "/v1/{project_id}/publicips"): [{"publicips": [
        {"id": "eip-one", "public_ip_address": "198.51.100.30", "private_ip_address": "192.0.2.30",
         "status": "ACTIVE", "bandwidth_size": 100, "bandwidth_share_type": "WHOLE", "bandwidth_name": "shared",
         "create_time": "2026-01-02 03:04:05"}]}]})
    record, = fetchers.fetch_eip(client, {"ip_owner": {"192.0.2.30": "server-contract"}})
    assert record.ip == "198.51.100.30" and record.spec == "100 Mbit/s"
    assert record.attrs["share_type"] == "共享" and record.attrs["bound_to"] == "server-contract"
    assert client.calls == [("vpc", "/v1/{project_id}/publicips", {"limit": 100})]


@pytest.mark.parametrize("capacity,minor,expected", [(0, "0.125", 0.125), (None, "0.25", 0.25), (8, None, 8), (0, None, None)])
def test_dcs_851_capacity_minor_is_gb_and_sub_gb_instances_are_not_zero_capacity(capacity, minor, expected):
    # DCS EDOC1100450216 f0c9651f#ListInstances，capacity_minor String，单位 GB。
    client = OfficialResponses({("dcs", "/v2/{project_id}/instances"): [{"instance_num": 1, "instances": [
        {"instance_id": "dcs-one", "name": "contract", "engine": "Redis", "engine_version": "6.0",
         "capacity": capacity, "capacity_minor": minor, "status": "RUNNING"}]}]})
    record, = fetchers.fetch_dcs(client, {})
    assert record.memory_gb == expected and record.attrs["capacity"] == expected
    assert client.calls == [("dcs", "/v2/{project_id}/instances", {"offset": 0, "limit": 1000})]


@pytest.mark.parametrize("internal,host", [("https://192.0.2.70:5443", "192.0.2.70"),
                                          ("https://[2001:db8::70]:5443", "2001:db8::70")])
def test_cce_851_cluster_and_node_lists_use_exact_uids_and_parse_the_internal_url(internal, host):
    # CCE EDOC1100453109 daeb8233#cce_02_0239；端点为 URL，不是按冒号切片的 IPv4 字段。
    cluster = {"metadata": {"uid": "cluster-native", "name": "contract", "creationTimestamp": "2026-01-02T03:04:05Z"},
               "spec": {"version": "v1.30.0", "flavor": "cce.s2.small", "type": "VirtualMachine"},
               "status": {"phase": "Available", "endpoints": [
                   {"type": "External", "url": "https://198.51.100.70:5443"}, {"type": "Internal", "url": internal}]}}
    client = OfficialResponses({("cce", "/api/v3/projects/{project_id}/clusters"): [{"items": [cluster]}],
        ("cce", "/api/v3/projects/{project_id}/clusters/cluster-native/nodes"): [{"items": [
            {"metadata": {"uid": "node-a"}}, {"metadata": {"uid": "node-b"}}]}]})
    record, = fetchers.fetch_cce(client, {})
    assert record.resource_id == "cluster-native" and record.ip == host and record.attrs["node_count"] == 2
    assert record.vcpus is None and record.memory_gb is None  # 不把 flavor 的最大可管理节点数当当前资源量。
    assert all(params == {} for _, _, params in client.calls)


def test_mrs_851_uses_page_numbers_string_total_and_ten_digit_creation_time():
    # MRS EDOC1100452559 1c9c9c61#EN-US_TOPIC_0000002301999772。
    def cluster(index):
        return {"clusterId": f"mrs-{index}", "clusterName": f"contract-{index}", "clusterState": "running",
                "clusterVersion": "MRS 3.5.1-LTS", "azName": "az-1", "masterNodeIp": "192.0.2.80",
                "totalNodeNum": "5", "createAt": "1498272043", "componentList": [{"componentName": "Hadoop"}]}

    client = OfficialResponses({("mrs", "/v1.1/{project_id}/cluster_infos"): [
        {"clusters": [cluster(i) for i in range(50)], "clusterTotal": "51"},
        {"clusters": [cluster(50)], "clusterTotal": "51"}]})
    records = fetchers.fetch_mrs(client, {})
    assert len(records) == 51 and records[0].ip == "192.0.2.80"
    assert records[0].created_time == datetime.fromtimestamp(1498272043, UTC)
    assert records[0].attrs["nodes"] == "5" and records[0].attrs["components"] == "Hadoop"
    assert [params for _, _, params in client.calls] == [{"currentPage": 1, "pageSize": 50}, {"currentPage": 2, "pageSize": 50}]


@pytest.mark.parametrize("resource_type", list(fetchers.FETCHERS))
@pytest.mark.parametrize("status", [403, 503])
def test_fetcher_auth_and_service_errors_are_never_converted_to_an_empty_inventory(resource_type, status):
    error = HcsError(status, "CONTRACT.FAILURE", "upstream unavailable")

    class Client:
        def get(self, *args, **kwargs):
            raise error

    with pytest.raises(HcsError) as caught:
        fetchers.FETCHERS[resource_type](Client(), {})
    assert caught.value is error


@pytest.mark.parametrize("resource_type,key", [
    ("ecs_server", "servers"), ("cce_cluster", "items"), ("evs_volume", "volumes"), ("vpc", "vpcs"),
    ("vpc_subnet", "subnets"), ("elb", "loadbalancers"), ("eip", "publicips"), ("rds_instance", "instances"),
    ("gaussdb_instance", "instances"), ("dcs_instance", "instances"), ("kafka_instance", "instances"),
    ("mrs_cluster", "clusters"), ("dws_cluster", "clusters"), ("css_cluster", "clusters"),
])
def test_malformed_list_is_not_treated_as_zero_resources(resource_type, key):
    class Client:
        def get(self, *args, **kwargs):
            return {key: {"unexpected": "object"}}

    with pytest.raises(HcsError) as caught:
        fetchers.FETCHERS[resource_type](Client(), {})
    assert caught.value.code == "LOCAL.INVALID_RESPONSE"
