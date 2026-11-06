"""用户上传 PDF 契约；页号是原 PDF 的物理页，不是手册页脚。

GaussDB 25.1.30 (2025-03-30)：§4.3.4 p82–107，§5.2.3 p865–886，
§5.2.4 p886–904，§5.2.6 p906–918。
DCS 2.3.0.2 (2025-06-13)：§4.1.2 p31–38，§4.2.6 p72–74，§6.5 p419。
所有响应均为合成数据；不创建 HCS 客户端，不查询租户或客户数据库。
"""
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector
from app.collectors.gaussdb import GaussDBCollector
from app.core.config import get_settings
from app.hcs import HcsError
from app.ledger.fetchers import fetch_dcs, fetch_gaussdb
from app.ledger.sync import apply_snapshot
from app.models import Resource

GAUSSDB_PATHS = [
    ("v3.3", "/gaussdb/v3.3/{project_id}/instances"),
    ("v3.2", "/gaussdb/v3.2/{project_id}/instances"),
    ("v3.1", "/gaussdb/v3.1/{project_id}/instances"),
    ("opengauss_v3", "/opengauss/v3/{project_id}/instances"),
]
DCS_INSTANCES = "/v2/{project_id}/instances"
DCS_STATISTICS = "/v2/{project_id}/instances/statistic"


class PdfResponses:
    """只允许预先列出的只读调用，禁止猜测路径、写请求和真实网络访问。"""

    def __init__(self, responses):
        self.responses = {key: deque(values) for key, values in responses.items()}
        self.calls = []

    def get(self, service, path, *, params=None):
        self.calls.append((service, path, deepcopy(params)))
        key = (service, path)
        assert key in self.responses and self.responses[key], f"unexpected GET: {key}"
        response = self.responses[key].popleft()
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


@pytest.fixture(autouse=True)
def native_only_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hcs_ces_enabled", False)
    monkeypatch.setattr(settings, "hcs_endpoints", {})
    monkeypatch.setattr(settings, "hcs_mock_url", "")
    monkeypatch.setattr(settings, "hcs_gaussdb_api_variant", "v3.3")


def context(client):
    return RunContext(client=client, account="pdf-contract", project_id="synthetic-project")


def gaussdb_instance(index=0, **changes):
    return {"id": f"gauss-{index}", "name": f"gauss-{index}", "status": "ACTIVE",
            "type": "centralization_standard", "datastore": {"type": "GaussDB", "version": "V2.0-8.203"},
            "flavor_info": {"vcpu": 8, "mem": 64}, "volume": {"type": "LOCALSSD", "size": 40},
            "created": "2024-07-02 08:32:07 UTC", "nodes": [], **changes}


def native_volumes(instances):
    # §4.2.1 正式 URI：磁盘查询使用 v3，不继承实例列表版本号，不用配置 volume.size 计算。
    return {("gaussdb", f"/gaussdb/v3/{{project_id}}/instances/{row['id']}/volume-usage"):
            [{"used": "15.0", "total": "185.0"}] for row in instances if row["status"] == "ACTIVE"}


def dcs_instance(index=0, **changes):
    return {"instance_id": f"dcs-{index}", "name": f"dcs-{index}", "status": "RUNNING",
            "engine": "Redis", "engine_version": "6.0", "capacity": 8,
            "created_at": "2017-03-31T12:24:46.297Z", **changes}


@pytest.mark.parametrize("variant,path", GAUSSDB_PATHS)
@pytest.mark.parametrize("consumer", ["collector", "ledger"])
def test_gaussdb_pdf_product_prefix_and_record_offsets(monkeypatch, variant, path, consumer):
    # 四个接口均明确为记录偏移，limit 最大 100；不使用 MySQL 或裸 /v3 路径。
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    instances = [gaussdb_instance(n) for n in range(3)]
    client = PdfResponses({("gaussdb", path): [
        {"instances": instances[:2], "total_count": 3},
        {"instances": instances[2:], "total_count": 3},
    ], **native_volumes(instances)})
    rows = GaussDBCollector().collect(context(client)) if consumer == "collector" else fetch_gaussdb(client, {})
    statuses = [row for row in rows if getattr(row, "check_item", "instance_status") == "instance_status"]
    assert [row.resource_id for row in statuses] == [item["id"] for item in instances]
    expected = [("gaussdb", path, {"offset": 0, "limit": 100}), ("gaussdb", path, {"offset": 2, "limit": 100})]
    if consumer == "collector":
        expected += [(service, route, None) for service, route in native_volumes(instances)]
    assert client.calls == expected


@pytest.mark.parametrize("variant,path", GAUSSDB_PATHS)
def test_gaussdb_pdf_no_version_or_product_fallback_after_404(monkeypatch, variant, path):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    client = PdfResponses({("gaussdb", path): [HcsError(404, "SYNTHETIC.NOT_FOUND", "unavailable")]})
    with pytest.raises(HcsError):
        GaussDBCollector().collect(context(client))
    assert client.calls == [("gaussdb", path, {"offset": 0, "limit": 100})]


@pytest.mark.parametrize(("error", "expected"), [
    (HcsError(0, "NETWORK", "private endpoint detail"), "NETWORK"),
    (HcsError(404, "APIGW.NOT_FOUND", "private endpoint detail"), "HTTP 404 / APIGW.NOT_FOUND"),
])
def test_gaussdb_volume_failure_has_safe_diagnostic(error, expected):
    item = gaussdb_instance()
    path = GAUSSDB_PATHS[0][1]
    volume_path = f"/gaussdb/v3/{{project_id}}/instances/{item['id']}/volume-usage"
    client = PdfResponses({("gaussdb", path): [{"instances": [item], "total_count": 1}],
                           ("gaussdb", volume_path): [error]})
    ctx = context(client)

    rows = GaussDBCollector().collect(ctx)

    assert len(rows) == 2 and rows[-1].storage_status == "ERROR"
    assert ctx.errors == [f"GaussDB 原生磁盘用量查询失败（{expected}）"]
    assert "private endpoint detail" not in ctx.errors[0]


@pytest.mark.parametrize("role", ["master", "slave", "offline", "secondary", "logger_dorado", "arbiter"])
def test_gaussdb_pdf_instance_nodes_preserve_roles_and_status(role):
    # §4.3.4 表4-59 p96–97：nodes 是节点列表，不是 /components 的嵌套结构。
    node = {"id": "node-1", "name": "node-1", "role": role, "status": "ACTIVE", "availability_zone": "az-1"}
    item = gaussdb_instance(nodes=[node])
    client = PdfResponses({("gaussdb", GAUSSDB_PATHS[0][1]): [
        {"instances": [item], "total_count": 1},
    ], **native_volumes([item])})
    row, disk = GaussDBCollector().collect(context(client))
    assert row.attrs["nodes"] == [{"name": "node-1", "role": role, "status": "ACTIVE"}]
    assert row.metrics == {"cpu_usage": None, "mem_usage": None, "disk_usage": None,
                           "disk_used_gb": None, "disk_total_gb": None}
    assert disk.metrics == {"disk_usage": 8.11, "disk_used_gb": 15, "disk_total_gb": 185}
    assert row.attrs["memory_size"] == 64 and row.attrs["volume_size"] == 40
    assert row.attrs["memory_unit_verified"] is False and row.attrs["volume_unit_verified"] is False
    assert len(client.calls) == 2


@pytest.mark.parametrize("variant,path", GAUSSDB_PATHS)
def test_gaussdb_pdf_created_utc_is_parsed_without_epoch_unit_guessing(monkeypatch, variant, path):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    client = PdfResponses({("gaussdb", path): [{"instances": [gaussdb_instance()], "total_count": 1}]})
    row, = fetch_gaussdb(client, {})
    assert row.created_time == datetime(2024, 7, 2, 8, 32, 7, tzinfo=UTC)


@pytest.mark.parametrize("status", ["BUILD", "BUILD_FAILED", "ACTIVE", "FAILED", "MODIFYING", "EXPANDING",
                                   "REBOOTING", "UPGRADING", "RESTORING", "SWITCHOVER", "MIGRATING", "BACKING UP",
                                   "REBALANCING", "UPGRADE TO BE OBSERVED", "MODIFYING DATABASE PORT",
                                   "STORAGE FULL", "REPAIRING", "SHUTDOWN"])
def test_gaussdb_pdf_instance_status_is_preserved_verbatim(status):
    item = gaussdb_instance(status=status)
    client = PdfResponses({("gaussdb", GAUSSDB_PATHS[0][1]): [
        {"instances": [item], "total_count": 1},
    ], **native_volumes([item])})
    row = GaussDBCollector().collect(context(client))[0]
    assert row.status_raw == status and row.facts == {"status": status}


@pytest.mark.parametrize("consumer", ["collector", "ledger"])
def test_dcs_pdf_v2_list_limit_and_offsets_with_reported_count(consumer):
    # PDF 的 instance_num 仅称“实例个数”；这里覆盖其为全量计数时的现有行为，不推断其他语义。
    instances = [dcs_instance(n, status="CREATING") for n in range(3)]
    client = PdfResponses({("dcs", DCS_INSTANCES): [
        {"instances": instances[:2], "instance_num": 3},
        {"instances": instances[2:], "instance_num": 3},
    ]})
    rows = DcsCollector().collect(context(client)) if consumer == "collector" else fetch_dcs(client, {})
    if consumer == "collector":
        rows = [row for row in rows if row.check_item == "instance_status"]
    assert [row.resource_id for row in rows] == [item["instance_id"] for item in instances]
    assert client.calls == [("dcs", DCS_INSTANCES, {"offset": 0, "limit": 1000}),
                            ("dcs", DCS_INSTANCES, {"offset": 2, "limit": 1000})]


def test_dcs_pdf_variable_length_fields_survive_ledger_storage(mem_db):
    # az_codes is an array, ip may contain comma-separated addresses, and the display spec combines API fields.
    az_codes = ["zone-a-" + "a" * 40, "zone-b-" + "b" * 40]
    addresses = ",".join(f"192.0.2.{index}" for index in range(1, 20))
    version = "6.0-" + "v" * 140
    item = dcs_instance(az_codes=az_codes, ip=addresses, engine_version=version)
    client = PdfResponses({("dcs", DCS_INSTANCES): [{"instances": [item], "instance_num": 1}]})

    record, = fetch_dcs(client, {})
    apply_snapshot(mem_db, "dcs_instance", [record], tenant_id=11, project_ref=111)
    stored = mem_db.scalar(select(Resource).where(Resource.resource_id == item["instance_id"]))

    assert stored.az == ",".join(az_codes)
    assert stored.ip == addresses
    assert stored.spec == f"Redis {version} | 8 GB"


@pytest.mark.parametrize("capacity,minor,expected", [(0, "0.125", 0.125), (None, "0.25", 0.25),
                                                   (8, "0.125", 8), (8, None, 8), (0, None, None)])
def test_dcs_pdf_sub_gb_capacity_agrees_between_collection_and_ledger(capacity, minor, expected):
    # §4.1.2 表4-17 p33：capacity 是整数，capacity_minor 承载小于1GB的规格。
    body = {"instances": [dcs_instance(status="CREATING", capacity=capacity, capacity_minor=minor)], "instance_num": 1}
    client = PdfResponses({("dcs", DCS_INSTANCES): [body, body]})
    observation, = [row for row in DcsCollector().collect(context(client)) if row.check_item == "instance_status"]
    record, = fetch_dcs(client, {})
    assert record.memory_gb == expected and record.attrs["capacity"] == expected
    assert observation.attrs["capacity"] == expected


def test_dcs_pdf_sub_gb_collection_does_not_overwrite_normalized_inventory(mem_db):
    from app.models import Resource
    from app.models.base import utcnow
    from app.services.pipeline import upsert_resource

    resource = Resource(service="dcs", resource_type="dcs_instance", resource_id="dcs-0", name="dcs-0",
                        memory_gb=0.125, attrs={"capacity": 0.125})
    mem_db.add(resource)
    mem_db.flush()
    client = PdfResponses({("dcs", DCS_INSTANCES): [
        {"instances": [dcs_instance(status="CREATING", capacity=0, capacity_minor="0.125")], "instance_num": 1},
    ]})
    ctx = context(client)
    rows = DcsCollector().collect(ctx)
    updated = upsert_resource(mem_db, "dcs", rows, utcnow(), ctx)
    assert updated.id == resource.id and updated.memory_gb == 0.125
    assert updated.attrs["capacity"] == 0.125


def test_dcs_pdf_statistics_memory_mb_and_cumulative_cpu_seconds():
    # §4.2.6 表4-81 p72–73：统计无分页；used_cpu 是累计秒数，不是百分比。
    client = PdfResponses({
        ("dcs", DCS_INSTANCES): [{"instances": [dcs_instance(used_memory=10, max_memory=100)], "instance_num": 1}],
        ("dcs", DCS_STATISTICS): [{"statistics": [{"instance_id": "dcs-0", "used_memory": 0, "max_memory": 460,
                                                  "used_cpu": "12000.56", "keys": 10, "cmd_get_count": 20}]}],
    })
    rows = DcsCollector().collect(context(client))
    status, memory = rows
    assert memory.check_item == "memory"
    assert memory.facts == {"memory_usage": 0.0, "used_memory_mb": 0.0, "max_memory_mb": 460.0}
    assert status.metrics["cpu_usage"] is None and status.metrics["connected_clients"] is None
    assert client.calls[-1] == ("dcs", DCS_STATISTICS, None)


@pytest.mark.parametrize("body", [None, [], "invalid-root", 1, {"statistics": None},
                                  {"statistics": [None]}, {"statistics": [{"instance_id": 1}]}])
def test_dcs_pdf_malformed_statistics_is_partial_and_keeps_list_snapshot(body):
    client = PdfResponses({
        ("dcs", DCS_INSTANCES): [{"instances": [dcs_instance(used_memory=2, max_memory=128)], "instance_num": 1}],
        ("dcs", DCS_STATISTICS): [body],
    })
    ctx = context(client)
    rows = DcsCollector().collect(ctx)
    assert rows[0].status_raw == "RUNNING" and ctx.errors
    assert rows[1].facts == {"memory_usage": 1.6, "used_memory_mb": 2, "max_memory_mb": 128}
    assert ctx.stats["native_statistics"] == 0
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", ["CREATING", "CREATEFAILED", "RUNNING", "ERROR", "RESTARTING", "EXTENDING", "RESTORING"])
def test_dcs_pdf_instance_status_is_preserved_without_node_status_conversion(status):
    # §6.5 p419；节点状态 Active/Inactive 属于 §4.3.2/3，不能代替实例状态。
    client = PdfResponses({
        ("dcs", DCS_INSTANCES): [{"instances": [dcs_instance(status=status)], "instance_num": 1}],
        ("dcs", DCS_STATISTICS): [{"statistics": []}],
    })
    row, = [item for item in DcsCollector().collect(context(client)) if item.check_item == "instance_status"]
    assert row.status_raw == status and row.facts == {"status": status}
    assert len(client.calls) == (2 if status == "RUNNING" else 1)


def test_dcs_pdf_iso_utc_created_time_and_capacity_units_are_independent_of_memory_mb():
    client = PdfResponses({("dcs", DCS_INSTANCES): [
        {"instances": [dcs_instance(used_memory=2, max_memory=128)], "instance_num": 1},
    ]})
    row, = fetch_dcs(client, {})
    assert row.created_time == datetime(2017, 3, 31, 12, 24, 46, 297000, tzinfo=UTC)
    assert row.memory_gb == 8


@pytest.mark.parametrize("status", [403, 404, 503])
def test_dcs_pdf_statistics_error_does_not_fall_back_to_v1_or_other_products(status):
    client = PdfResponses({
        ("dcs", DCS_INSTANCES): [{"instances": [dcs_instance(used_memory=2, max_memory=128)], "instance_num": 1}],
        ("dcs", DCS_STATISTICS): [HcsError(status, "SYNTHETIC.ERROR", "unavailable")],
    })
    ctx = context(client)
    rows = DcsCollector().collect(ctx)
    assert ctx.errors and len(rows) == 2
    assert [call[:2] for call in client.calls] == [("dcs", DCS_INSTANCES), ("dcs", DCS_STATISTICS)]
