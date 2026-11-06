"""平台目录（services/catalog.py）是资源类型、服务、巡检任务的唯一来源；其余模块只能从它派生。

这些检查在新增 / 删除资源类型或任务时，提示漏改的位置，避免多处清单各自漂移。
"""
from pathlib import Path

import pytest

from app.collectors.registry import TASKS
from app.ledger.fetchers import FETCHERS
from app.ledger.sync import LEDGER_TASK
from app.rules.loader import load_yaml_rules
from app.services import bootstrap, catalog, collect_switch, inspection_evidence
from app.services.cleanup import CLEANUP_TASK
from app.services.resource_guides import get_resource_guide
from app.tools.probe import PROBES, TYPES
from app.workers.scheduler import RUNNABLE

ROOT = Path(__file__).resolve().parents[2]


def test_retired_services_are_gone_everywhere():
    assert not set(catalog.RETIRED_TYPES) & set(catalog.RESOURCE_TYPES)
    assert not set(catalog.RETIRED_SERVICES) & set(catalog.SERVICES)
    assert not set(catalog.RETIRED_TYPES) & set(FETCHERS)
    assert not set(catalog.RETIRED_SERVICES) & {meta["service"] for meta in catalog.RESOURCE_TYPES.values()}
    assert not {p.service for p in PROBES} & set(catalog.RETIRED_SERVICES)
    assert all(rule["resource_type"] not in catalog.RETIRED_TYPES for rule in load_yaml_rules())


def test_every_resource_type_belongs_to_a_known_service_and_category():
    categories = {c["code"] for c in catalog.CATEGORIES}
    for code, meta in catalog.RESOURCE_TYPES.items():
        assert meta["service"] in catalog.SERVICES, code
        assert meta["category"] in categories, code
    assert set(catalog.LEDGER_TYPES) | set(catalog.INSPECT_ONLY_TYPES) == set(catalog.RESOURCE_TYPES)


def test_task_codes_are_registered_once_and_agree_across_modules():
    specs = {t.code for t in catalog.TASK_SPECS}
    assert len(specs) == len(catalog.TASK_SPECS)
    assert set(TASKS) == set(catalog.COLLECTOR_TASK_CODES)
    assert specs == set(TASKS) | {LEDGER_TASK, CLEANUP_TASK}
    assert RUNNABLE == specs
    assert {row[0] for row in bootstrap.BUILTIN_TASKS} == specs
    for code, collector in TASKS.items():
        spec = catalog.TASK_BY_CODE[code]
        assert collector.service == spec.service, code


def test_task_resource_types_and_switch_mapping_are_derived_from_the_catalog():
    for spec in catalog.TASK_SPECS:
        assert set(spec.types) <= set(catalog.RESOURCE_TYPES), spec.code
    assert collect_switch.TASK_TYPES is catalog.TASK_TYPES
    assert inspection_evidence.TASK_CHECKS is catalog.TASK_CHECKS
    assert set(collect_switch.SWITCH_TYPES) == set(catalog.RESOURCE_TYPES)
    for rtype in catalog.RESOURCE_TYPES:
        guide = get_resource_guide(rtype)
        assert guide is not None
        assert guide.collection_tasks == catalog.TYPE_TASKS.get(rtype, []), rtype


def test_ledger_fetchers_cover_exactly_the_ledger_types_in_dependency_order():
    assert set(FETCHERS) == set(catalog.LEDGER_TYPES)
    order = list(FETCHERS)
    # 同步顺序有依赖：ECS 在 EVS / EIP 之前，VPC 在子网之前，ELB 在 EIP 之前
    assert order.index("ecs_server") < order.index("evs_volume")
    assert order.index("ecs_server") < order.index("eip") and order.index("elb") < order.index("eip")
    assert order.index("vpc") < order.index("vpc_subnet")


def test_probe_types_only_reference_catalog_types():
    for probe_code, types in TYPES.items():
        assert set(types) <= set(catalog.RESOURCE_TYPES), probe_code
    assert {p.code for p in PROBES} >= {"dataarts.instances", "dataarts.instance_detail"}
    assert all(p.code != "mrs.job_executions" for p in PROBES)


def test_default_rules_target_known_resource_types():
    known = set(catalog.RESOURCE_TYPES) | set(catalog.PLATFORM_TYPES)
    ids = [rule["id"] for rule in load_yaml_rules()]
    assert len(ids) == len(set(ids))
    assert {rule["resource_type"] for rule in load_yaml_rules()} <= known


@pytest.mark.parametrize("path", ["README.md", "CLAUDE.md", "docs/架构文档.md"])
def test_docs_state_the_actual_builtin_rule_count(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert f"{len(load_yaml_rules())} 条内置规则" in text, f"{path} 的内置规则数量与 default_rules.yaml 不一致"
