"""跨页面监控口径：无数据不等于健康，原生样本不能永久当作当前值。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.collectors.base import Observation, RunContext
from app.models import AlarmEvent, AlarmRule, Resource
from app.reports import builder
from app.rules.evaluator import evaluate_rule
from app.rules.model import Rule
from app.schemas.resource import ResourcePatch
from app.services import dashboard_service
from app.services.metric_visibility import current_metrics
from app.services.pipeline import process_observations

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def native_resource(kind, *, age=0, source=None):
    service = "DCS" if kind == "dcs_instance" else "GAUSSDB"
    meta = {"source": source or service, "observed_at": (NOW - timedelta(seconds=age)).isoformat(),
            "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": 900,
            "api_version": "v2" if service == "DCS" else "v3",
            "endpoint": "instances/statistic" if service == "DCS" else "volume-usage"}
    metrics = ({"memory_usage": 0, "used_memory_mb": 0, "max_memory_mb": 1024, "used_cpu_seconds": 12,
                "cpu_usage": 99, "connected_clients": 99} if service == "DCS" else
               {"disk_usage": 0, "disk_used_gb": 0, "disk_total_gb": 100, "cpu_usage": 99, "mem_usage": 98})
    return SimpleNamespace(resource_type=kind, attrs={"metrics_source": source or service,
                           f"{service.lower()}_monitoring": meta}, metrics=metrics)


@pytest.mark.parametrize("kind", ["dcs_instance", "gaussdb_instance"])
@pytest.mark.parametrize("age", [901, -31])
def test_current_values_reject_stale_and_future_native_snapshots(kind, age):
    resource = native_resource(kind, age=age)
    original = deepcopy(resource.metrics)
    assert all(value is None for value in current_metrics(resource, now=NOW).values())
    assert resource.metrics == original


@pytest.mark.parametrize("kind", ["dcs_instance", "gaussdb_instance"])
def test_fresh_native_zero_is_kept_but_unavailable_cpu_not_displayed(kind):
    visible = current_metrics(native_resource(kind), now=NOW)
    assert visible["memory_usage" if kind == "dcs_instance" else "disk_usage"] == 0
    assert visible["cpu_usage"] is None
    if kind == "gaussdb_instance":
        assert visible["mem_usage"] is None
    else:
        assert visible["connected_clients"] is None


@pytest.mark.parametrize("kind", ["dcs_instance", "gaussdb_instance"])
def test_native_source_without_timestamp_does_not_certify_current_metrics(kind):
    resource = native_resource(kind)
    resource.attrs = {"metrics_source": "DCS" if kind == "dcs_instance" else "GAUSSDB"}
    assert all(value is None for value in current_metrics(resource, now=NOW).values())


def test_empty_and_all_unknown_monitoring_has_no_health_score(mem_db):
    data = dashboard_service.summary(mem_db, now=NOW)
    assert data["health_score"] is None
    assert all(group["score"] is None for group in data["groups"])
    mem_db.add(Resource(service="dcs", resource_type="dcs_instance", resource_id="unknown", name="unknown",
                        health="UNKNOWN", monitored=True))
    mem_db.commit()
    data = dashboard_service.summary(mem_db, now=NOW)
    assert data["health_score"] is None
    assert data["kpis"]["unknown_resources"] == 1


def test_no_inspection_run_does_not_report_perfect_success(mem_db):
    report = builder.build_report(mem_db, "DAILY", NOW - timedelta(days=1), NOW)
    assert report["overview"]["run_success_rate"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_state_fact_cannot_fire_or_resolve_alarm(value):
    rule = Rule(id="state-number", name="数值条件", resource_type="test", check_item="value", kind="state",
                expr={"when": "value >= 90", "severity": "P2"}, recover={"when": "value != 90"})
    for active in (None, "P2"):
        state = {"hits": 2, "ok_hits": 2}
        decision = evaluate_rule(rule, {"value": value}, state, active)
        assert decision.action is None
        assert state == {"hits": 0, "ok_hits": 0}
        assert "无数据" in decision.message


def test_intermediate_numeric_overflow_is_unavailable_not_threshold_hit():
    rule = Rule(id="overflow", name="计算", resource_type="test", check_item="value", kind="state",
                expr={"when": "value * value > 90", "severity": "P2"})
    assert evaluate_rule(rule, {"value": 1e308}, {}, None).action is None


def test_missing_observation_is_not_rewritten_as_healthy(mem_db):
    mem_db.add(AlarmRule(id="native-state", name="状态", resource_type="dcs_instance", check_item="instance_status",
                        kind="state", expr={"when": "status == 'ERROR'", "severity": "P2"},
                        recover={"when": "status == 'RUNNING'"}))
    mem_db.commit()
    ctx = RunContext(client=SimpleNamespace(), account="test", project_id="p")
    observation = Observation("dcs_instance", "dcs", "cache", "instance_status", {"status": None})
    process_observations(mem_db, "dcs", [observation], ctx, NOW)
    resource = mem_db.query(Resource).one()
    assert resource.health == "UNKNOWN"
    assert resource.attrs["inspection_monitoring"]["unavailable_rules"] == 1
    observation.facts = {"status": "RUNNING"}
    process_observations(mem_db, "dcs", [observation], ctx, NOW + timedelta(seconds=60))
    assert resource.health == "OK"
    observation.facts = {"status": None}
    process_observations(mem_db, "dcs", [observation], ctx, NOW + timedelta(seconds=120))
    assert resource.health == "UNKNOWN"


def test_stale_inspection_is_not_freshened_by_inventory_timestamp(mem_db):
    mem_db.add(Resource(service="dcs", resource_type="dcs_instance", resource_id="old", name="old", health="OK",
        last_seen=NOW, attrs={"inspection_monitoring": {"evaluated_at": (NOW - timedelta(hours=1)).isoformat(),
                              "stale_after_sec": 900, "checked_rules": 1, "unavailable_rules": 0}}))
    mem_db.commit()
    assert dashboard_service.summary(mem_db, now=NOW)["health_score"] is None


def test_interrupted_collection_cannot_join_two_old_threshold_hits(mem_db):
    mem_db.add(AlarmRule(id="continuous", name="内存", resource_type="dcs_instance", check_item="memory",
        kind="threshold", expr={"value": "memory_usage", "levels": [{"severity": "P2", "threshold": 90}]},
        for_count=2, recover={"below": 80}))
    mem_db.commit()
    ctx = RunContext(client=SimpleNamespace(), account="test", project_id="p")
    observation = Observation("dcs_instance", "dcs", "cache", "memory", {"memory_usage": 95})
    process_observations(mem_db, "dcs", [observation], ctx, NOW)
    process_observations(mem_db, "dcs", [observation], ctx, NOW + timedelta(hours=3))
    assert mem_db.query(AlarmEvent).count() == 0
    process_observations(mem_db, "dcs", [observation], ctx, NOW + timedelta(hours=3, seconds=60))
    assert mem_db.query(AlarmEvent).count() == 1


@pytest.mark.parametrize("field,value", [("critical", None), ("monitored", None), ("monitored", "false"),
                                       ("critical", 1), ("expected_state", {"running": "false"}),
                                       ("expected_state", {"running": None})])
def test_monitoring_flags_do_not_coerce_invalid_values(field, value):
    with pytest.raises(ValidationError):
        ResourcePatch.model_validate({field: value})
    assert ResourcePatch.model_validate({"expected_state": {"running": False}}).expected_state == {"running": False}
    assert ResourcePatch.model_validate({}).model_dump(exclude_unset=True) == {}


def test_manual_close_refreshes_health_without_claiming_cloud_recovery(mem_db, monkeypatch):
    from app.services import alarm_service

    row = Resource(service="dcs", resource_type="dcs_instance", resource_id="cache", name="cache", health="MAJOR",
        attrs={"inspection_monitoring": {"evaluated_at": NOW.isoformat(), "stale_after_sec": 900,
                                         "checked_rules": 1, "unavailable_rules": 0}})
    mem_db.add(row)
    mem_db.flush()
    alarm = AlarmEvent(resource_ref=row.id, fingerprint="close-health", rule_id="r", service="dcs",
        resource_type="dcs_instance", resource_name="cache", severity="P2", status="FIRING", title="fault",
        check_item="instance_status")
    mem_db.add(alarm)
    mem_db.commit()
    monkeypatch.setattr("app.services.inspection_health.utcnow", lambda: NOW)
    alarm_service.close(mem_db, alarm.id, "tester")
    assert row.health == "OK"
    assert alarm.status == "CLOSED"


@pytest.mark.parametrize("kind,metric", [("cce_workload", "backpressure_min"),
    ("dgc_batch_job", "run_minutes"), ("mrs_job", "run_minutes"), ("dgc_realtime_job", "bad_nodes")])
def test_other_runtime_metrics_share_freshness_and_explicit_missing_rules(kind, metric):
    raw = {metric: 0}
    row = SimpleNamespace(resource_type=kind, attrs={"metrics_monitoring": {
        "observed_at": NOW.isoformat(), "time_basis": "LOCAL_COLLECTION", "stale_after_sec": 900}}, metrics=raw)
    assert current_metrics(row, now=NOW)[metric] == 0
    assert current_metrics(row, now=NOW + timedelta(seconds=901))[metric] is None
    row.attrs["inspection_monitoring"] = {"checked_rules": 0, "unavailable_rules": 1}
    assert current_metrics(row, now=NOW)[metric] is None
    assert row.metrics == raw
