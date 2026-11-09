"""ManageOne selection, persistence and display integration; synthetic data only."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.collectors.ecs import EcsStatusCollector
from app.core.crypto import encrypt
from app.metrics import effective
from app.metrics.ecs_readings import EcsReading, EcsReadingsResult
from app.models import EcsMetricSample, EvalState
from app.services import ecs_monitoring
from tests.test_ecs_collection_integration import NOW, ecs_scope, integrated, run, samples

__all__ = ["integrated"]


def native_config(**updates):
    return {"enabled": True, "endpoint": "https://manageone.integration.invalid", "username": "synthetic-northbound",
            "organization": "", "secret_cipher": encrypt("synthetic-northbound-password"), **updates}


@pytest.mark.parametrize("mode,enabled,configured,expected", [
    ("AUTO", True, True, "PROMETHEUS"),
    ("NONE", True, True, "NONE"),
    ("AOM", True, True, "NONE"),
    ("MANAGEONE", False, True, "NONE"),
    ("MANAGEONE", True, False, "NONE"),
    ("MANAGEONE", True, True, "MANAGEONE"),
])
def test_native_source_requires_explicit_tenant_choice_and_complete_enabled_config(mode, enabled, configured, expected):
    tenant = SimpleNamespace(metrics_source=mode, prometheus_url="https://prom.integration.invalid",
                             manageone_config=native_config(enabled=enabled))
    if not configured:
        tenant.manageone_config["secret_cipher"] = ""
    assert effective(tenant) == expected


def test_manageone_config_does_not_invalidate_other_sources(integrated):
    env = integrated
    old = ecs_monitoring.config_signature(env.tenant, env.policy, "PROMETHEUS")
    env.tenant.manageone_config = native_config()
    assert ecs_monitoring.config_signature(env.tenant, env.policy, "PROMETHEUS") == old


@pytest.mark.parametrize("field,value", [("endpoint", "https://another.integration.invalid"),
                                        ("username", "another-user"), ("organization", "another-org"),
                                        ("secret_cipher", "changed-cipher"), ("enabled", False),
                                        ("auth_type", "AKSK"), ("access_key", "different-ak"),
                                        ("sk_cipher", "changed-sk-cipher")])
def test_native_configuration_changes_invalidate_current_sample_signature(integrated, field, value):
    env = integrated
    env.tenant.metrics_source, env.tenant.manageone_config = "MANAGEONE", native_config()
    old = ecs_monitoring.config_signature(env.tenant, env.policy, "MANAGEONE")
    env.tenant.manageone_config = {**env.tenant.manageone_config, field: value}
    assert ecs_monitoring.config_signature(env.tenant, env.policy, "MANAGEONE") != old


def native_readings(_ctx, ids, _index, **kwargs):
    metrics = {key: {"status": "ready", "fresh_points": len(ids), "timestamp_semantics": "source_sample"}
               for key in kwargs["metrics"]}
    return EcsReadingsResult({sid: [EcsReading(key, value, NOW-timedelta(seconds=60), "MANAGEONE",
        dimensions={"semantics": "cloud_volume"} if key == "disk_util" else {}, period_sec=60)
        for key, value in (("cpu_util", 0), ("mem_util", 48), ("disk_util", 99))] for sid in ids}, "MANAGEONE",
        {"status": "ready", "metrics": metrics, "requested_resources": len(ids)})


def test_native_samples_reach_current_history_xlsx_and_status_without_filesystem_rule(integrated, monkeypatch):
    from io import BytesIO

    from openpyxl import load_workbook

    from app.collectors import ecs_metrics
    from app.collectors.base import RunContext
    from app.reports.ecs_excel import export_metrics

    env = integrated
    env.tenant.metrics_source, env.tenant.manageone_config = "MANAGEONE", native_config()
    env.db.commit()
    monkeypatch.setattr(ecs_metrics, "collect_readings", native_readings)
    result = run(env, "ecs_metrics")
    assert result.status == "SUCCESS", result.error_summary
    assert len(samples(env)) == 9 and {row.source for row in samples(env)} == {"MANAGEONE"}
    current = ecs_monitoring.list_instances(env.db, ecs_scope(env))
    assert all(item.metrics_source == "MANAGEONE" and item.diagnostic.status == "FRESH" for item in current.items)
    assert all(item.latest["cpu_util"].value == 0 and item.latest["disk_util"].value == 99 for item in current.items)
    assert all(item.latest["cpu_util"].timestamp_semantics == "source_sample" for item in current.items)
    status = run(env, "ecs_status")
    assert status.status == "SUCCESS", status.error_summary
    assert all(resource.metrics["disk_util"] == 99 for resource in env.resources.values())
    assert not any((state.data.get("_inspection_evidence") or {}).get("rule_id") == "R-ECS-006"
                   for state in env.db.scalars(select(EvalState)))
    ctx = RunContext(client=env.client, account=env.target.code, project_id=env.target.project_id,
                     project_ref=env.target.project_ref, tenant_id=env.tenant.id, tenant=env.tenant, db=env.db)
    observations = EcsStatusCollector().collect(ctx)
    assert all(obs.facts == {} for obs in observations if obs.check_item == "disk")
    assert any(obs.facts.get("cpu_util") == 0 for obs in observations if obs.check_item == "cpu")
    query = ecs_monitoring.HistoryQuery.create(NOW-timedelta(minutes=5), NOW, 300)
    history = ecs_monitoring.history(env.db, ecs_scope(env), next(iter(env.resources.values())).id, query)
    assert {series.source for series in history.series} == {"MANAGEONE"}
    assert next(series.label for series in history.series if series.metric == "disk_util") == "云硬盘使用率"
    assert any("不代表最满文件系统分区" in message for message in history.warnings)
    wb = load_workbook(BytesIO(export_metrics(history)), data_only=True)
    values = [cell.value for sheet in wb for row in sheet.iter_rows() for cell in row]
    assert "MANAGEONE" in values and "云硬盘使用率" in values and 99 in values
    wb.close()


def test_native_disabled_during_collection_cannot_commit_result(integrated, monkeypatch):
    from app.collectors import ecs_metrics

    env = integrated
    env.tenant.metrics_source, env.tenant.manageone_config = "MANAGEONE", native_config()
    env.db.commit()

    def changed(ctx, ids, index, **kwargs):
        result = native_readings(ctx, ids, index, **kwargs)
        env.tenant.manageone_config = {**deepcopy(env.tenant.manageone_config), "enabled": False}
        env.db.flush()
        return result

    monkeypatch.setattr(ecs_metrics, "collect_readings", changed)
    result = run(env, "ecs_metrics")
    assert result.status == "PARTIAL"
    assert result.stats["metrics_skipped"] == "configuration_changed"
    assert list(env.db.scalars(select(EcsMetricSample))) == []


@pytest.mark.parametrize("updates,expected", [
    ({}, "MANAGEONE"), ({"enabled": False}, "NONE"), ({"access_key": ""}, "NONE"),
    ({"sk_cipher": ""}, "NONE"), ({"auth_type": "unknown"}, "NONE"),
])
def test_aksk_source_selection_requires_its_own_credentials(updates, expected):
    tenant = SimpleNamespace(metrics_source="MANAGEONE", manageone_config=native_config(
        auth_type="AKSK", access_key="northbound-ak", sk_cipher=encrypt("northbound-sk")))
    tenant.manageone_config.update(updates)
    assert effective(tenant) == expected
