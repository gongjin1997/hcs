"""本地文档审计回归；合成响应只验证实现，不代表生产已联调。

EIP 8.5.1/01 正文第39、71页：bandwidth_share_type 的 PER/WHOLE 才能
解释为独享/共享；字段缺失不能默认为 PER。
"""
from copy import deepcopy

import pytest

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.collectors.mrs import MrsJobCollector
from app.hcs import HcsError
from app.ledger import fetchers
from app.ledger.fetchers import fetch_eip, fetch_kafka, fetch_rds
from app.ledger.sync import apply_snapshot
from app.models import Resource


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, service, path, **kwargs):
        self.calls.append((service, path, deepcopy(kwargs)))
        assert self.responses, "Unexpected API request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("NEW_VARIANT", None), ([], None),
                                          ("PER", "独享"), ("WHOLE", "共享")])
def test_eip_unknown_or_missing_bandwidth_share_type_is_not_dedicated(value, expected):
    publicip = {"id": "eip-test", "public_ip_address": "192.0.2.10"}
    if value is not None:
        publicip["bandwidth_share_type"] = value
    client = Client([{"publicips": [publicip]}])
    record, = fetch_eip(client, {})
    assert record.attrs["share_type"] == expected
    assert "share_type" in record.clear_attrs
    assert client.calls == [("vpc", "/v1/{project_id}/publicips", {"params": {"limit": 100}})]


@pytest.mark.parametrize("connection,expected", [
    ("192.0.2.10,192.0.2.11", "192.0.2.10"),
    ("192.0.2.10:9092,192.0.2.11:9092", "192.0.2.10"),
    ("2001:db8::10,2001:db8::11", "2001:db8::10"),
    ("[2001:db8::10]:9092,[2001:db8::11]:9092", "2001:db8::10"),
    ("[2001:db8::10]", "2001:db8::10"),
    (" 192.0.2.10 ,192.0.2.11", "192.0.2.10"),
    (None, None), ("", None),
])
def test_kafka_connection_ip_is_not_truncated_at_an_ipv6_colon(connection, expected):
    # 已有 HCS DMS 证据：connect_address 为连接 IP，port 为独立字段；不发明新接口。
    client = Client([{"instances": [{"instance_id": "kafka-test", "name": "kafka-test",
                                     "connect_address": connection, "port": 9092}]}])
    record, = fetch_kafka(client, {})
    assert record.ip == expected
    assert client.calls == [("dms", "/v2/{project_id}/instances",
                             {"params": {"engine": "kafka", "offset": 0, "limit": 50}})]


@pytest.mark.parametrize("cpu", ["4.5", "-4", "0", "NaN", "Infinity", True, None])
def test_rds_invalid_vcpu_count_is_not_rounded_to_a_different_flavor(cpu):
    client = Client([{"instances": [{"id": "rds-test", "name": "rds-test", "cpu": cpu,
                                     "mem": "16", "volume": {"size": 40}}], "total_count": 1}])
    record, = fetch_rds(client, {})
    assert record.vcpus is None
    assert record.memory_gb == 16 and record.storage_gb == 40
    assert record.spec == "- vCPU | 16 GB | 40 GB"


@pytest.mark.parametrize("cpu", ["4", "4.0", 4])
def test_rds_valid_vcpu_and_gb_units_remain_unchanged(cpu):
    client = Client([{"instances": [{"id": "rds-test", "name": "rds-test", "cpu": cpu,
                                     "mem": "16", "volume": {"size": 40}}], "total_count": 1}])
    record, = fetch_rds(client, {})
    assert record.vcpus == 4 and record.memory_gb == 16 and record.storage_gb == 40
    assert record.spec == "4 vCPU | 16 GB | 40 GB" and not record.clear_fields


@pytest.mark.parametrize("connection", [[], {}, "[broken", "192.0.2.10:invalid"])
def test_invalid_kafka_connection_is_a_contract_error_not_an_arbitrary_host(connection):
    client = Client([{"instances": [{"instance_id": "kafka-test", "name": "kafka-test",
                                     "connect_address": connection}]}])
    with pytest.raises(HcsError) as error:
        fetch_kafka(client, {})
    assert error.value.code == "LOCAL.INVALID_RESPONSE" and len(client.calls) == 1


@pytest.mark.parametrize("resource_type,service,path", [
    ("ecs_server", "ecs", "/v1/{project_id}/cloudservers/detail"),
    ("cce_cluster", "cce", "/api/v3/projects/{project_id}/clusters"),
    ("evs_volume", "evs", "/v2/{project_id}/cloudvolumes/detail"),
    ("vpc", "vpc", "/v1/{project_id}/vpcs"),
    ("vpc_subnet", "vpc", "/v1/{project_id}/subnets"),
    ("elb", "elb", "/v3/{project_id}/elb/loadbalancers"),
    ("eip", "vpc", "/v1/{project_id}/publicips"),
    ("rds_instance", "rds", "/v3/{project_id}/instances"),
    ("kafka_instance", "dms", "/v2/{project_id}/instances"),
    ("mrs_cluster", "mrs", "/v1.1/{project_id}/cluster_infos"),
    ("dws_cluster", "dws", "/v1.0/{project_id}/clusters"),
    ("css_cluster", "css", "/v1.0/{project_id}/clusters"),
])
@pytest.mark.parametrize("status", [403, 404])
def test_native_inventory_failure_never_probes_other_services_or_versions(resource_type, service, path, status):
    # CSS 路径仅记录当前兼容实现，不以此断言上传 PDF 定义了 CSS 契约。
    failure = HcsError(status, "SYNTHETIC.ERROR", "synthetic rejection")
    client = Client([failure])
    with pytest.raises(HcsError) as caught:
        fetchers.FETCHERS[resource_type](client, {})
    assert caught.value is failure
    assert len(client.calls) == 1 and client.calls[0][:2] == (service, path)


@pytest.mark.parametrize("collector,path", [
    (DataArtsRealtimeCollector, "/v1/{project_id}/jobs"),
    (DataArtsBatchCollector, "/v1/{project_id}/jobs/instances/detail"),
])
def test_dataarts_list_failure_keeps_workspace_error_and_does_not_fallback(collector, path):
    client = Client([HcsError(404, "SYNTHETIC.ERROR", "synthetic rejection")])
    context = RunContext(client=client, account="synthetic", project_id="synthetic-project",
                         workspaces=["workspace-test"])
    assert collector().collect(context) == [] and len(context.errors) == 1
    assert client.calls[0][:2] == ("dataarts", path) and len(client.calls) == 1
    assert client.calls[0][2]["headers"] == {"workspace": "workspace-test"}


def test_mrs_unsupported_compatibility_job_api_does_not_become_an_empty_success():
    client = Client([{"clusters": [{"clusterId": "mrs-test", "clusterName": "mrs-test"}], "clusterTotal": "1"},
                     HcsError(404, "SYNTHETIC.ERROR", "synthetic unsupported compatibility API")])
    context = RunContext(client=client, account="synthetic", project_id="synthetic-project")
    assert MrsJobCollector().collect(context) == [] and len(context.errors) == 1
    assert context.stats["job_api_contract"] == "unverified_hcs851_compatibility"
    assert [call[:2] for call in client.calls] == [
        ("mrs", "/v1.1/{project_id}/cluster_infos"),
        ("mrs", "/v2/{project_id}/clusters/mrs-test/job-executions")]


@pytest.mark.parametrize("resource_type", ["eip", "rds_instance"])
def test_confirmed_invalid_old_fields_are_cleared_only_in_the_selected_project(mem_db, resource_type):
    if resource_type == "eip":
        client = Client([{"publicips": [{"id": "test", "public_ip_address": "192.0.2.10"}]}])
    else:
        client = Client([{"instances": [{"id": "test", "name": "test", "cpu": "4.5"}], "total_count": 1}])
    record, = fetchers.FETCHERS[resource_type](client, {})
    current = Resource(service="synthetic", resource_type=resource_type, resource_id="test", name="test",
                       tenant_id=1, project_ref=11, vcpus=4, attrs={"share_type": "独享", "manual": "keep"})
    other = Resource(service="synthetic", resource_type=resource_type, resource_id="test", name="other-project",
                     tenant_id=2, project_ref=22, vcpus=4, attrs={"share_type": "独享"})
    mem_db.add_all([current, other])
    mem_db.commit()
    stats = apply_snapshot(mem_db, resource_type, [record], tenant_id=1, project_ref=11)
    assert stats["updated"] == 1 and stats["deleted"] == 0
    if resource_type == "eip":
        assert current.attrs["share_type"] is None
    else:
        assert current.vcpus is None
    assert current.attrs["manual"] == "keep"
    assert other.vcpus == 4 and other.attrs == {"share_type": "独享"}
    assert apply_snapshot(mem_db, resource_type, [record], tenant_id=1, project_ref=11)["updated"] == 0
