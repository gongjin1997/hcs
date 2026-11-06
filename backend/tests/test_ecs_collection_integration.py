"""Real ECS adapters + pipeline, isolated database and synthetic HTTP only."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.collectors import ecs, ecs_metrics
from app.core.config import get_settings
from app.hcs import HcsClient, ratelimit
from app.hcs.endpoints import EndpointResolver
from app.ledger import sync as inventory
from app.metrics import available, ecs_readings, mark
from app.metrics.prometheus import DEFAULT_QUERIES
from app.models import AlarmEvent, CollectSwitch, EcsMetricSample, EvalState, HcsAccount, HcsProject, Resource, SysSetting
from app.schemas.collection_policy import CollectionPolicy
from app.services import collection_policy, ecs_monitoring, pipeline, settings_service
from app.services.bootstrap import bootstrap
from app.services.tenancy import resolve_targets

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
MAPPING = {"namespace": "TEST.LocalGuestTelemetry", "dimension": "machine_ref", "metrics": {
    "cpu_util": "guest_cpu_percent", "mem_util": "guest_memory_percent", "disk_util": "guest_disk_percent"},
    "period": 60, "window_sec": 900}


class FakeTelemetry:
    def __init__(self):
        self.calls = []
        self.times = [NOW-timedelta(seconds=600), NOW-timedelta(seconds=60)]
        self.values = {"cpu_util": 96, "mem_util": 44, "disk_util": 33}
        self.fail_cpu = False
        self.forbid_metrics = False
        self.servers = [{"id": f"host-{name}", "name": f"Synthetic {name}", "status": "ACTIVE",
            "OS-EXT-STS:power_state": 1, "flavor": {"vcpus": 2, "ram": 4096}, "addresses": {
                "private": [{"addr": f"10.0.0.{index}", "OS-EXT-IPS:type": "fixed"}]}}
            for index, name in enumerate(("a", "b", "c"), 1)]

    def respond(self, request):
        self.calls.append(request)
        path = request.url.path
        if request.url.host == "native.integration.invalid":
            assert request.method == "GET"
            if path.endswith("/cloudservers/detail"):
                return httpx.Response(200, json={"servers": self.servers, "count": len(self.servers)})
            if path.endswith("/cloudvolumes/detail"):
                return httpx.Response(200, json={"volumes": [], "volumes_links": []})
            if "/cloudservers/" in path:
                assert request.url.params["expect_fields"] == "basic"
                sid = path.rsplit("/", 1)[-1]
                return httpx.Response(200, json={"server": next(row for row in self.servers if row["id"] == sid)})
            pytest.fail(f"Unexpected native route: {path}")
        assert not self.forbid_metrics, "status evaluation must never query HCS/Prometheus metrics"
        if request.url.host == "prom.integration.invalid":
            assert request.method == "GET" and path == "/api/v1/query"
            alias = next(key for key, query in DEFAULT_QUERIES.items() if query == request.url.params["query"])
            if self.fail_cpu and alias == "cpu_util":
                return httpx.Response(403, json={"error": "synthetic-denied"})
            rows = [{"metric": {"instance": row["addresses"]["private"][0]["addr"]+":9100",
                                **({"mountpoint": "/"} if alias == "disk_util" else {})},
                     "value": [max(self.times).timestamp(), str(self.values[alias])]} for row in self.servers]
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": rows}})
        body = json.loads(request.content) if request.method == "POST" else {}
        if path.endswith("/batch-query-metric-data"):
            assert request.method == "POST"
            aliases = {"cpu_util": "cpu_util", "mem_usedPercent": "mem_util", "disk_usedPercent": "disk_util"}
            return httpx.Response(200, json={"metrics": [{**row, "unit": "%", "datapoints": [
                {"timestamp": int(at.timestamp()*1000), "average": self.values[aliases[row["metric_name"]]]}
                for at in self.times]} for row in body["metrics"]]})
        if path.endswith("/metrics") and request.url.host == "ces.integration.invalid":
            assert request.method == "GET"
            return httpx.Response(200, json={"metrics": [{"namespace": "AGT.ECS", "metric_name": "disk_usedPercent",
                "dimensions": [{"name": "instance_id", "value": row["id"]}, {"name": "mount_point", "value": "/"}]}
                for row in self.servers], "meta_data": {}})
        pytest.fail(f"Unexpected telemetry route: {request.method} {path}")


def install_policy(env, **changes):
    value = CollectionPolicy.model_validate({**env.policy, **changes}).model_dump(mode="json")
    key = collection_policy.policy_key(env.target.tenant_id)
    row = env.db.scalar(select(SysSetting).where(SysSetting.key == key))
    if row is None:
        env.db.add(SysSetting(key=key, value=value))
    else:
        row.value = value
    env.db.commit()
    env.policy = value


@pytest.fixture
def integrated(mem_db, monkeypatch):
    settings = get_settings()
    for key, value in (("redis_url", None), ("hcs_ces_enabled", False), ("hcs_mock_url", None),
                       ("hcs_endpoints", {}), ("hcs_total_qps", 100000), ("env", "test")):
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(ratelimit, "_shared", {})
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(ecs_readings, "_now", lambda: clock.now)
    for module in (ecs, ecs_metrics, ecs_monitoring, pipeline, collection_policy, inventory):
        monkeypatch.setattr(module, "utcnow", lambda: clock.now)
    monkeypatch.setattr(settings_service, "get_collect", lambda _db: settings_service.CollectionSettings(
        hcs_qps={name: 1000 for name in ("ecs", "evs", "ces")}, hcs_concurrency=2))
    bootstrap(mem_db)
    tenant = HcsAccount(code="synthetic-ecs", name="Synthetic ECS", iam_endpoint="https://iam.integration.invalid",
                        domain_name="synthetic-ecs", metrics_source="PROMETHEUS", endpoints={"aom": "https://aom.integration.invalid"},
                        prometheus_url="https://prom.integration.invalid", enabled=True)
    mem_db.add(tenant)
    mem_db.flush()
    project = HcsProject(account_id=tenant.id, project_id="synthetic-project", project_name="Synthetic project",
                         region_id="test", external_domain="integration.invalid", enabled=True)
    mem_db.add(project)
    mem_db.commit()
    target = resolve_targets(mem_db)[0]
    telemetry = FakeTelemetry()
    with httpx.Client(transport=httpx.MockTransport(telemetry.respond)) as http:
        auth = SimpleNamespace(kind="TEST", apply=lambda *_: None, refresh=lambda: False,
                               access_key="synthetic-ak", _secret_key="synthetic-sk")
        endpoints = {"ecs": "https://native.integration.invalid", "evs": "https://native.integration.invalid",
                     "aom": "https://aom.integration.invalid", "ces": "https://ces.integration.invalid"}
        client = HcsClient(http, auth, EndpointResolver(endpoints, "test", "integration.invalid"), project.project_id,
                           tenant_id=tenant.id, project_ref=project.id, max_retries=0)
        env = SimpleNamespace(db=mem_db, tenant=tenant, target=target, telemetry=telemetry, clock=clock,
                              client=client, policy={}, resources={})
        install_policy(env, metric_keys=["cpu_util", "mem_util", "disk_util"])
        seeded = inventory.sync_ledger(mem_db, client=client, target=target, types=["ecs_server"], trigger="MANUAL")
        assert seeded.status == "SUCCESS", seeded.error_summary
        env.resources = {row.resource_id: row for row in mem_db.scalars(select(Resource))}
        assert set(env.resources) == {"host-a", "host-b", "host-c"}
        env.resources["host-a"].critical = True
        mem_db.commit()
        telemetry.calls.clear()
        yield env


def run(env, code, trigger="MANUAL"):
    return pipeline.run_task(env.db, code, client=env.client, target=env.target, trigger=trigger)


def samples(env):
    return list(env.db.scalars(select(EcsMetricSample).order_by(EcsMetricSample.observed_at, EcsMetricSample.resource_ref)))


def evaluate_without_metrics(env, trigger="MANUAL"):
    before = len(env.telemetry.calls)
    env.telemetry.forbid_metrics = True
    try:
        result = run(env, "ecs_status", trigger)
    finally:
        env.telemetry.forbid_metrics = False
    assert result.status == "SUCCESS", result.error_summary
    assert all(request.url.host == "native.integration.invalid" and request.method == "GET"
               for request in env.telemetry.calls[before:])
    return result


@pytest.mark.parametrize("source", ["PROMETHEUS", "CES"])
def test_real_adapters_save_only_actual_upstream_times_before_status_evaluation(integrated, monkeypatch, source):
    env = integrated
    env.tenant.metrics_source = source
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", source == "CES")
    env.db.commit()
    collected = run(env, "ecs_metrics")
    assert collected.status == "SUCCESS", collected.error_summary
    rows = samples(env)
    expected_times = {max(env.telemetry.times)} if source == "PROMETHEUS" else set(env.telemetry.times)
    assert len(rows) == 9*len(expected_times)
    assert {row.observed_at for row in rows} == expected_times
    assert all(row.source == source and row.unit == "%" and row.collected_at == NOW for row in rows)
    assert all(row.observed_at < row.collected_at and row.tenant_id == env.tenant.id
               and row.project_ref == env.target.project_ref for row in rows)
    assert all("/cloudservers/" not in request.url.path for request in env.telemetry.calls)
    assert env.db.scalar(select(func.count()).select_from(AlarmEvent)) == 0
    assert env.db.scalar(select(func.count()).select_from(EvalState)) == 0
    status = evaluate_without_metrics(env)
    assert status.api_calls == 1 and len(samples(env)) == len(rows)
    for resource in env.resources.values():
        assert resource.metrics["cpu_util"] == 96
        assert resource.attrs["ecs_monitoring"]["latest"]["cpu_util"]["observed_at"] == max(expected_times).isoformat()


def test_history_is_idempotent_and_repeated_status_does_not_repeat_metric_evidence(integrated):
    env = integrated
    assert run(env, "ecs_metrics").status == "SUCCESS"
    assert run(env, "ecs_metrics").status == "SUCCESS"
    assert len(samples(env)) == 9
    evaluate_without_metrics(env)
    previous = {row.fingerprint: deepcopy(row.data) for row in env.db.scalars(select(EvalState))
                if "_ecs_metric_observed_at" in row.data}
    assert previous
    for _ in range(3):
        evaluate_without_metrics(env)
    current = {row.fingerprint: row.data for row in env.db.scalars(select(EvalState))
               if "_ecs_metric_observed_at" in row.data}
    assert current == previous and len(samples(env)) == 9
    assert env.db.scalar(select(func.count()).select_from(AlarmEvent).where(AlarmEvent.rule_id == "R-ECS-004")) == 0
    for step in (1, 2):
        env.clock.now += timedelta(seconds=120)
        env.telemetry.times = [env.clock.now-timedelta(seconds=60)]
        assert run(env, "ecs_metrics").status == "SUCCESS"
        evaluate_without_metrics(env)
        alarms = list(env.db.scalars(select(AlarmEvent).where(AlarmEvent.rule_id == "R-ECS-004")))
        assert len(alarms) == (0 if step == 1 else 3)
    assert len(samples(env)) == 27


def test_inventory_sampling_and_evaluation_keep_independent_schedule_clocks(integrated):
    env = integrated
    assert run(env, "ecs_metrics", "SCHEDULE").status == "SUCCESS"
    before = len(env.telemetry.calls)
    assert run(env, "ecs_metrics", "SCHEDULE").status == "SKIPPED"
    assert len(env.telemetry.calls) == before
    evaluate_without_metrics(env, "SCHEDULE")
    before = len(env.telemetry.calls)
    assert run(env, "ecs_status", "SCHEDULE").status == "SKIPPED"
    assert len(env.telemetry.calls) == before
    env.clock.now += timedelta(seconds=120)
    evaluate_without_metrics(env, "SCHEDULE")
    before = len(env.telemetry.calls)
    assert run(env, "ecs_metrics", "SCHEDULE").status == "SKIPPED"
    assert len(env.telemetry.calls) == before
    env.clock.now += timedelta(seconds=180)
    env.telemetry.times = [env.clock.now-timedelta(seconds=60)]
    assert run(env, "ecs_metrics", "SCHEDULE").status == "SUCCESS"
    assert inventory.sync_ledger(env.db, client=env.client, target=env.target, types=["ecs_server"]).status == "SKIPPED"
    assert run(env, "ecs_metrics", "MANUAL").status == "SUCCESS"


@pytest.mark.parametrize("selection", ["selected", "critical"])
def test_selection_limits_sampling_and_uses_one_native_host_state_get(integrated, selection):
    env = integrated
    chosen = env.resources["host-a"]
    install_policy(env, selection={"mode": selection, "resource_ids": [chosen.id] if selection == "selected" else []})
    untouched = {}
    for sid, resource in env.resources.items():
        if sid != "host-a":
            resource.attrs, resource.metrics = {"sentinel": sid}, {"cpu_util": 17}
            untouched[sid] = (deepcopy(resource.attrs), deepcopy(resource.metrics), resource.lifecycle)
    env.db.commit()
    result = run(env, "ecs_metrics")
    assert result.status == "SUCCESS" and result.resource_total == 1
    assert {row.resource_ref for row in samples(env)} == {chosen.id}
    # Instant PromQL may return peer vectors; only selected local IDs may be saved.
    assert len(env.telemetry.calls) == 3
    assert all(request.url.host == "prom.integration.invalid" and request.method == "GET"
               for request in env.telemetry.calls)
    before = len(env.telemetry.calls)
    status = evaluate_without_metrics(env)
    requests = env.telemetry.calls[before:]
    assert status.resource_total == 1 and status.api_calls == len(requests) == 1
    assert requests[0].url.path == "/v1/synthetic-project/cloudservers/host-a"
    assert requests[0].url.params["expect_fields"] == "basic"
    for sid, snapshot in untouched.items():
        resource = env.resources[sid]
        assert (resource.attrs, resource.metrics, resource.lifecycle) == snapshot


@pytest.mark.parametrize("stage", ["ecs_metrics", "ecs_status"])
@pytest.mark.parametrize("gate", ["master", "stage"])
def test_manual_runs_bypass_cadence_but_still_honor_master_and_phase_gates(integrated, stage, gate):
    env = integrated
    if gate == "master":
        env.db.add(CollectSwitch(tenant_id=env.tenant.id, resource_type="ecs_server", enabled=False))
        env.db.commit()
    else:
        phase = "metrics" if stage == "ecs_metrics" else "inspection"
        install_policy(env, **{phase: {"enabled": False, "interval_sec": 300}})
    result = run(env, stage, "MANUAL")
    assert result.status == "SKIPPED" and env.telemetry.calls == [] and samples(env) == []


def test_stale_history_is_kept_without_requerying_or_emitting_fresh_rule_evidence(integrated):
    env = integrated
    assert run(env, "ecs_metrics").status == "SUCCESS"
    original = {(row.id, row.observed_at, row.value) for row in samples(env)}
    env.clock.now += timedelta(seconds=901)
    evaluate_without_metrics(env)
    assert {(row.id, row.observed_at, row.value) for row in samples(env)} == original
    assert all(resource.metrics["cpu_util"] is None for resource in env.resources.values())
    assert not [row for row in env.db.scalars(select(EvalState)) if "_ecs_metric_observed_at" in row.data]


def test_partial_cpu_failure_keeps_other_metrics_and_status_uses_only_saved_points(integrated):
    env = integrated
    env.telemetry.fail_cpu = True
    result = run(env, "ecs_metrics")
    assert result.status == "PARTIAL" and result.error_summary
    assert {row.metric for row in samples(env)} == {"mem_util", "disk_util"}
    assert {row.observed_at for row in samples(env)} == {max(env.telemetry.times)}
    evaluate_without_metrics(env)
    assert all(resource.metrics["cpu_util"] is None and resource.metrics["mem_util"] == 44
               and resource.metrics["disk_util"] == 33 for resource in env.resources.values())


@pytest.mark.parametrize("source", ["AUTO", "AOM"])
def test_legacy_aom_policy_is_ignored_without_database_rewrite_or_cloud_calls(integrated, source):
    env = integrated
    env.tenant.metrics_source = source
    env.tenant.prometheus_url = None
    old = {**env.policy, "aom": MAPPING}
    row = env.db.scalar(select(SysSetting).where(SysSetting.key == collection_policy.policy_key(env.tenant.id)))
    row.value = deepcopy(old)
    mark(env.tenant, "aom", True, "old successful probe", project_ref=999)
    env.db.commit()
    assert available(env.tenant, "aom") is False
    assert collection_policy.effective(env.db, env.tenant.id).aom is None
    result = run(env, "ecs_metrics")
    assert result.status == "SUCCESS", result.error_summary
    assert result.stats["metrics_source"] == "NONE" and samples(env) == []
    assert env.telemetry.calls == []
    env.db.refresh(row)
    assert row.value == old
    evaluate_without_metrics(env)
    assert all(resource.metrics["cpu_util"] is None for resource in env.resources.values())
    assert not [state for state in env.db.scalars(select(EvalState)) if "_ecs_metric_observed_at" in state.data]


def test_auto_with_legacy_aom_policy_uses_only_configured_prometheus(integrated):
    env = integrated
    env.tenant.metrics_source = "AUTO"
    row = env.db.scalar(select(SysSetting).where(SysSetting.key == collection_policy.policy_key(env.tenant.id)))
    row.value = {**env.policy, "aom": MAPPING}
    env.db.commit()
    result = run(env, "ecs_metrics")
    assert result.status == "SUCCESS" and result.stats["metrics_source"] == "PROMETHEUS"
    assert {row.source for row in samples(env)} == {"PROMETHEUS"}
    assert {row.observed_at for row in samples(env)} == {max(env.telemetry.times)}
    assert all(request.url.host == "prom.integration.invalid" for request in env.telemetry.calls)
