"""接口修正须覆盖已有台账，不能只让新建资源显示正确。"""
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext
from app.core.db import Base
from app.hcs import HcsError
from app.ledger import fetchers
from app.ledger.sync import apply_snapshot
from app.models import Resource, ResourceChange
from app.models.base import utcnow
from app.services.pipeline import upsert_resource

from .test_ledger_hcs851_contract import OfficialResponses, dws_cluster, gaussdb_instance


@pytest.fixture
def isolated_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def native_record(rtype):
    if rtype == "gaussdb_instance":
        client = OfficialResponses({("gaussdb", "/gaussdb/v3.3/{project_id}/instances"): [
            {"instances": [gaussdb_instance()], "total_count": 1}]})
        return fetchers.fetch_gaussdb(client, {})[0]
    if rtype == "elb":
        client = OfficialResponses({("elb", "/v3/{project_id}/elb/loadbalancers"): [
            {"loadbalancers": [{"id": "elb-contract", "name": "contract", "guaranteed": True}]}]})
        return fetchers.fetch_elb(client, {})[0]
    if rtype == "evs_volume":
        client = OfficialResponses({("evs", "/v2/{project_id}/cloudvolumes/detail"): [
            {"volumes": [{"id": "evs-contract", "name": "contract", "size": 40, "encrypted": True}]}]})
        return fetchers.fetch_evs(client, {"ecs_names": {}})[0]
    client = OfficialResponses({("dws", "/v1.0/{project_id}/clusters"): [
        {"clusters": [dws_cluster(endpoints=[])]}]})
    return fetchers.fetch_dws(client, {})[0]


@pytest.mark.parametrize("rtype,old_values,old_attrs", [
    ("gaussdb_instance", {"memory_gb": 64, "storage_gb": 500}, {"volume_gb": 500}),
    ("elb", {"spec": "独享"}, {"members": 0, "listeners": 0}),
    ("evs_volume", {}, {"encrypted": True}),
    ("dws_cluster", {"ip": "192.0.2.250"}, {}),
])
def test_native_snapshot_clears_only_declared_invalid_old_fields(isolated_db, rtype, old_values, old_attrs):
    record = native_record(rtype)
    row = Resource(service="contract", resource_type=rtype, resource_id=record.resource_id, name=record.name,
                   attrs={"manual_tag": "keep", **old_attrs}, tenant_id=101, project_ref=1001, **old_values)
    other = Resource(service="contract", resource_type=rtype, resource_id=record.resource_id, name="other-project",
                     attrs=dict(old_attrs), tenant_id=102, project_ref=1002, **old_values)
    isolated_db.add_all([row, other])
    isolated_db.commit()
    result = apply_snapshot(isolated_db, rtype, [record], tenant_id=101, project_ref=1001)
    assert result["updated"] == 1 and result["deleted"] == 0
    for key in record.clear_fields:
        assert getattr(row, key) is None
    for key in record.clear_attrs:
        assert row.attrs[key] is None
    assert row.attrs["manual_tag"] == "keep"
    assert all(getattr(other, key) == value for key, value in old_values.items())
    assert other.attrs == old_attrs
    changes = isolated_db.scalars(select(ResourceChange).where(ResourceChange.resource_ref == row.id)).all()
    assert changes and any(change["new"] is None for event in changes for change in event.changes)
    assert apply_snapshot(isolated_db, rtype, [record], tenant_id=101, project_ref=1001)["updated"] == 0


@pytest.mark.parametrize("verified,expected", [(False, None), (None, 64)])
def test_gaussdb_status_collection_clears_only_explicitly_unverified_capacity(isolated_db, verified, expected):
    disk = {"disk_usage": 25, "disk_used_gb": 50, "disk_total_gb": 200}
    monitoring = {"status": "AVAILABLE", "observed_at": "2026-11-09T00:00:00+00:00"}
    components = {"state": "AVAILABLE", "total": 1, "down": 0, "items": [{"component_id": "dn-1"}]}
    row = Resource(service="gaussdb", resource_type="gaussdb_instance", resource_id="one", name="one",
                   memory_gb=64, storage_gb=64, spec="64 GB | 64 GB",
                   attrs={"volume_gb": 64, "gaussdb_monitoring": monitoring, "gaussdb_components": components},
                   metrics={"cpu_usage": 99, "mem_usage": 98, **disk},
                   tenant_id=101, project_ref=1001)
    isolated_db.add(row)
    isolated_db.commit()
    attrs = {} if verified is None else {"memory_unit_verified": verified, "volume_unit_verified": verified,
                                        "volume_gb": None, "type": "分布式", "vcpus": 8}
    obs = Observation("gaussdb_instance", "one", "one", "instance_status", {"status": "ACTIVE"}, attrs=attrs,
                      metrics={"cpu_usage": None, "mem_usage": None})
    ctx = RunContext(client=None, account="contract", project_id="contract", tenant_id=101, project_ref=1001)
    result = upsert_resource(isolated_db, "gaussdb", [obs], utcnow(), ctx)
    assert result.id == row.id and result.memory_gb == expected and result.storage_gb == expected
    assert result.attrs["volume_gb"] == expected
    assert result.spec == ("分布式 | 8 vCPU" if verified is False else "64 GB | 64 GB")
    assert result.metrics == {"cpu_usage": None, "mem_usage": None, **disk}
    assert result.attrs["gaussdb_monitoring"] == monitoring
    assert result.attrs["gaussdb_components"] == components


@pytest.mark.parametrize("bad", ["field", "attr"])
def test_invalid_clear_declaration_fails_before_applying_any_snapshot(isolated_db, bad):
    original = fetchers.LedgerRecord("one", "one")
    invalid = replace(original, clear_fields=("not_a_field",)) if bad == "field" else replace(
        original, clear_attrs=("absent_attr",))
    with pytest.raises(HcsError, match="字段清理声明"):
        apply_snapshot(isolated_db, "gaussdb_instance", [invalid])
    assert isolated_db.scalars(select(Resource)).all() == []
