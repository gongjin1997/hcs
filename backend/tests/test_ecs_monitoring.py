"""ECS 单机趋势与采样契约：独立库、合成身份、禁止真实云查询。"""
import io
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.v1.ecs import router
from app.collectors.base import RunContext
from app.collectors.ecs import EcsStatusCollector
from app.collectors.ecs_metrics import EcsMetricsCollector, ip_index
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_current_user
from app.core.response import BizError
from app.main import app as production_app
from app.metrics.ecs_readings import EcsReading, EcsReadingsResult
from app.models import (
    AlarmEvent,
    AlarmRule,
    CollectSwitch,
    EcsMetricSample,
    EvalState,
    HcsAccount,
    HcsProject,
    Resource,
    SysSetting,
)
from app.reports.ecs_excel import export_metrics
from app.schemas.collection_policy import CollectionPolicy
from app.services import ecs_monitoring as service

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture()
def ecs_world(mem_db, monkeypatch):
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"ecs-{tid}", name=f"测试租户{tid}", iam_endpoint="https://iam.invalid",
            domain_name=f"domain-{tid}", enabled=True, metrics_source="PROMETHEUS",
            prometheus_url="https://prom.invalid", endpoints={}))
    mem_db.flush()
    for pid, tid in ((11, 1), (12, 1), (21, 2)):
        mem_db.add(HcsProject(id=pid, account_id=tid, region_id="test-region", project_id=f"project-{pid}",
            project_name=f"测试项目{pid}", external_domain="example.invalid", enabled=True))
    mem_db.flush()
    for rid, tid, pid in ((1, 1, 11), (2, 1, 12), (3, 2, 21)):
        mem_db.add(Resource(id=rid, tenant_id=tid, project_ref=pid, service="ecs", resource_type="ecs_server",
            resource_id="shared-cloud-id", name=f"测试主机{rid}", ip="10.0.0.1", attrs={}, metrics={},
            health="OK", status_raw="ACTIVE", monitored=True, critical=rid == 1, last_seen=NOW))
    mem_db.commit()
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    from app.collectors import ecs, ecs_metrics
    monkeypatch.setattr(ecs, "utcnow", lambda: NOW)
    monkeypatch.setattr(ecs_metrics, "utcnow", lambda: NOW)
    return mem_db


def reader(*permissions):
    user = CurrentUser(id=100, username="ecs-reader", display_name="指标读者", permissions=list(permissions or ["ledger:view"]),
                       tenant_ids=[1])
    return Scope([1], False, user)


def policy(**updates):
    return CollectionPolicy.model_validate(updates).model_dump(mode="json")


def snapshot(db, at=NOW-timedelta(seconds=60), value=40, **config):
    row, tenant = db.get(Resource, 1), db.get(HcsAccount, 1)
    setting = policy(**config)
    data = {key: {"value": value, "observed_at": at.isoformat(), "collected_at": NOW.isoformat(),
                  "source": "PROMETHEUS", "dimensions": {}} for key in service.METRICS}
    row.attrs = {"ecs_monitoring": {"config_signature": service.config_signature(tenant, setting, "PROMETHEUS"),
        "source": "PROMETHEUS", "checked_at": NOW.isoformat(), "latest": data,
        "diagnostic": {"status": "ready"}}}
    db.commit()
    return row, tenant, setting


def point(value=42.5, at=NOW-timedelta(seconds=60), metric="cpu_util", source="PROMETHEUS", **kwargs):
    return EcsReading(metric=metric, value=value, observed_at=at, source=source, **kwargs)


def sample(db, *, rid=1, tid=1, pid=11, value=42.5, at=NOW-timedelta(seconds=60), metric="cpu_util", source="PROMETHEUS"):
    db.add(EcsMetricSample(resource_ref=rid, tenant_id=tid, project_ref=pid, metric=metric, value=value,
        source=source, unit="%", observed_at=at, collected_at=NOW, period_sec=60, dimensions={}))
    db.commit()


def out(db, row, tenant, setting):
    return service.instance_out(row, tenant, db.get(HcsProject, row.project_ref), setting, master=True, now=NOW)


def test_snapshot_last_seen_and_old_resource_numbers_do_not_create_evidence(ecs_world):
    db = ecs_world
    row = db.get(Resource, 1)
    row.metrics = {key: 99 for key in service.METRICS}
    db.commit()
    data = service.list_instances(db, reader())
    assert data.total == 2
    assert all(item.latest["cpu_util"].value is None for item in data.items)
    assert all(item.latest["cpu_util"].observed_at is None for item in data.items)
    assert data.items[0].diagnostic.status == "NO_DATA"


@pytest.mark.parametrize("age,state", [(60, "FRESH"), (900, "FRESH"), (901, "STALE")])
def test_actual_sample_time_controls_freshness_instead_of_collection_or_ledger_time(ecs_world, age, state):
    row, tenant, setting = snapshot(ecs_world, at=NOW-timedelta(seconds=age), value=0)
    result = out(ecs_world, row, tenant, setting)
    assert result.latest["cpu_util"].value == 0
    assert result.latest["cpu_util"].state == state
    assert result.latest["cpu_util"].observed_at == NOW-timedelta(seconds=age)
    assert result.latest["cpu_util"].collected_at == NOW


@pytest.mark.parametrize("updates,expected", [
    ({"metrics": {"enabled": False}}, "DISABLED"),
    ({"selection": {"mode": "selected", "resource_ids": []}}, "NOT_SELECTED"),
])
def test_disabled_and_unselected_never_present_current_numbers(ecs_world, updates, expected):
    row, tenant, setting = snapshot(ecs_world, **updates)
    result = out(ecs_world, row, tenant, setting)
    assert result.latest["cpu_util"].state == expected and result.latest["cpu_util"].value is None


def test_source_and_mapping_change_invalidate_old_latest_without_erasing_history(ecs_world):
    row, tenant, setting = snapshot(ecs_world)
    sample(ecs_world)
    tenant.prometheus_url = "https://another.invalid"
    assert out(ecs_world, row, tenant, setting).latest["cpu_util"].value is None
    tenant.metrics_source = "NONE"
    assert out(ecs_world, row, tenant, setting).latest["cpu_util"].state == "SOURCE_UNCONFIGURED"
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 1


def test_requested_cpu_only_is_not_reported_as_partial_for_intentionally_unselected_metrics(ecs_world):
    row, tenant, setting = snapshot(ecs_world, metric_keys=["cpu_util"])
    result = out(ecs_world, row, tenant, setting)
    assert result.latest["mem_util"].state == "DISABLED"
    assert result.diagnostic.status == "FRESH"


def test_malformed_snapshot_is_not_a_500(ecs_world):
    row = ecs_world.get(Resource, 1)
    row.attrs = {"ecs_monitoring": "bad"}
    assert service.list_instances(ecs_world, reader()).items[0].latest["cpu_util"].value is None


def test_sample_storage_is_idempotent_and_uses_upstream_timestamp(ecs_world):
    row = ecs_world.get(Resource, 1)
    observed = NOW-timedelta(seconds=120)
    reading = point(0, at=observed)
    for collected in (NOW, NOW+timedelta(seconds=30)):
        accepted, rejected = service.save_readings(ecs_world, [row], {row.resource_id: [reading]}, collected)
        ecs_world.commit()
        assert len(accepted[row.id]) == 1 and rejected == 0
    saved = ecs_world.scalar(select(EcsMetricSample))
    assert saved.observed_at == observed and saved.collected_at == NOW and saved.value == 0
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -1, 101])
def test_invalid_percent_is_never_saved_or_coerced_to_zero(ecs_world, value):
    row = ecs_world.get(Resource, 1)
    accepted, rejected = service.save_readings(ecs_world, [row], {row.resource_id: [point(value)]}, NOW)
    assert not accepted and rejected == 1
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_conflicting_timestamp_never_resurrected_by_a_third_duplicate(ecs_world):
    row = ecs_world.get(Resource, 1)
    accepted, rejected = service.save_readings(ecs_world, [row], {row.resource_id: [point(10), point(20), point(10)]}, NOW)
    assert not accepted and rejected >= 1
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_existing_conflicting_timestamp_is_not_overwritten(ecs_world):
    row = ecs_world.get(Resource, 1)
    sample(ecs_world, value=10)
    accepted, rejected = service.save_readings(ecs_world, [row], {row.resource_id: [point(20)]}, NOW)
    assert not accepted and rejected == 1
    assert ecs_world.scalar(select(EcsMetricSample)).value == 10


def test_real_history_aggregation_gaps_zero_boundary_and_scope(ecs_world):
    start = NOW-timedelta(minutes=20)
    for value, at in [(10, start), (20, start+timedelta(minutes=1)), (0, start+timedelta(minutes=10)),
                      (99, NOW)]:
        sample(ecs_world, value=value, at=at)
    # 即便历史脏数据错误地引用同一 resource_ref，租户、项目仍必须一致。
    sample(ecs_world, tid=2, pid=21, value=99, at=start+timedelta(minutes=2))
    query = service.HistoryQuery.create(start, NOW, 300, ["cpu_util"])
    data = service.history(ecs_world, reader(), 1, query)
    points = data.series[0].points
    assert [(p.average, p.maximum, p.minimum, p.sample_count) for p in points] == [
        (15, 20, 10, 2), (None, None, None, 0), (0, 0, 0, 1), (None, None, None, 0)]
    assert data.series[0].source == "PROMETHEUS"


def test_different_sources_are_separate_series(ecs_world):
    sample(ecs_world, value=12, source="AOM")
    sample(ecs_world, value=35, source="PROMETHEUS")
    data = service.history(ecs_world, reader(), 1,
        service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 60, ["cpu_util"]))
    assert [s.source for s in data.series] == ["AOM", "PROMETHEUS"]
    assert [s.points[-1].average for s in data.series] == [12, 35]
    assert any("分开" in warning for warning in data.warnings)
    assert any("AOM" in warning and "已停用" in warning for warning in data.warnings)


def test_no_history_is_not_filled_from_latest_snapshot(ecs_world):
    snapshot(ecs_world)
    data = service.history(ecs_world, reader(), 1,
        service.HistoryQuery.create(NOW-timedelta(hours=1), NOW, 300))
    assert data.warnings
    assert all(p.average is None and p.sample_count == 0 for s in data.series for p in s.points)
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_released_instance_history_remains_available_by_authorized_local_id(ecs_world):
    sample(ecs_world)
    ecs_world.get(Resource, 1).lifecycle = "DELETED"
    ecs_world.commit()
    data = service.history(ecs_world, reader(), 1, service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 60))
    assert any(point.sample_count == 1 for series in data.series for point in series.points)
    assert not data.instance.included
    assert 1 not in {item.id for item in service.list_instances(ecs_world, reader()).items}


def test_history_and_export_scope_never_leak_another_tenant_or_non_ecs(ecs_world):
    query = service.HistoryQuery.create(NOW-timedelta(hours=1), NOW, 300)
    with pytest.raises(BizError) as error:
        service.history(ecs_world, reader(), 3, query)
    assert error.value.http_status == 404
    ecs_world.get(Resource, 1).resource_type = "dcs_instance"
    ecs_world.flush()
    with pytest.raises(BizError):
        service.history(ecs_world, reader(), 1, query)


def test_xlsx_contains_same_gaps_and_formula_safe_names(ecs_world):
    ecs_world.get(Resource, 1).name = "=SUM(1,1)"
    sample(ecs_world, value=0)
    data = service.history(ecs_world, reader(), 1,
        service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 60, ["cpu_util"]))
    content = export_metrics(data)
    assert content[:2] == b"PK"
    wb = load_workbook(io.BytesIO(content))
    assert wb.sheetnames == ["查询条件", "指标趋势", "最近采样"]
    ws = wb["指标趋势"]
    assert ws.max_row == 6 and ws["A2"].value == "=SUM(1,1)" and ws["A2"].data_type == "s"
    assert ws["H2"].value is None and ws["H6"].value == 0
    assert ws["K2"].value == 0 and ws["K6"].value == 1
    assert ws["L1"].value == "时间语义" and ws["L6"].value == "时间语义未记录"
    for basis, label in (("evaluation", "PromQL 求值时间"), ("source_sample", "上游点或配置的源时间")):
        for series in data.series:
            series.timestamp_semantics = basis
        data.instance.latest["cpu_util"].timestamp_semantics = basis
        typed = load_workbook(io.BytesIO(export_metrics(data)))
        assert typed["指标趋势"]["L6"].value == label
        assert typed["指标趋势"]["H6"].value == 0
        assert typed["最近采样"]["C1"].value == "结果时间"
        assert typed["最近采样"]["G2"].value == label


@pytest.mark.parametrize("args", [
    (NOW.replace(tzinfo=None)-timedelta(hours=1), NOW, 300),
    (NOW, NOW, 300), (NOW-timedelta(days=32), NOW, 3600),
    (NOW-timedelta(days=7), NOW, 60), (NOW-timedelta(hours=1), NOW, 120),
    (NOW, NOW+timedelta(hours=1), 300),
])
def test_history_invalid_ranges_are_bounded_without_cloud_queries(ecs_world, args):
    with pytest.raises(BizError):
        service.HistoryQuery.create(*args)


def test_ip_association_excludes_duplicates_even_when_only_one_is_selected(ecs_world):
    one = ecs_world.get(Resource, 1)
    other = Resource(id=4, tenant_id=1, project_ref=11, service="ecs", resource_type="ecs_server",
        resource_id="another-id", name="另一个实例", ip=one.ip)
    ecs_world.add(other)
    ecs_world.flush()
    assert ip_index([one, other]) == {}


def test_metrics_collector_queries_only_selected_local_machine_and_never_ecs_inventory(ecs_world, monkeypatch):
    from app.collectors import ecs_metrics as module
    setting = policy(selection={"mode": "selected", "resource_ids": [1]}, metric_keys=["cpu_util"])
    ecs_world.add(SysSetting(key="collection_policy:1:ecs_server", value=setting))
    ecs_world.commit()
    calls = []

    def collect(ctx, ids, index, **kwargs):
        calls.append((ids, index, kwargs))
        return EcsReadingsResult({"shared-cloud-id": [point(0)]}, "PROMETHEUS", {"status": "ready"})

    monkeypatch.setattr(module, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=ecs_world.get(HcsAccount, 1), db=ecs_world)
    observations = EcsMetricsCollector().collect(ctx)
    # 同租户另一项目复用了 IP，指标源没有项目维度时不能猜关联。
    assert calls[0][:2] == (["shared-cloud-id"], {})
    assert len(observations) == 1 and observations[0].metrics["cpu_util"] == 0
    assert observations[0].attrs["ecs_monitoring"]["latest"]["cpu_util"]["observed_at"] == point().observed_at.isoformat()
    assert ecs_world.scalar(select(func.count()).select_from(EcsMetricSample)) == 1
    assert all(s.tenant_id == 1 and s.project_ref == 11 for s in ecs_world.scalars(select(EcsMetricSample)))


def test_selected_machine_does_not_query_addresses_of_same_cloud_id_in_other_project(ecs_world, monkeypatch):
    from app.collectors import ecs_metrics as module

    db = ecs_world
    db.get(Resource, 2).ip = "10.0.0.2"
    setting = policy(selection={"mode": "selected", "resource_ids": [1]}, metric_keys=["cpu_util"])
    db.add(SysSetting(key="collection_policy:1:ecs_server", value=setting))
    db.commit()
    requests = []

    def collect(ctx, ids, index, **kwargs):
        requests.append((ids, index))
        return EcsReadingsResult({"shared-cloud-id": [point(0)]}, "PROMETHEUS", {"status": "ready"})

    monkeypatch.setattr(module, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db)
    assert len(EcsMetricsCollector().collect(ctx)) == 1
    assert requests == [(["shared-cloud-id"], {"10.0.0.1": "shared-cloud-id"})]


@pytest.mark.parametrize("change", ["empty_selection", "add_selection", "revert_selection", "cadence"])
def test_policy_save_during_metric_io_discards_all_points_before_persistence(ecs_world, monkeypatch, change):
    from app.collectors import ecs_metrics as module
    from app.services import collection_policy

    db = ecs_world
    initial = CollectionPolicy(selection={"mode": "selected", "resource_ids": [1]}, metric_keys=["cpu_util"])
    collection_policy.save(db, 1, "ecs_server", initial, "initial")
    db.commit()
    requested = []

    def collect(ctx, ids, index, **kwargs):
        requested.append(ids)
        updated = initial.model_copy(deep=True)
        if change == "add_selection":
            updated.selection.resource_ids.append(2)
        elif change == "cadence":
            updated.metrics.interval_sec = 600
        else:
            updated.selection.resource_ids = []
        collection_policy.save(db, 1, "ecs_server", updated, "concurrent-editor")
        db.commit()
        if change == "revert_selection":
            collection_policy.save(db, 1, "ecs_server", initial, "concurrent-editor")
            db.commit()
        return EcsReadingsResult({"shared-cloud-id": [point(90)]}, "PROMETHEUS", {"status": "ready"})

    monkeypatch.setattr(module, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db)
    assert EcsMetricsCollector().collect(ctx) == []
    assert requested == [["shared-cloud-id"]]
    assert ctx.stats["metrics_skipped"] == "configuration_changed"
    assert db.scalar(select(func.count()).select_from(EcsMetricSample)) == 0
    assert all("ecs_monitoring" not in resource.attrs for resource in db.scalars(select(Resource)))


def test_inspection_uses_fresh_local_samples_without_querying_metric_source(ecs_world):
    row, tenant, setting = snapshot(ecs_world)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=tenant, db=ecs_world, params={"_collection_policy": setting})
    values, source = EcsStatusCollector._metrics(ctx, [row.resource_id], {})
    assert source == "PROMETHEUS" and values[row.resource_id]["cpu_util"] == 40
    assert values[row.resource_id]["cpu_util_observed_at"] == (NOW-timedelta(seconds=60)).isoformat()
    row.attrs["ecs_monitoring"]["latest"]["cpu_util"]["observed_at"] = (NOW-timedelta(hours=1)).isoformat()
    assert "cpu_util" not in EcsStatusCollector._metrics(ctx, [row.resource_id], {})[0].get(row.resource_id, {})


def test_api_read_only_permissions_and_xlsx_export(ecs_world):
    user = reader().user
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: ecs_world
    app.dependency_overrides[get_current_user] = lambda: user
    params = {"start_at": (NOW-timedelta(minutes=5)).isoformat(), "end_at": NOW.isoformat(), "interval_sec": 60}
    with TestClient(app) as client:
        assert client.get("/api/v1/ecs/instances").status_code == 200
        assert client.get("/api/v1/ecs/instances/1/history", params=params).status_code == 200
        assert client.get("/api/v1/ecs/instances/3/history", params=params).status_code == 404
        assert client.get("/api/v1/ecs/instances/1/export", params=params).status_code == 403
        user.permissions.append("ledger:export")
        response = client.get("/api/v1/ecs/instances/1/export", params=params)
        assert response.status_code == 200 and response.content[:2] == b"PK"
        user.permissions = []
        assert client.get("/api/v1/ecs/instances").status_code == 403


@pytest.mark.parametrize("change", ["source", "master", "selection"])
def test_configuration_changed_during_sampling_discards_results(ecs_world, monkeypatch, change):
    from app.collectors import ecs_metrics as module
    db = ecs_world
    db.add(CollectSwitch(tenant_id=1, resource_type="ecs_server", enabled=True))
    db.commit()

    def collect(ctx, ids, index, **kwargs):
        if change == "source":
            db.execute(update(HcsAccount).where(HcsAccount.id == 1).values(prometheus_url="https://changed.invalid"))
        elif change == "master":
            db.execute(update(CollectSwitch).where(CollectSwitch.tenant_id == 1).values(enabled=False))
        else:
            db.execute(update(Resource).where(Resource.id == 1).values(monitored=False))
        db.commit()
        return EcsReadingsResult({"shared-cloud-id": [point(90)]}, "PROMETHEUS", {"status": "ready"})

    monkeypatch.setattr(module, "collect_readings", collect)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db)
    assert EcsMetricsCollector().collect(ctx) == []
    assert db.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_inventory_refresh_keeps_metrics_committed_after_a_stale_session_read(ecs_world):
    from app.ledger.fetchers import LedgerRecord
    from app.ledger.sync import apply_snapshot
    db = ecs_world
    row = db.get(Resource, 1)
    assert row.attrs == {}
    with Session(db.get_bind()) as other:
        other.execute(update(Resource).where(Resource.id == 1).values(attrs={"ecs_monitoring": {"keep": "latest"}}))
        other.commit()
    apply_snapshot(db, "ecs_server", [LedgerRecord(row.resource_id, row.name, attrs={"os": "current"})],
                   NOW, tenant_id=1, project_ref=11)
    assert row.attrs == {"ecs_monitoring": {"keep": "latest"}, "os": "current"}


def test_late_inspection_does_not_overwrite_newer_metric_snapshot(ecs_world):
    from app.collectors.base import Observation
    from app.services.pipeline import process_observations
    db = ecs_world
    row, _tenant, setting = snapshot(db)
    row.metrics = {"cpu_util": 12, "mem_util": 24, "disk_util": 36}
    db.commit()
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11,  tenant=db.get(HcsAccount, 1), db=db,
        params={"_collection_phase": "inspection", "_collection_policy": setting})
    process_observations(db, "ecs", [Observation("ecs_server", row.resource_id, row.name, "instance_status", {},
        status_raw="ACTIVE", metrics={"cpu_util": None, "mem_util": 99, "disk_util": 99})], ctx, NOW)
    assert row.metrics == {"cpu_util": 12, "mem_util": 24, "disk_util": 36}


def test_source_or_mapping_change_starts_a_new_rule_sample_sequence(ecs_world):
    from app.collectors.base import Observation
    from app.services.pipeline import process_observations
    db = ecs_world
    db.add(AlarmRule(id="ecs-source-sequence", name="CPU", resource_type="ecs_server", check_item="cpu", kind="threshold",
        expr={"value": "cpu_util", "levels": [{"threshold": 80, "severity": "P2"}]}, for_count=2,
        recover={"when": "cpu_util < 70", "for_count": 2}))
    db.commit()
    row = db.get(Resource, 1)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db, params={"_collection_phase": "inspection"})

    def emit(signature, seconds, value=95):
        process_observations(db, "ecs", [Observation("ecs_server", row.resource_id, row.name, "cpu", {"cpu_util": value},
            metric_observed_at=NOW-timedelta(seconds=seconds), metric_config_signature=signature)], ctx, NOW)

    emit("source-a", 240)
    emit("source-b", 180)
    assert db.scalar(select(AlarmEvent)) is None
    assert db.scalar(select(EvalState)).data["hits"] == 1
    emit("source-b", 120)
    assert db.scalar(select(AlarmEvent)).status == "FIRING"
    emit("source-b", 120, 10)
    assert db.scalar(select(AlarmEvent)).status == "FIRING"
    emit("new-mapping", 60, 10)
    state = db.scalar(select(EvalState))
    assert state.data["_ecs_metric_signature"] == "new-mapping" and state.data["ok_hits"] == 1
    assert db.scalar(select(AlarmEvent)).status == "FIRING"


@pytest.mark.parametrize("selection", ["all", "critical"])
def test_state_failure_does_not_block_fresh_metric_alerts_or_fabricate_status(ecs_world, selection):
    from app.hcs import HcsError
    from app.services.pipeline import process_observations
    db = ecs_world
    row, tenant, setting = snapshot(db, value=95, selection={"mode": selection})
    db.add(SysSetting(key="collection_policy:1:ecs_server", value=setting))
    db.add(AlarmRule(id="ecs-state-failure-cpu", name="CPU", resource_type="ecs_server", check_item="cpu", kind="threshold",
        expr={"value": "cpu_util", "levels": [{"threshold": 80, "severity": "P2"}]}, for_count=1, recover={}))
    db.commit()

    def failed_get(*_args, **_kwargs):
        raise HcsError(503, "SYNTHETIC.STATE", "synthetic state failure")

    ctx = RunContext(client=SimpleNamespace(get=failed_get), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=tenant, db=db, params={"_collection_phase": "inspection", "_collection_policy": setting})
    observations = EcsStatusCollector().collect(ctx)
    assert ctx.errors and not any(o.check_item == "instance_status" for o in observations)
    assert next(o for o in observations if o.check_item == "cpu").facts["cpu_util"] == 95
    process_observations(db, "ecs", observations, ctx, NOW)
    assert db.scalar(select(AlarmEvent)).status == "FIRING"
    assert row.status_raw == "ACTIVE" and "ecs_state_checked_at" not in row.attrs


@pytest.mark.parametrize("metric", ["cpu_util", "mem_util", "disk_util"])
def test_new_aom_readings_rejected_without_removing_existing_history(ecs_world, metric):
    db = ecs_world
    sample(db, source="AOM", metric=metric, value=0)
    row = db.get(Resource, 1)
    accepted, rejected = service.save_readings(db, [row], {row.resource_id: [
        point(99, at=NOW-timedelta(seconds=30), source="AOM", metric=metric)]}, NOW)
    db.commit()
    assert not accepted and rejected == 1
    saved = list(db.scalars(select(EcsMetricSample)))
    assert len(saved) == 1 and saved[0].source == "AOM" and saved[0].value == 0
    assert saved[0].observed_at == NOW-timedelta(seconds=60)


@pytest.mark.parametrize("bad_source", ["AOM", "CES", None])
@pytest.mark.parametrize("metric", ["cpu_util", "mem_util", "disk_util"])
def test_each_cached_point_requires_current_source_even_with_valid_wrapper_and_signature(ecs_world, bad_source, metric):
    db = ecs_world
    row, tenant, setting = snapshot(db, value=95)
    attrs = deepcopy(row.attrs)
    attrs["ecs_monitoring"]["latest"][metric]["source"] = bad_source
    row.attrs = attrs
    db.commit()
    result = out(db, row, tenant, setting)
    assert result.latest[metric].value is None and result.latest[metric].observed_at is None
    assert result.latest[metric].state == "NO_DATA"
    assert all(result.latest[key].value == 95 for key in service.METRICS if key != metric)
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=tenant, db=db, params={"_collection_policy": setting})
    data, source = EcsStatusCollector._metrics(ctx, [row.resource_id], {})
    assert source == "PROMETHEUS" and metric not in data[row.resource_id]
    assert all(data[row.resource_id][key] == 95 for key in service.METRICS if key != metric)


@pytest.mark.parametrize("returned_source", ["AOM", "CES", "NONE"])
def test_metrics_collector_rejects_result_source_different_from_current_configuration(
        ecs_world, monkeypatch, returned_source):
    from app.collectors import ecs_metrics as module
    db = ecs_world
    monkeypatch.setattr(module, "collect_readings", lambda *_args, **_kwargs: EcsReadingsResult(
        {"shared-cloud-id": [point(95, source=returned_source)]}, returned_source, {"status": "ready"}))
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db)
    assert EcsMetricsCollector().collect(ctx) == []
    assert ctx.stats["metrics_skipped"] == "source_mismatch" and ctx.errors
    assert db.scalar(select(func.count()).select_from(EcsMetricSample)) == 0
    assert db.get(Resource, 1).attrs == {} and db.get(Resource, 1).metrics == {}


def test_metrics_collector_does_not_reuse_aom_points_inside_valid_prometheus_snapshot(ecs_world, monkeypatch):
    from app.collectors import ecs_metrics as module
    db = ecs_world
    row, tenant, _setting = snapshot(db, value=95)
    attrs = deepcopy(row.attrs)
    for item in attrs["ecs_monitoring"]["latest"].values():
        item["source"] = "AOM"
    row.attrs = attrs
    db.commit()
    monkeypatch.setattr(module, "collect_readings", lambda *_args, **_kwargs: EcsReadingsResult(
        {}, "PROMETHEUS", {"status": "no_data"}))
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=tenant, db=db)
    observations = EcsMetricsCollector().collect(ctx)
    assert observations
    assert all(not observation.attrs["ecs_monitoring"]["latest"] for observation in observations)
    assert all(observation.metrics["cpu_util"] is None for observation in observations)
    assert db.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_aom_history_and_xlsx_keep_original_source_zero_gaps_and_disabled_warning(ecs_world):
    db = ecs_world
    sample(db, source="AOM", value=0)
    sample(db, source="PROMETHEUS", value=12)
    query = service.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 60, ["cpu_util"])
    data = service.history(db, reader(), 1, query)
    aom_series = next(series for series in data.series if series.source == "AOM")
    assert [(p.average, p.sample_count) for p in aom_series.points] == [(None, 0)]*4+[(0, 1)]
    assert any("AOM" in warning and "已停用" in warning and "历史" in warning for warning in data.warnings)
    assert data.instance.latest["cpu_util"].value is None
    workbook = load_workbook(io.BytesIO(export_metrics(data)))
    rows = list(workbook["指标趋势"].iter_rows(min_row=2, values_only=True))
    aom_rows = [row for row in rows if row[3] == "AOM（已停用，仅历史）"]
    assert len(aom_rows) == 5 and [row[7] for row in aom_rows] == [None]*4+[0]
    assert [row[10] for row in aom_rows] == [0]*4+[1]
    assert any(row[3] == "PROMETHEUS" for row in rows)
    conditions = dict(workbook["查询条件"].iter_rows(min_row=2, values_only=True))
    assert "AOM 已停用" in conditions["说明"]
    assert not any(cell.data_type == "f" for sheet in workbook for row in sheet for cell in row)
    assert {row.source for row in db.scalars(select(EcsMetricSample))} == {"AOM", "PROMETHEUS"}


@pytest.mark.parametrize("corruption", ["wrapper_source", "point_source", "signature", "explicit_aom"])
def test_inspection_clears_current_aom_values_and_never_evaluates_them_even_with_fresh_time(ecs_world, corruption):
    from app.services.pipeline import process_observations
    db = ecs_world
    row, tenant, setting = snapshot(db, value=99)
    attrs = deepcopy(row.attrs)
    if corruption == "wrapper_source":
        attrs["ecs_monitoring"]["source"] = "AOM"
    elif corruption == "point_source":
        for item in attrs["ecs_monitoring"]["latest"].values():
            item["source"] = "AOM"
    elif corruption == "signature":
        attrs["ecs_monitoring"]["config_signature"] = "old-aom-signature"
    else:
        tenant.metrics_source = "AOM"
    row.attrs = attrs
    row.metrics = {"cpu_util": 99, "mem_util": 99, "disk_util": 99, "disk_mount": "/old-aom"}
    db.add(SysSetting(key="collection_policy:1:ecs_server", value=setting))
    db.add(AlarmRule(id="old-aom-must-not-fire", name="CPU", resource_type="ecs_server", check_item="cpu",
        kind="threshold", expr={"value": "cpu_util", "levels": [{"threshold": 80, "severity": "P1"}]},
        for_count=1, recover={}))
    sample(db, source="AOM", value=99)
    def native_get(service_name, path, **_kwargs):
        assert service_name == "ecs" and path == "/v1/{project_id}/cloudservers/detail"
        return {"servers": [{"id": row.resource_id, "name": row.name, "status": "ACTIVE"}], "count": 1}
    ctx = RunContext(client=SimpleNamespace(get=native_get), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=tenant, db=db,
        params={"_collection_phase": "inspection", "_collection_policy": setting})
    observations = EcsStatusCollector().collect(ctx)
    assert all(value is None for item in observations if item.check_item in ("cpu", "memory", "disk")
               for key, value in item.facts.items() if key != "disk_mount")
    process_observations(db, "ecs", observations, ctx, NOW)
    assert all(row.metrics[key] is None for key in ("cpu_util", "mem_util", "disk_util", "disk_mount"))
    assert db.scalar(select(AlarmEvent)) is None
    assert not [state for state in db.scalars(select(EvalState)) if "_ecs_metric_observed_at" in state.data]
    assert db.scalar(select(EcsMetricSample)).source == "AOM"


def test_metrics_collector_rejects_aom_points_even_when_result_claims_prometheus(ecs_world, monkeypatch):
    from app.collectors import ecs_metrics as module
    db = ecs_world
    monkeypatch.setattr(module, "collect_readings", lambda *_args, **_kwargs: EcsReadingsResult(
        {"shared-cloud-id": [point(99, source="AOM"), point(0, metric="mem_util")]},
        "PROMETHEUS", {"status": "partial"}))
    ctx = RunContext(client=SimpleNamespace(), account="ecs-1", project_id="project-11", tenant_id=1,
        project_ref=11, tenant=db.get(HcsAccount, 1), db=db)
    observations = EcsMetricsCollector().collect(ctx)
    rows = list(db.scalars(select(EcsMetricSample)))
    assert len(rows) == 1 and rows[0].metric == "mem_util" and rows[0].source == "PROMETHEUS" and rows[0].value == 0
    assert ctx.stats["rejected_samples"] == 1 and ctx.errors
    assert observations[0].metrics["cpu_util"] is None and observations[0].metrics["mem_util"] == 0
    assert set(observations[0].attrs["ecs_monitoring"]["latest"]) == {"mem_util"}
