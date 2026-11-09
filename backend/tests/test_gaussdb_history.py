"""GaussDB collector→pipeline→本地历史→同条件 XLSX；合成云与独立内存库。"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.collectors.base import Observation, RunContext
from app.collectors.gaussdb import GaussDBCollector, GaussDBStorageCollector, GaussStorageObservation
from app.hcs import HcsError
from app.models import GaussDBStorageSample, HcsAccount, HcsProject, Resource
from app.services import gaussdb_monitoring as service
from app.services.cleanup import run_cleanup
from app.services.gaussdb_history import persist_disk_samples
from app.services.pipeline import process_observations
from tests.test_gaussdb_monitoring import START, path, slow_params, workbook
from tests.test_gaussdb_monitoring import gauss_api as gauss_api


def history_params(**extra):
    return {"start_at": START.isoformat(), "end_at": (START + timedelta(minutes=15)).isoformat(),
            "period_sec": 300, **extra}


def sample(g, at=START, *, resource_ref=111, tenant_id=1, project_ref=11, status="AVAILABLE", used=25, total=100):
    row = GaussDBStorageSample(resource_ref=resource_ref, tenant_id=tenant_id, project_ref=project_ref,
        observed_at=at, collected_at=at, status=status, used_gb=used, total_gb=total,
        usage_percent=round(used / total * 100, 2) if used is not None and total else None)
    g.db.add(row)
    g.db.commit()
    return row


def test_history_read_and_export_require_no_task_permission_and_never_call_cloud(gauss_api):
    g = gauss_api
    g.user.permissions = ["ledger:view", "ledger:export"]
    sample(g, used=0)
    sample(g, START + timedelta(minutes=10), used=40)
    response = g.client.get(path("disk-history"), params=history_params())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["instance"]["id"] == 111 and data["instance"]["project_ref"] == 11
    assert data["sampling_source"] == "LOCAL_COLLECTOR" and data["source"] == "GAUSSDB"
    assert data["time_basis"] == "RESPONSE_RECEIVED"
    assert data["points"][0]["collected_at"] == data["points"][0]["observed_at"]
    assert data["status"] == "PARTIAL"
    assert [p["usage_percent"] for p in data["points"]] == [0, None, 40]
    assert data["points"][1]["observed_at"] is None
    assert data["points"][1]["collected_at"] is None
    assert g.cloud.requests == g.cloud.contexts == []
    book = workbook(g.client.get(path("export"), params={"panel": "disk-history", **history_params()}))
    rows = list(book["存储历史"].iter_rows(min_row=2, values_only=True))
    assert [r[4] for r in rows] == [0, None, 40]
    assert rows[1][1:5] == (None, None, None, None)
    assert rows[0][1] == START.replace(tzinfo=None)
    assert all(c.data_type != "f" for sheet in book for row in sheet for c in row)
    assert g.cloud.requests == g.cloud.contexts == []
    assert g.client.get(path("overview")).status_code == 403
    assert g.client.get(path("export"), params={"panel": "overview"}).status_code == 403


@pytest.mark.parametrize("export", [False, True])
@pytest.mark.parametrize("permission", ["ledger:view", "ledger:export"])
def test_history_permissions_and_tenant_scope_before_any_data(gauss_api, export, permission):
    g = gauss_api
    g.user.permissions = [p for p in g.user.permissions if p != permission]
    suffix = "export" if export else "disk-history"
    params = {"panel": "disk-history", **history_params()} if export else history_params()
    response = g.client.get(path(suffix), params=params)
    assert response.status_code == (403 if export or permission == "ledger:view" else 200)
    g.user.permissions = ["ledger:view", "ledger:export"]
    assert g.client.get(path(suffix, 211), params=params).status_code == 404
    assert g.client.get(path(suffix), params=params, headers={"X-Tenant-Id": "2"}).status_code == 403
    assert g.cloud.requests == []


def test_history_half_open_latest_observation_failure_and_zero_capacity(gauss_api):
    g = gauss_api
    sample(g, START - timedelta(microseconds=1), used=99)
    sample(g, START + timedelta(seconds=1), used=10)
    sample(g, START + timedelta(seconds=299), status="ERROR", used=None, total=None)
    sample(g, START + timedelta(seconds=301), used=50)
    sample(g, START + timedelta(seconds=302), used=0, total=0, status="PARTIAL")
    sample(g, START + timedelta(minutes=15), used=88)
    data = g.client.get(path("disk-history"), params=history_params()).json()["data"]
    assert data["status"] == "PARTIAL"
    assert data["points"][0]["observed_at"] is None
    assert data["points"][0]["used_gb"] is None
    assert data["points"][1]["used_gb"] == data["points"][1]["total_gb"] == 0
    assert data["points"][1]["usage_percent"] is None
    assert data["points"][2]["observed_at"] is None
    assert len(data["points"]) == 3


def test_history_uses_all_scope_columns_and_never_inventory_values_or_other_project(gauss_api):
    g = gauss_api
    sample(g, tenant_id=2, project_ref=21, used=95)  # 同 resource_ref 的损坏身份也不可读。
    sample(g, resource_ref=112, tenant_id=1, project_ref=12, used=88)
    data = g.client.get(path("disk-history"), params=history_params()).json()["data"]
    assert data["status"] == "NO_DATA" and all(p["usage_percent"] is None for p in data["points"])
    assert g.db.get(Resource, 111).metrics["disk_usage"] == 97
    other = g.client.get(path("disk-history", 112), params=history_params()).json()["data"]
    assert other["instance"]["project_ref"] == 12 and other["points"][0]["usage_percent"] == 88
    assert g.cloud.requests == []


def test_history_is_uncached_and_preserves_real_timestamp_instead_of_bucket_time(gauss_api):
    g = gauss_api
    assert g.client.get(path("disk-history"), params=history_params()).json()["data"]["status"] == "NO_DATA"
    at = START + timedelta(seconds=123)
    sample(g, at)
    point = g.client.get(path("disk-history"), params=history_params()).json()["data"]["points"][0]
    assert point["timestamp"] == START.isoformat().replace("+00:00", "Z")
    assert point["observed_at"] == at.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("params", [history_params(period_sec=59), history_params(period_sec=86401),
    history_params(start_at=START.replace(tzinfo=None).isoformat()), history_params(end_at=START.isoformat()),
    history_params(end_at=(START + timedelta(days=32)).isoformat()),
    history_params(end_at=(START + timedelta(days=31)).isoformat(), period_sec=60),
    {"start_at": START.isoformat()}])
def test_history_invalid_ranges_rejected_without_cloud(gauss_api, params):
    g = gauss_api
    assert g.client.get(path("disk-history"), params=params).status_code == 400
    assert g.client.get(path("export"), params={"panel": "disk-history", **params}).status_code == 400
    assert g.cloud.requests == []


def test_history_complete_status_and_local_disabled_tenant_access(gauss_api):
    g = gauss_api
    for minute in (0, 5, 10):
        sample(g, START + timedelta(minutes=minute), used=minute)
    g.db.get(HcsAccount, 1).enabled = False
    g.db.get(HcsProject, 11).enabled = False
    g.db.commit()
    assert g.client.get(path("disk-history"), params=history_params()).json()["data"]["status"] == "AVAILABLE"
    assert g.cloud.requests == []


class CollectorCloud:
    def __init__(self, volume=None, status="ACTIVE"):
        self.calls = []
        self.volume = {"used": "25", "total": "200"} if volume is None else volume
        self.status = status

    def get(self, service_name, path, *, params=None):
        self.calls.append((service_name, path, params))
        assert service_name == "gaussdb"
        if path.endswith("/instances"):
            return {"total_count": 1, "instances": [{"id": "same-cloud-instance", "name": "原生实例",
                "status": self.status, "volume": {"size": 9999}, "flavor_info": {"mem": 8888}, "nodes": []}]}
        assert path == "/gaussdb/v3/{project_id}/instances/same-cloud-instance/volume-usage"
        if isinstance(self.volume, Exception):
            raise self.volume
        return self.volume


def context(g, cloud):
    return RunContext(client=cloud, account="gauss-isolated-1", project_id="project-11", tenant_id=1,
                      project_ref=11, db=g.db, tenant=g.db.get(HcsAccount, 1))


@pytest.mark.parametrize("volume,status,expected", [({"used": "25", "total": "200"}, "ACTIVE", "AVAILABLE"),
    ({"used": "0", "total": "0"}, "ACTIVE", "PARTIAL"),
    ({"used": "bad", "total": "200"}, "ACTIVE", "ERROR"),
    (HcsError(503, "SYNTHETIC", "private"), "ACTIVE", "ERROR"),
    ({"used": "25", "total": "200"}, "BUILD", "UNAVAILABLE")])
def test_collector_pipeline_persist_actual_samples_and_failures_not_config(gauss_api, monkeypatch, volume, status, expected):
    g = gauss_api
    monkeypatch.setattr("app.collectors.gaussdb.utcnow", lambda: START)
    cloud = CollectorCloud(volume, status)
    ctx = context(g, cloud)
    observations = GaussDBStorageCollector().collect(ctx)
    assert [o.check_item for o in observations] == ["disk"]
    assert observations[0].status_raw is None and observations[0].storage_status == expected
    assert not any("disk_sample_status" in o.metrics for o in observations)
    process_observations(g.db, "gaussdb", observations, ctx, START)
    g.db.commit()
    row = g.db.scalar(select(GaussDBStorageSample))
    assert row.status == expected and row.observed_at == row.collected_at == START
    assert (row.resource_ref, row.tenant_id, row.project_ref) == (111, 1, 11)
    if expected == "AVAILABLE":
        assert (row.used_gb, row.total_gb, row.usage_percent) == (25, 200, 12.5)
    elif expected == "PARTIAL":
        assert row.used_gb == row.total_gb == 0 and row.usage_percent is None
    else:
        assert row.used_gb is row.total_gb is row.usage_percent is None
    assert g.db.get(Resource, 112).metrics["disk_usage"] == 97  # 同云 ID 的其他项目不变。
    resource = g.db.get(Resource, 111)
    assert resource.metrics["cpu_usage"] == 99 and resource.metrics["mem_usage"] == 98
    assert resource.status_raw == "ACTIVE" and resource.attrs["memory_unit_verified"] is True
    assert all(service_name == "gaussdb" for service_name, _, _ in cloud.calls)
    assert len(cloud.calls) == (1 if status == "BUILD" else 2)
    disk_metrics = {key: resource.metrics[key] for key in ("disk_usage", "disk_used_gb", "disk_total_gb")}
    monitoring = resource.attrs["gaussdb_monitoring"].copy()
    before_status = len(cloud.calls)
    process_observations(g.db, "gaussdb", GaussDBCollector().collect(ctx), ctx, START)
    assert len(cloud.calls) == before_status + 1 and cloud.calls[-1][1].endswith("/instances")
    assert resource.metrics["cpu_usage"] is resource.metrics["mem_usage"] is None
    assert {key: resource.metrics[key] for key in disk_metrics} == disk_metrics
    assert resource.attrs["gaussdb_monitoring"] == monitoring
    assert g.db.scalar(select(func.count()).select_from(GaussDBStorageSample)) == 1


@pytest.mark.parametrize("skip", ["snapshot", "unmonitored", "wrong_scope", "no_project", "naive"])
def test_writer_requires_explicit_collector_provenance_and_monitored_identity(gauss_api, skip):
    g = gauss_api
    ctx = context(g, CollectorCloud())
    row = g.db.get(Resource, 111)
    at = START.replace(tzinfo=None) if skip == "naive" else START
    cls = Observation if skip == "snapshot" else GaussStorageObservation
    observation = cls("gaussdb_instance", row.resource_id, row.name, "disk", {}, metrics={
        "disk_used_gb": 25, "disk_total_gb": 100, "disk_usage": 25}, metric_observed_at=at)
    if skip == "unmonitored":
        row.monitored = False
    elif skip == "wrong_scope":
        ctx.project_ref = 12
    elif skip == "no_project":
        ctx.project_ref = None
    key = ("gaussdb_instance", row.resource_id)
    assert persist_disk_samples(g.db, {key: [observation]}, {key: row}, ctx, START) == 0
    assert g.db.scalar(select(func.count()).select_from(GaussDBStorageSample)) == 0


def test_pipeline_new_resource_flushes_before_sample_and_retries_are_idempotent(gauss_api, monkeypatch):
    g = gauss_api
    monkeypatch.setattr("app.collectors.gaussdb.utcnow", lambda: START)
    ctx = context(g, CollectorCloud())
    observations = GaussDBStorageCollector().collect(ctx)
    for observation in observations:
        observation.resource_id = "new-gauss-instance"
    process_observations(g.db, "gaussdb", observations, ctx, START)
    process_observations(g.db, "gaussdb", observations, ctx, START)
    rows = g.db.scalars(select(GaussDBStorageSample)).all()
    assert len(rows) == 1 and rows[0].resource_ref != 111
    assert g.db.get(Resource, rows[0].resource_ref).resource_id == "new-gauss-instance"
    # pipeline 自身拥有提交权，持久化 helper 不能提前提交自己的新样本。
    key = ("gaussdb_instance", "new-gauss-instance")
    resource = g.db.get(Resource, rows[0].resource_ref)
    for observation in observations:
        observation.metric_observed_at = START + timedelta(seconds=1)
    persist_disk_samples(g.db, {key: observations}, {key: resource}, ctx, START + timedelta(seconds=1))
    assert len(g.db.scalars(select(GaussDBStorageSample)).all()) == 2
    g.db.rollback()
    assert len(g.db.scalars(select(GaussDBStorageSample)).all()) == 1


def test_cleanup_expires_samples_and_released_resource_children_preserving_active(gauss_api):
    g = gauss_api
    now = datetime(2026, 11, 2, tzinfo=UTC)
    keep = sample(g, now)
    sample(g, now - timedelta(days=91))
    victim = g.db.get(Resource, 112)
    victim.lifecycle, victim.deleted_at = "DELETED", now - timedelta(days=1000)
    sample(g, now, resource_ref=112, project_ref=12)
    result = run_cleanup(g.db, now)
    g.db.commit()
    assert result["gaussdb_storage_sample"] == 2
    assert [row.id for row in g.db.scalars(select(GaussDBStorageSample))] == [keep.id]


def test_slow_detail_export_uses_exact_cached_query_and_native_microsecond_columns(gauss_api):
    g = gauss_api
    params = slow_params(sql_id="sql-1")
    data = g.client.get(path("slow-sql/detail"), params=params).json()["data"]
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "slow-sql-detail", **params}))
    assert len(g.cloud.requests) == before
    rows = list(book["慢SQL详情"].iter_rows(values_only=True))
    assert "总耗时（微秒）" in rows[0]
    assert rows[1][8] == data["items"][0]["total_time"] == 1500000
    assert rows[1][9:13] == (0, 25, None, 0)


def test_cached_rows_translates_cache_failure_without_upstream_or_details(gauss_api, monkeypatch):
    g = gauss_api
    def unavailable(*_args, **_kwargs):
        raise HcsError(0, "LOCAL.QUERY_CACHE", "PRIVATE_CACHE_DETAIL")
    monkeypatch.setattr(service._cache, "get", unavailable)
    response = g.client.get(path("slow-nodes"))
    assert response.status_code == 200 and response.json()["data"]["status"] == "ERROR"
    assert "PRIVATE_CACHE_DETAIL" not in response.text and not g.cloud.requests


def test_overview_time_basis_is_local_response_received_not_upstream_sample(gauss_api, monkeypatch):
    g = gauss_api
    received = START + timedelta(days=2)
    monkeypatch.setattr(service, "utcnow", lambda: received)
    g.cloud.overrides["volume-usage"] = {"used": "0", "total": "200", "timestamp": 1}
    disk = g.client.get(path("overview")).json()["data"]["disk"]
    assert disk["time_basis"] == "RESPONSE_RECEIVED"
    assert disk["collected_at"] == disk["observed_at"] == received.isoformat().replace("+00:00", "Z")
    g.cloud.overrides["volume-usage"] = (503, {"error_code": "SYNTHETIC"})
    monkeypatch.setattr(service, "_cache", service.QueryCache())
    failed = g.client.get(path("overview")).json()["data"]["disk"]
    assert failed["time_basis"] is failed["collected_at"] is failed["observed_at"] is None


def test_storage_history_received_time_never_uses_pipeline_clock(gauss_api, monkeypatch):
    g = gauss_api
    monkeypatch.setattr("app.collectors.gaussdb.utcnow", lambda: START)
    ctx = context(g, CollectorCloud())
    observations = GaussDBStorageCollector().collect(ctx)
    process_observations(g.db, "gaussdb", observations, ctx, START + timedelta(minutes=10))
    row = g.db.scalar(select(GaussDBStorageSample))
    assert row.observed_at == row.collected_at == START
    point = g.client.get(path("disk-history"), params=history_params()).json()["data"]["points"][0]
    assert point["collected_at"] == point["observed_at"] == START.isoformat().replace("+00:00", "Z")
    assert point["used_gb"] == 25 and point["total_gb"] == 200


@pytest.mark.parametrize("field,value", [("instance_id", "foreign"), ("node_id", "node-21"),
                                         ("component_id", "component-21")])
def test_sessions_rejects_explicit_foreign_scope_in_upstream_response(gauss_api, field, value):
    g = gauss_api
    g.cloud.overrides["real-time-session"] = {"sessions": [{field: value, "query": "PRIVATE_FOREIGN_QUERY"}]}
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"})
    assert response.json()["data"]["status"] == "ERROR" and "PRIVATE_FOREIGN_QUERY" not in response.text
