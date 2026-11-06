"""没有数据来源的内置规则（R-DCS-007 / R-GS-007 / R-GS-008）停用；GaussDB 变更状态超时规则（R-GS-011）。

迁移 0020 只停用未经人工修改的系统规则，可还原；新装环境的内置规则一开始就是停用的。
"""
import re
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AlarmRule, SysSetting
from app.rules.expr import evaluate
from app.rules.loader import load_yaml_rules

from .test_retire_migration_0018 import config

DEAD = ("R-DCS-007", "R-GS-007", "R-GS-008")
MARKER = "system:no-data-source-v1"


def rule(code, **extra):
    return AlarmRule(id=code, name=code, resource_type="dcs_instance", check_item="connections", kind="threshold",
                     expr={"value": "x", "levels": []}, recover={}, updated_by=extra.pop("updated_by", "system"), **extra)


def test_default_rules_mark_the_data_less_rules_disabled_and_keep_the_live_cpu_rule_enabled():
    rules = {r["id"]: r for r in load_yaml_rules()}
    assert all(rules[code].get("enabled") is False for code in DEAD)
    assert rules["R-DCS-006"].get("enabled", True) is True                       # DCS CPU 由累计秒折算后恢复可用
    assert len(rules) == len({r["id"] for r in load_yaml_rules()}) == 40      # 含 DCS 节点、GaussDB 组件 3 条


def test_gaussdb_transitional_state_rule_fires_only_after_an_hour_in_a_transitional_state():
    raw = next(r for r in load_yaml_rules() if r["id"] == "R-GS-011")
    assert raw["resource_type"] == "gaussdb_instance" and raw["check_item"] == "instance_status" and raw["kind"] == "state"
    when = raw["expr"]["when"]
    cases = [("UPGRADE TO BE OBSERVED", 90, True), ("MODIFYING DATABASE PORT", 61, True), ("MODIFYING", 60, False),
             ("BUILD", 120, True), ("REPAIRING", 61, True), ("ACTIVE", 600, False), ("BACKING UP", 600, False),
             ("FAILED", 600, False)]                                              # 备份是例行操作；故障由 R-GS-001 负责
    for status, minutes, expected in cases:
        assert bool(evaluate(when, {"status": status, "status_duration_min": minutes})) is expected, (status, minutes)
    assert raw["recover"] == {"when": "status == 'ACTIVE'"}


def test_every_gaussdb_status_in_the_pdf_is_known_to_the_collector_and_covered_by_some_rule():
    from app.collectors.gaussdb import NATIVE_STATES

    text = " ".join(str(r["expr"].get("when", "")) for r in load_yaml_rules() if r["resource_type"] == "gaussdb_instance")
    quoted = set(re.findall(r"'([A-Z][A-Z _]+)'", text))
    benign = {"ACTIVE", "BACKING UP"}                                             # 正常 / 例行备份：不需要告警
    assert NATIVE_STATES - quoted - benign == set()


def test_migration_disables_only_unmodified_enabled_system_rules_and_downgrade_restores_them(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0019")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            db.add_all([rule("R-DCS-007"), rule("R-GS-007", updated_by="zhangsan"),      # 人工改过的不动
                        rule("R-GS-008", enabled=False), rule("R-DCS-006", enabled=True)])
            db.commit()

        command.upgrade(cfg, "0020")
        with Session(engine) as db:
            state = {r.id: (r.enabled, r.version, r.updated_by) for r in db.scalars(select(AlarmRule))}
            assert state["R-DCS-007"] == (False, 2, MARKER)
            assert state["R-GS-007"] == (True, 1, "zhangsan")
            assert state["R-GS-008"] == (False, 1, "system")
            assert state["R-DCS-006"] == (True, 1, "system")
            keys = {s.key for s in db.scalars(select(SysSetting).where(SysSetting.key.like("rule-nosource-0020:%")))}
            assert keys == {"rule-nosource-0020:R-DCS-007"}

        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0019")
        with Session(engine) as db:
            row = db.get(AlarmRule, "R-DCS-007")
            assert (row.enabled, row.version, row.updated_by) == (True, 1, "system")
            assert db.scalars(select(SysSetting).where(SysSetting.key.like("rule-nosource-0020:%"))).all() == []
    finally:
        engine.dispose()


def test_downgrade_keeps_a_rule_that_was_edited_after_the_migration(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0019")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            db.add(rule("R-GS-007"))
            db.commit()
        command.upgrade(cfg, "0020")
        with Session(engine) as db:
            row = db.get(AlarmRule, "R-GS-007")
            row.name, row.version, row.updated_by = "自定义名称", row.version + 1, "lisi"
            db.commit()
        command.downgrade(cfg, "0019")
        with Session(engine) as db:
            row = db.get(AlarmRule, "R-GS-007")
            assert (row.enabled, row.name, row.updated_by) == (False, "自定义名称", "lisi")
    finally:
        engine.dispose()


def test_migration_file_is_the_single_head_after_0019():
    text = (Path(__file__).resolve().parents[1] / "migrations/versions/20261107_0020_disable_rules_without_data_source.py"
            ).read_text(encoding="utf-8")
    assert 'revision = "0020"' in text and 'down_revision = "0019"' in text
