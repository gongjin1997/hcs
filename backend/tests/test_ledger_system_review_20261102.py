"""资源台账专项复核：合成输入验证本地缺陷，PDF 契约证据另见专项审计文档。

不访问云/通知服务、不以本测试证明真实 API 可用；所有数据库均为 mem_db。
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError

from app.hcs import HcsError
from app.hcs.pager import response_items
from app.ledger import fetchers, sync
from app.models import Resource, ResourceChange
from app.models.base import utcnow
from app.services.tenancy import Target

TARGET = Target(101, "review", "review", "review", 1001, "project-a", "review", "region")
TYPES = tuple(fetchers.FETCHERS)
LIST_KEYS = dict(zip(TYPES, ("servers", "items", "volumes", "vpcs", "subnets", "loadbalancers", "publicips",
                           "instances", "instances", "clusters", "clusters"),
                     strict=True))


class ReviewClient:
    project_id = "project-a"
    tenant_id = 101
    project_ref = 1001

    def __init__(self, body):
        self.body = deepcopy(body)
        self.calls = 0
        self.requests = []

    def get(self, service, path, **kwargs):
        self.calls += 1
        self.requests.append((service, path, kwargs.get("params", {})))
        if service == "evs" and "servers" in self.body:
            return {"volumes": []}
        if service == "cce" and path.endswith("/nodes"):
            return {"items": []}
        if service == "vpc" and path.endswith("/subnets") and "vpcs" in self.body:
            return {"subnets": []}
        return deepcopy(self.body)

    def configure_qps(self, _qps):
        pass

    def metrics_snapshot(self):
        return {"calls": self.calls}


def inventory(rtype, **changes):
    item = {"id": "one", "name": "one", **changes}
    if rtype == "cce_cluster":
        item = {"metadata": {"uid": "one", "name": "one"}, "spec": {}, **changes}
    elif rtype == "dcs_instance":
        item["instance_id"] = "one"
    elif rtype == "eip":
        item["public_ip_address"] = "198.51.100.10"
    elif rtype == "mrs_cluster":
        item.update(clusterId="one", clusterName="one")
    return {LIST_KEYS[rtype]: [item], **({"count": 1} if rtype == "ecs_server" else {}),
            **({"clusterTotal": "1"} if rtype == "mrs_cluster" else {})}


@pytest.mark.parametrize("rtype", TYPES)
def test_missing_native_status_clears_previous_status_with_change_evidence(mem_db, rtype):
    row = Resource(service="review", resource_type=rtype, resource_id="one", name="one", status_raw="ACTIVE",
                   owner="manual-owner", tenant_id=101, project_ref=1001)
    other = Resource(service="review", resource_type=rtype, resource_id="one", name="other", status_raw="ACTIVE",
                     tenant_id=202, project_ref=2002)
    mem_db.add_all([row, other])
    mem_db.commit()
    record, = fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype)), {})
    now = utcnow()
    result = sync.apply_snapshot(mem_db, rtype, [record], now, tenant_id=101, project_ref=1001)
    assert row.status_raw is None
    assert row.status_since == now and row.owner == "manual-owner"
    assert other.status_raw == "ACTIVE" and other.lifecycle == "ACTIVE"
    assert result["updated"] == 1
    changes = mem_db.scalars(select(ResourceChange).where(ResourceChange.resource_ref == row.id)).all()
    assert any(c["field"] == "status_raw" and c["old"] == "ACTIVE" and c["new"] is None
               for event in changes for c in event.changes)
    assert sync.apply_snapshot(mem_db, rtype, [record], tenant_id=101, project_ref=1001)["updated"] == 0


@pytest.mark.parametrize("rtype", TYPES)
@pytest.mark.parametrize("envelope", [{"error": {"code": "SYNTHETIC.FAILURE"}},
                                     {"error_code": "SYNTHETIC.FAILURE", "error_msg": "private-do-not-log"}])
def test_error_envelope_with_empty_list_never_releases_existing_inventory(mem_db, rtype, envelope):
    row = Resource(service="review", resource_type=rtype, resource_id="retained", name="retained",
                   tenant_id=101, project_ref=1001)
    mem_db.add(row)
    mem_db.commit()
    body = {LIST_KEYS[rtype]: [], "count": 0, "clusterTotal": "0", **envelope}
    run = sync.sync_ledger(mem_db, client=ReviewClient(body), target=TARGET, types=[rtype], trigger="MANUAL")
    mem_db.refresh(row)
    assert run.status == "FAILED" and row.lifecycle == "ACTIVE"
    assert "private-do-not-log" not in (run.error_summary or "")
    assert not mem_db.scalars(select(ResourceChange)).all()


def test_database_data_error_reports_safe_column_diagnostic(mem_db, monkeypatch):
    original = SimpleNamespace(sqlstate="22001", diag=SimpleNamespace(
        table_name="resource", column_name="spec",
        message_primary="value too long for type character varying(128)"))
    failure = DataError("INSERT", {"spec": "private-value"}, original)

    def fail(_client, _context):
        raise failure

    monkeypatch.setitem(sync.FETCHERS, "dcs_instance", fail)
    run = sync.sync_ledger(mem_db, client=ReviewClient({}), target=TARGET, types=["dcs_instance"], trigger="MANUAL")

    assert run.status == "FAILED"
    assert "table_name=resource" in run.error_summary
    assert "column_name=spec" in run.error_summary
    assert "sqlstate=22001" in run.error_summary
    assert "db_column_limit=128" in run.error_summary
    assert "private-value" not in run.error_summary


@pytest.mark.parametrize("rtype,field", [("ecs_server", "tenant_id"), ("evs_volume", "os-vol-tenant-attr:tenant_id"),
    ("vpc", "project_id"), ("vpc_subnet", "project_id"), ("elb", "tenant_id"),
    ("eip", "tenant_id"), ("eip", "project_id"), ("mrs_cluster", "tenantId")])
def test_explicit_foreign_project_response_fails_before_snapshot_and_context(rtype, field):
    context = {}
    with pytest.raises(HcsError) as error:
        fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **{field: "project-b"})), context)
    assert error.value.code == "LOCAL.PROJECT_MISMATCH"
    assert context == {}


@pytest.mark.parametrize("field,value", [("project_id", "project-b"), ("tenant_id", 202), ("project_ref", 2002)])
def test_client_cannot_apply_another_target_inventory(mem_db, field, value):
    client = ReviewClient(inventory("dcs_instance"))
    setattr(client, field, value)
    run = sync.sync_ledger(mem_db, client=client, target=TARGET, types=["dcs_instance"], trigger="MANUAL")
    assert run.status == "FAILED" and client.calls == 0
    assert not mem_db.scalars(select(Resource)).all()


def test_evs_duplicate_tail_volume_cannot_inflate_ecs_storage_or_enter_cache():
    pages = iter([{"volumes": [{"id": "disk", "size": 40, "attachments": [{"server_id": "one"}]}],
                   "volumes_links": ["next"]},
                  {"volumes": [{"id": "disk", "size": 40, "attachments": [{"server_id": "one"}]}],
                   "volumes_links": []}])
    client = ReviewClient(inventory("ecs_server"))
    get = client.get
    client.get = lambda service, path, **kw: next(pages) if service == "evs" else get(service, path, **kw)
    context = {}
    with pytest.raises(HcsError):
        fetchers.fetch_ecs(client, context)
    assert "evs_raw" not in context


def test_evs_multiattach_counts_one_volume_once_per_server():
    body = {"volumes": [{"id": "disk", "size": 40, "attachments": [
        {"server_id": "one", "attachment_id": "a"}, {"server_id": "one", "attachment_id": "b"}]}]}
    client = ReviewClient(inventory("ecs_server"))
    get = client.get
    client.get = lambda service, path, **kw: body if service == "evs" else get(service, path, **kw)
    record, = fetchers.fetch_ecs(client, {})
    assert record.storage_gb == 40


@pytest.mark.parametrize("value", [-1, "-1", "NaN", "Infinity", float("inf"), True])
@pytest.mark.parametrize("rtype,changes,field", [
    ("evs_volume", {"size": None}, "storage_gb"),
    ("dcs_instance", {"capacity": None}, "memory_gb"),
])
def test_invalid_native_capacity_does_not_keep_known_capacity(mem_db, rtype, changes, field, value):
    changes = deepcopy(changes)
    key = next(iter(changes))
    if key == "volume":
        changes[key]["size"] = value
    else:
        changes[key] = value
    record, = fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **changes)), {})
    assert getattr(record, field) is None
    row = Resource(service="review", resource_type=rtype, resource_id="one", name="one",
                   tenant_id=101, project_ref=1001, **{field: 40})
    mem_db.add(row)
    mem_db.commit()
    sync.apply_snapshot(mem_db, rtype, [record], tenant_id=101, project_ref=1001)
    assert getattr(row, field) is None


@pytest.mark.parametrize("value,expected", [(0, 0), ("0", 0), (100, 100), (None, None),
                                         (-1, None), ("NaN", None), ("Infinity", None), (True, None)])
def test_eip_bandwidth_is_finite_nonnegative_and_zero_is_retained(value, expected):
    record, = fetchers.fetch_eip(ReviewClient(inventory("eip", bandwidth_size=value)), {})
    assert record.attrs["bandwidth"] == expected
    assert record.spec == (None if expected is None else f"{expected} Mbit/s")


def test_elb_missing_operating_state_is_not_provisioning_active():
    record, = fetchers.fetch_elb(ReviewClient(inventory("elb", provisioning_status="ACTIVE")), {})
    assert record.status is None
    assert record.attrs["provisioning_status"] == "ACTIVE"


@pytest.mark.parametrize("rtype", TYPES)
def test_unknown_native_status_is_preserved_verbatim(rtype):
    field = {"elb": "operating_status", "mrs_cluster": "clusterState"}.get(rtype, "status")
    change = {field: "FUTURE_STATE"} if rtype != "cce_cluster" else {"status": {"phase": "FUTURE_STATE"}}
    record, = fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **change)), {})
    assert record.status == "FUTURE_STATE"


@pytest.mark.parametrize("value", [False, 123, [], {}])
def test_invalid_snapshot_status_is_rejected_before_writes(mem_db, value):
    with pytest.raises(HcsError):
        sync.apply_snapshot(mem_db, "evs_volume", [fetchers.LedgerRecord("one", "one", status=value)])
    assert not mem_db.scalars(select(Resource)).all()


def test_legitimate_empty_error_fields_and_list_still_work():
    assert response_items({"items": [], "error_code": None, "error": None}, "items") == []


def test_failed_type_cannot_pollute_later_type_owner_context(mem_db, monkeypatch):
    def fail(_client, context):
        context["ecs_names"]["phantom"] = "wrong-owner"
        raise HcsError(0, "SYNTHETIC.FAILURE", "failure")

    observed = {}

    def succeed(_client, context):
        observed.update(context["ecs_names"])
        return []

    monkeypatch.setitem(sync.FETCHERS, "evs_volume", fail)
    monkeypatch.setitem(sync.FETCHERS, "vpc", succeed)
    run = sync.sync_ledger(mem_db, client=ReviewClient({}), target=TARGET, types=["evs_volume", "vpc"], trigger="MANUAL")
    assert run.status == "PARTIAL" and observed == {}


@pytest.mark.parametrize("port,expected", [("port-a", "云服务器 vm-a"), ("port-b", "云服务器 vm-b"),
                                          ("unknown-port", "192.0.2.10"), (None, "192.0.2.10")])
def test_eip_binding_uses_native_port_identity_across_overlapping_vpcs(port, expected):
    servers = [{"id": f"vm-{suffix}", "name": f"vm-{suffix}", "addresses": {"net": [
        {"addr": "192.0.2.10", "OS-EXT-IPS:type": "fixed", "OS-EXT-IPS:port_id": f"port-{suffix}"}]}}
        for suffix in ("a", "b")]
    client = ReviewClient({"servers": servers, "count": 2})
    context = {}
    fetchers.fetch_ecs(client, context)
    client.body = inventory("eip", private_ip_address="192.0.2.10", port_id=port)
    record, = fetchers.fetch_eip(client, context)
    assert record.attrs["bound_to"] == expected


def test_eip_binding_recognizes_secondary_ecs_port():
    client = ReviewClient(inventory("ecs_server", addresses={"net": [
        {"addr": "192.0.2.10", "OS-EXT-IPS:type": "fixed", "OS-EXT-IPS:port_id": "primary"},
        {"addr": "192.0.2.11", "OS-EXT-IPS:type": "fixed", "OS-EXT-IPS:port_id": "secondary"}]}))
    context = {}
    server, = fetchers.fetch_ecs(client, context)
    client.body = inventory("eip", private_ip_address="192.0.2.11", port_id="secondary")
    record, = fetchers.fetch_eip(client, context)
    assert record.attrs["bound_to"] == "云服务器 one"
    assert server.attrs["fixed_ports"] == {"primary": "192.0.2.10", "secondary": "192.0.2.11"}


@pytest.mark.parametrize("rtype,label,attrs", [("ecs_server", "云服务器", {"fixed_ports": {"native-port": "192.0.2.10"}}),
                                             ("elb", "负载均衡", {"vip_port_id": "native-port"})])
def test_standalone_eip_sync_restores_native_owner_mapping_from_selected_project(mem_db, rtype, label, attrs):
    row = Resource(service="review", resource_type=rtype, resource_id="owner", name="right-owner", attrs=attrs,
                   ip="192.0.2.10", tenant_id=101, project_ref=1001)
    foreign = Resource(service="review", resource_type=rtype, resource_id="other", name="wrong-project",
                       attrs=attrs, ip="192.0.2.10", tenant_id=202, project_ref=2002)
    mem_db.add_all([row, foreign])
    mem_db.commit()
    client = ReviewClient(inventory("eip", private_ip_address="192.0.2.10", port_id="native-port"))
    run = sync.sync_ledger(mem_db, client=client, target=TARGET, types=["eip"], trigger="MANUAL")
    result = mem_db.scalar(select(Resource).where(Resource.resource_type == "eip"))
    assert run.status == "SUCCESS" and result.attrs["bound_to"] == f"{label} right-owner"


def test_evs_record_does_not_hide_other_native_attachments():
    client = ReviewClient(inventory("evs_volume", attachments=[
        {"server_id": "vm-a", "device": "/dev/vda", "attachment_id": "a"},
        {"server_id": "vm-b", "device": "/dev/vdb", "attachment_id": "b"}]))
    record, = fetchers.fetch_evs(client, {"ecs_names": {"vm-a": "first", "vm-b": "second"}})
    assert record.attrs["attached_to"] == "first, second"
    assert record.attrs["device"] == "/dev/vda, /dev/vdb"
    assert [a["server_id"] for a in record.attrs["attachments"]] == ["vm-a", "vm-b"]


@pytest.mark.parametrize("rtype", TYPES)
@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_invalid_service_status_never_becomes_a_string_or_silent_unknown(rtype, value):
    field = {"elb": "operating_status", "mrs_cluster": "clusterState"}.get(rtype, "status")
    change = {field: value} if rtype != "cce_cluster" else {"status": {"phase": value}}
    with pytest.raises(HcsError):
        fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **change)), {})


@pytest.mark.parametrize("value", [False, 0, [], "bad"])
def test_cce_invalid_status_container_is_an_explicit_error(value):
    with pytest.raises(HcsError):
        fetchers.fetch_cce(ReviewClient(inventory("cce_cluster", status=value)), {})


@pytest.mark.parametrize("rtype,changes,field", [("evs_volume", {"size": 0}, "storage_gb")])
def test_native_zero_capacity_is_preserved_in_snapshot(mem_db, rtype, changes, field):
    record, = fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **changes)), {})
    sync.apply_snapshot(mem_db, rtype, [record], tenant_id=101, project_ref=1001)
    assert getattr(mem_db.scalar(select(Resource)), field) == 0


def test_evs_unattached_or_unknown_attachment_clears_stale_ownership(mem_db):
    row = Resource(service="evs", resource_type="evs_volume", resource_id="one", name="one",
                   attrs={"attached_to": "deleted-server", "device": "/dev/vdb", "bootable": "是"},
                   tenant_id=101, project_ref=1001)
    mem_db.add(row)
    mem_db.commit()
    record, = fetchers.fetch_evs(ReviewClient(inventory("evs_volume", attachments=[])), {})
    sync.apply_snapshot(mem_db, "evs_volume", [record], tenant_id=101, project_ref=1001)
    assert row.attrs["attached_to"] is None and row.attrs["device"] is None and row.attrs["bootable"] is None


@pytest.mark.parametrize("rtype,raw_field,attr", [("dws_cluster", "number_of_node", "node_count"),
    ("mrs_cluster", "totalNodeNum", "nodes")])
@pytest.mark.parametrize("value", [None, -1, True, "NaN", "Infinity", 1.5])
def test_unavailable_native_counts_are_unknown_instead_of_invalid_quantities(rtype, raw_field, attr, value):
    record, = fetchers.FETCHERS[rtype](ReviewClient(inventory(rtype, **{raw_field: value})), {})
    assert record.attrs[attr] is None
    assert "None" not in (record.spec or "")
    assert "nan" not in (record.spec or "").lower()
    assert "inf" not in (record.spec or "").lower()


@pytest.mark.parametrize("field,source", [("memory_size", "flavor_info"), ("volume_size", "volume")])
@pytest.mark.parametrize("value", [None, -1, "NaN"])
def test_gaussdb_unknown_raw_sizes_do_not_preserve_stale_current_attrs(mem_db, field, source, value):
    raw_key = "mem" if source == "flavor_info" else "size"
    record, = fetchers.fetch_gaussdb(ReviewClient(inventory("gaussdb_instance", **{source: {raw_key: value}})), {})
    row = Resource(service="gaussdb", resource_type="gaussdb_instance", resource_id="one", name="one",
                   tenant_id=101, project_ref=1001, attrs={field: 64})
    mem_db.add(row)
    mem_db.commit()
    sync.apply_snapshot(mem_db, "gaussdb_instance", [record], tenant_id=101, project_ref=1001)
    assert row.attrs[field] is None


def test_ecs_sum_of_finite_volume_sizes_cannot_create_infinity():
    client = ReviewClient(inventory("ecs_server"))
    get = client.get
    volumes = [{"id": f"disk-{i}", "size": 1e308, "attachments": [{"server_id": "one"}]} for i in range(2)]
    client.get = lambda service, path, **kw: {"volumes": volumes} if service == "evs" else get(service, path, **kw)
    record, = fetchers.fetch_ecs(client, {})
    assert record.storage_gb is None


@pytest.mark.parametrize("rtype", TYPES)
@pytest.mark.parametrize("state", [None, "FUTURE_STATE"])
def test_new_inventory_with_unavailable_health_evidence_is_unknown(mem_db, rtype, state):
    record = fetchers.LedgerRecord("new", "new", status=state)
    sync.apply_snapshot(mem_db, rtype, [record], tenant_id=101, project_ref=1001)
    assert mem_db.scalar(select(Resource)).health == "UNKNOWN"
