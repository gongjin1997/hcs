"""指南必须覆盖真实采集/台账输出，不以演示清单臆造指标。"""

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.v1.guides import router
from app.collectors.base import RunContext
from app.collectors.registry import TASKS
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.hcs.ratelimit import TokenBucket
from app.ledger.fetchers import FETCHERS
from app.ledger.sync import sync_ledger
from app.schemas.guide import ResourceGuide
from app.services.catalog import RESOURCE_TYPES
from app.services.pipeline import run_task
from app.services.resource_guides import (
    get_resource_guide,
    list_resource_guides,
    render_resource_guides_markdown,
    rule_fact_fields,
)
from app.services.tenancy import resolve_targets
from mock_hcs import tenants as mock_tenants

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("resource_type", RESOURCE_TYPES)
def test_every_catalog_type_has_concrete_guide(resource_type):
    guide = get_resource_guide(resource_type)
    assert guide is not None
    meta = RESOURCE_TYPES[resource_type]
    assert (guide.name, guide.service, guide.category, guide.ledger) == (
        meta["name"],
        meta["service"],
        meta["category"],
        meta["ledger"],
    )
    assert len(guide.guidance) >= 2 and len(guide.best_practices) >= 2
    assert len(set(guide.guidance)) == len(guide.guidance)
    assert guide.summary and guide.limitations
    assert ResourceGuide.model_validate(guide.model_dump()) == guide
    for fields in (guide.metrics, guide.facts, guide.attributes, guide.ledger_fields):
        assert len({f.key for f in fields}) == len(fields)
        assert all(f.key and f.label and f.meaning and f.source and f.optional for f in fields)
    for column in meta["columns"]:
        kind, key = column["key"].split(".", 1)
        fields = guide.metrics if kind == "metrics" else guide.attributes
        assert key in {f.key for f in fields}, (resource_type, column)
    if not guide.collection_tasks:
        assert guide.facts == [] and rule_fact_fields(resource_type) == []


def test_guides_are_ordered_and_do_not_share_mutable_lists():
    guides = list_resource_guides()
    assert [g.resource_type for g in guides] == list(RESOURCE_TYPES)
    before = guides[0].metrics[0].limitations.copy()
    guides[0].metrics[0].limitations.append("modified")
    guides[0].best_practices.clear()
    refreshed = get_resource_guide(guides[0].resource_type)
    assert refreshed.metrics[0].limitations == before and refreshed.best_practices
    assert get_resource_guide("not-a-resource") is None
    assert rule_fact_fields("not-a-resource") == []
    assert rule_fact_fields("ecs_server", "not-a-check") == []


def test_guides_cover_every_actual_collector_and_ledger_key(world, mem_db, mock_http, monkeypatch):
    # 测的是字段契约，不测限速；只替换桶等待，仍调用真实采集器和进程内 Mock HTTP。
    monkeypatch.setattr(TokenBucket, "acquire", lambda self, timeout=30: True)
    seen_types: set[str] = set()
    seen_metrics: dict[str, set[str]] = defaultdict(set)
    seen_checks: dict[str, set[str]] = defaultdict(set)
    # 字段契约测试明确使用本地合成 Prometheus；不依赖 AOM 映射或局点接入。
    for account in world.values():
        account.metrics_source = "PROMETHEUS"
        account.prometheus_url = f"http://mock/prom/{account.code}"
        monkeypatch.setitem(mock_tenants.BY_CODE[account.code], "prometheus_url", account.prometheus_url)
    mem_db.commit()
    for target in resolve_targets(mem_db):
        client = target.client(http=mock_http, mock_url="http://mock")
        inventory = sync_ledger(mem_db, client=client, target=target, types=["ecs_server"], trigger="MANUAL")
        assert inventory.status == "SUCCESS", inventory.error_summary
        sampled = run_task(mem_db, "ecs_metrics", client=client, target=target, trigger="MANUAL")
        assert sampled.status == "SUCCESS", sampled.error_summary
        ctx = RunContext(
            client=client,
            account=target.code,
            project_id=target.project_id,
            workspaces=target.workspaces,
            tenant_id=target.tenant_id,
            project_ref=target.project_ref,
            tenant=target.tenant,
            db=mem_db,
            params={"hcs_concurrency": 2},
        )
        for collector in TASKS.values():
            ctx.params = {"hcs_concurrency": 2}
            observations = collector.collect(ctx)
            # 租户可以没有某类资源；跨租户汇总仍须覆盖全部真实输出类型。
            for obs in observations:
                seen_types.add(obs.resource_type)
                guide = get_resource_guide(obs.resource_type)
                assert guide is not None
                assert collector.code in guide.collection_tasks
                metrics = {f.key for f in guide.metrics if f.availability != "not_collected"}
                attrs = {f.key for f in guide.attributes if f.availability != "not_collected"}
                facts = {f.key for f in rule_fact_fields(obs.resource_type, obs.check_item)}
                unavailable = {f.key for f in guide.metrics if f.availability == "not_collected"}
                assert set(obs.metrics) <= metrics | unavailable, (collector.code, set(obs.metrics) - metrics - unavailable)
                assert all(value is None for key, value in obs.metrics.items() if key in unavailable)
                assert set(obs.attrs) <= attrs, (collector.code, set(obs.attrs) - attrs)
                assert set(obs.facts) <= facts, (collector.code, obs.check_item, set(obs.facts) - facts)
                seen_metrics[obs.resource_type].update(set(obs.metrics) - unavailable)
                if collector.code == "ecs_metrics":
                    # 独立采样只更新证据，不是规则检查项；不能为凑齐目录伪造业务事实。
                    assert obs.check_item == "metric_sample" and obs.facts == {}
                else:
                    seen_checks[obs.resource_type].add(obs.check_item)
        assert ctx.errors == [], ctx.errors
        cache: dict = {}
        for resource_type, fetcher in FETCHERS.items():
            guide = get_resource_guide(resource_type)
            records = fetcher(client, cache)
            if records:
                seen_types.add(resource_type)
            attrs = {f.key for f in guide.attributes}
            capacity = {f.key for f in guide.ledger_fields}
            for record in records:
                assert set(record.attrs) <= attrs, (resource_type, set(record.attrs) - attrs)
                for key in ("vcpus", "memory_gb", "storage_gb"):
                    if getattr(record, key) is not None:
                        assert key in capacity, (resource_type, key)
    assert seen_types == set(RESOURCE_TYPES)
    # 缺少背压证据时不再伪造0；补充真实执行探针的正向合成样本证明该可选指标仍被采集。
    from datetime import timedelta

    from app.models import PodProbe
    from tests.test_cce_container_review_20261102 import NOW, Client, pod, run

    mem_db.add(PodProbe(tenant_id=11, name="guide-pressure", namespace="ns", metric="queue_depth", threshold=1,
                        for_minutes=1, recover_minutes=1))
    mem_db.commit()
    run(mem_db, [pod()], now=NOW, client=Client([pod()]))
    _, optional_observations = run(mem_db, [pod()], now=NOW + timedelta(minutes=1), client=Client([pod()]))
    assert any(o.check_item == "pod_backpressure" and o.facts.get("backpressure_min") == 1
               for o in optional_observations)
    for observation in optional_observations:
        seen_metrics[observation.resource_type].update(observation.metrics)
        seen_checks[observation.resource_type].add(observation.check_item)
    from tests.test_dataarts_p90_20261102 import collect as collect_batch
    from tests.test_dataarts_p90_20261102 import success

    baseline_rows, _, _ = collect_batch(monkeypatch, [success(index + 1, index + 1) for index in range(5)])
    assert baseline_rows[0].metrics["p90_minutes"] == 5
    seen_metrics["dgc_batch_job"].update(baseline_rows[0].metrics)
    for guide in list_resource_guides():
        # 反向也对照：没有真实输出的遗留展示字段必须明确 not_collected。
        assert seen_metrics[guide.resource_type] == {f.key for f in guide.metrics if f.availability != "not_collected"}
        assert seen_checks[guide.resource_type] == {check for f in guide.facts
                                                   if f.availability != "not_collected" for check in f.check_items}


def test_frontend_monitor_columns_have_metadata_including_unavailable_legacy_keys():
    source = (ROOT / "frontend/src/views/ledger/columns.ts").read_text()
    monitor = source.split("export const MONITOR_COLUMNS:", 1)[1]
    blocks = re.findall(r"^  (\w+): \[(.*?)^  \],", monitor, re.M | re.S)
    assert blocks
    for resource_type, body in blocks:
        guide = get_resource_guide(resource_type)
        assert guide is not None
        for key in re.findall(r"r\.metrics\.(\w+)", body):
            assert key in {f.key for f in guide.metrics}, (resource_type, key)
        for key in re.findall(r"r\.attrs\.(\w+)", body):
            assert key in {f.key for f in guide.attributes}, (resource_type, key)
    realtime = get_resource_guide("dgc_realtime_job")
    assert next(f for f in realtime.attributes if f.key == "restarts_24h").availability == "not_collected"
    batch = get_resource_guide("dgc_batch_job")
    assert next(f for f in batch.metrics if f.key == "p90_minutes").availability == "derived"
    p90_fact = next(f for f in batch.facts if f.key == "p90_minutes")
    assert "缺失时为空" in p90_fact.meaning
    assert "默认" not in p90_fact.meaning and "回退" not in p90_fact.meaning


def test_rule_fact_context_is_exact_and_default_expressions_are_explained():
    rules = yaml.safe_load((ROOT / "backend/app/rules/default_rules.yaml").read_text())
    for rule in rules:
        fields = rule_fact_fields(rule["resource_type"], rule["check_item"])
        assert fields, rule["id"]
        keys = {f.key for f in fields}
        expressions = [rule["expr"].get(key) for key in ("when", "value", "value_field")]
        expressions.append((rule.get("recover") or {}).get("when"))
        for expr in expressions:
            if isinstance(expr, str):
                names = {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}
                assert names - {"max", "min", "abs", "round"} <= keys, (rule["id"], names - keys)
    restart = {f.key for f in rule_fact_fields("cce_workload", "pod_restart")}
    assert {"restarts_10m", "restarts_1h", "critical", "expected_running", "status_duration_min"} <= restart
    assert not {"pods", "ready_pods", "restarts_24h", "oom_1h"} & restart
    assert "node_status" not in {f.key for f in rule_fact_fields("dgc_realtime_job", "job_status")}
    assert "cpu_util" not in {f.key for f in rule_fact_fields("ecs_server", "disk")}


@pytest.fixture()
def guide_client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(BizError)
    async def biz_error(_, error):
        return JSONResponse(
            status_code=error.http_status, content={"code": error.code, "message": error.message, "data": error.data}
        )

    with TestClient(app) as client:
        yield client, app


def test_guide_api_requires_login_and_no_resource_permission_or_tenant_scope(guide_client):
    client, app = guide_client
    for path in ("/api/v1/guides", "/api/v1/guides/ecs_server"):
        response = client.get(path)
        assert response.status_code == 401 and response.json()["code"] == 40101
    # 一个没有资源权限、没有租户授权的已登录用户也可以读文档。
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=999, username="docs-reader", display_name="文档读者", permissions=[], tenant_ids=[]
    )
    response = client.get("/api/v1/guides", headers={"X-Tenant-Id": "999999"})
    assert response.status_code == 200 and response.json()["code"] == 0
    data = response.json()["data"]
    assert [g["resource_type"] for g in data] == list(RESOURCE_TYPES)
    for guide in data:
        detail = client.get(f"/api/v1/guides/{guide['resource_type']}").json()["data"]
        assert detail == guide
        assert "tenant_id" not in detail and "resource_id" not in detail
    missing = client.get("/api/v1/guides/not-a-resource")
    assert missing.status_code == 404 and missing.json()["code"] == 40401


def test_generated_resource_document_matches_metadata():
    document = (ROOT / "docs/资源与指标指南.md").read_text()
    assert document == render_resource_guides_markdown()
    for code in RESOURCE_TYPES:
        assert f"（`{code}`）" in document
    for code, key in (
        ("ecs_server", "reboots_24h"),
        ("cce_workload", "oom_1h"),
        ("cce_workload", "backpressure_min"),
        ("dcs_instance", "max_memory_mb"),
    ):
        assert any(f.key == key for f in get_resource_guide(code).facts)


def test_qps_document_tracks_current_defaults_and_limits():
    from app.hcs.factory import DEFAULT_QPS

    document = (ROOT / "docs/采集与QPS最佳实践.md").read_text()
    assert {k for k, v in DEFAULT_QPS.items() if v == 10} == {"ces", "cce_k8s"}
    assert {v for v in DEFAULT_QPS.values()} == {5, 10}
    assert all(f"`{key}`" in document for key in DEFAULT_QPS)
    for text in (
        "0.1 QPS",
        "10 秒",
        "跨 API、调度等进程共享",
        "不补充初始突发额度",
        "不是华为官方配额表",
        "遵循 `Retry-After`",
        "包含首次发送",
        "expires_at",
        "MASTER_KEY 加密",
        "DCS 无任何 CES 路径",
        "PromClient",
        "TokenProvider",
    ):
        assert text in document
