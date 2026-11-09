"""本地文档审计回归；合成响应只验证实现，不代表生产已联调。

EIP 8.5.1/01 正文第39、71页：bandwidth_share_type 的 PER/WHOLE 才能
解释为独享/共享；字段缺失不能默认为 PER。
"""
from copy import deepcopy

import pytest

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.collectors.mrs import DataArtsMrsNodeCollector
from app.hcs import HcsError
from app.ledger import fetchers
from app.ledger.fetchers import fetch_eip
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


@pytest.mark.parametrize("resource_type,service,path", [
    ("ecs_server", "ecs", "/v1/{project_id}/cloudservers/detail"),
    ("cce_cluster", "cce", "/api/v3/projects/{project_id}/clusters"),
    ("evs_volume", "evs", "/v2/{project_id}/cloudvolumes/detail"),
    ("vpc", "vpc", "/v1/{project_id}/vpcs"),
    ("vpc_subnet", "vpc", "/v1/{project_id}/subnets"),
    ("elb", "elb", "/v3/{project_id}/elb/loadbalancers"),
    ("eip", "vpc", "/v1/{project_id}/publicips"),
    ("mrs_cluster", "mrs", "/v1.1/{project_id}/cluster_infos"),
    ("dws_cluster", "dws", "/v1.0/{project_id}/clusters"),
])
@pytest.mark.parametrize("status", [403, 404])
def test_native_inventory_failure_never_probes_other_services_or_versions(resource_type, service, path, status):
    failure = HcsError(status, "SYNTHETIC.ERROR", "synthetic rejection")
    client = Client([failure])
    with pytest.raises(HcsError) as caught:
        fetchers.FETCHERS[resource_type](client, {})
    assert caught.value is failure
    assert len(client.calls) == 1 and client.calls[0][:2] == (service, path)


@pytest.mark.parametrize("collector,path", [
    (DataArtsRealtimeCollector, "/v1/{project_id}/jobs"),
    (DataArtsBatchCollector, "/v1/{project_id}/jobs"),
])
def test_dataarts_list_failure_keeps_workspace_error_and_does_not_fallback(collector, path):
    client = Client([HcsError(404, "SYNTHETIC.ERROR", "synthetic rejection")])
    context = RunContext(client=client, account="synthetic", project_id="synthetic-project",
                         workspaces=["workspace-test"])
    assert collector().collect(context) == [] and len(context.errors) == 1
    assert client.calls[0][:2] == ("dataarts", path) and len(client.calls) == 1
    assert client.calls[0][2]["headers"] == {"workspace": "workspace-test"}


def test_confirmed_invalid_old_fields_are_cleared_only_in_the_selected_project(mem_db):
    resource_type = "eip"
    client = Client([{"publicips": [{"id": "test", "public_ip_address": "192.0.2.10"}]}])
    record, = fetchers.FETCHERS[resource_type](client, {})
    current = Resource(service="synthetic", resource_type=resource_type, resource_id="test", name="test",
                       tenant_id=1, project_ref=11, vcpus=4, attrs={"share_type": "独享", "manual": "keep"})
    other = Resource(service="synthetic", resource_type=resource_type, resource_id="test", name="other-project",
                     tenant_id=2, project_ref=22, vcpus=4, attrs={"share_type": "独享"})
    mem_db.add_all([current, other])
    mem_db.commit()
    stats = apply_snapshot(mem_db, resource_type, [record], tenant_id=1, project_ref=11)
    assert stats["updated"] == 1 and stats["deleted"] == 0
    assert current.attrs["share_type"] is None
    assert current.attrs["manual"] == "keep"
    assert other.vcpus == 4 and other.attrs == {"share_type": "独享"}
    assert apply_snapshot(mem_db, resource_type, [record], tenant_id=1, project_ref=11)["updated"] == 0


def test_mrs_job_dataarts_instance_list_failure_is_a_workspace_error_not_an_empty_success():
    client = Client([HcsError(404, "SYNTHETIC.ERROR", "synthetic unsupported API")])
    context = RunContext(client=client, account="synthetic", project_id="synthetic-project",
                         workspaces=["workspace-test"])
    assert DataArtsMrsNodeCollector().collect(context) == [] and len(context.errors) == 1
    # DataArts MRS 节点采集与 MRS 原生执行独立
    assert [call[:2] for call in client.calls] == [("dataarts", "/v1/{project_id}/jobs/instances/detail")]
