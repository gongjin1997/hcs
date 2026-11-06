"""恢复必须有正面证据；0014 和运行时补丁均保护自定义并可逆。"""
from copy import deepcopy
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AlarmRule, SysSetting
from app.rules.evaluator import evaluate_rule
from app.rules.loader import load_yaml_rules, sync_default_rules
from app.rules.model import Rule
from app.rules.recovery_evidence import MARKER, PATCHES, PREFIX


def legacy(code):
    patch = PATCHES[code]
    return AlarmRule(id=code, **deepcopy(patch["match"]), recover=deepcopy(patch["before"]), version=1, updated_by="system")


def config(url):
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


def test_startup_repairs_only_original_system_defaults_once(mem_db):
    mem_db.add_all(legacy(code) for code in PATCHES)
    mem_db.commit()
    sync_default_rules(mem_db)
    for code, patch in PATCHES.items():
        row = mem_db.get(AlarmRule, code)
        assert row.recover == patch["after"] and row.version == 2 and row.updated_by == MARKER
        assert mem_db.get(SysSetting, PREFIX + code).value["before"]["recover"] == patch["before"]
    sync_default_rules(mem_db)
    assert all(mem_db.get(AlarmRule, code).version == 2 for code in PATCHES)
    assert mem_db.get(AlarmRule, "R-GS-009") is not None and mem_db.get(AlarmRule, "R-GS-010") is not None


@pytest.mark.parametrize("change", [{"updated_by": "operator"}, {"version": 2}, {"enabled": False},
                                    {"scope": {"critical": True}}, {"name": "custom-name"},
                                    {"expr": {"when": "status == 'EXCEPTION'", "severity": "P1"}},
                                    {"recover": {"when": "status == 'NORMAL'", "for_count": 3}}])
def test_runtime_does_not_rewrite_customized_rule(mem_db, change):
    row = legacy("R-DGC-RT-003")
    for key, value in change.items():
        setattr(row, key, value)
    mem_db.add(row)
    mem_db.commit()
    before = deepcopy(row.recover), deepcopy(row.expr), row.version, row.updated_by
    sync_default_rules(mem_db)
    assert (row.recover, row.expr, row.version, row.updated_by) == before
    assert mem_db.get(SysSetting, PREFIX + row.id) is None


@pytest.mark.parametrize("code", list(PATCHES))
def test_unknown_state_or_missing_result_never_recovers_builtin_alarm(code):
    raw = next(row for row in load_yaml_rules() if row["id"] == code)
    rule = Rule(**raw)
    facts = {"status": "FUTURE_STATE", "last_status": "FUTURE_STATE", "job_state": "FUTURE_STATE",
             "job_result": "SUCCEEDED", "status_duration_min": 20}
    assert evaluate_rule(rule, facts, {}, "P3").action != "RESOLVE"


def test_migration_restores_owned_changes_and_preserves_custom_rule(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0013")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            db.add_all(legacy(code) for code in PATCHES)
            baseline = db.get(AlarmRule, "R-DGC-BAT-003")
            baseline.runbook = PATCHES[baseline.id]["runbook_before"]
            customized = db.get(AlarmRule, "R-DGC-RT-004")
            customized.updated_by, customized.version = "operator", 2
            db.commit()
        command.upgrade(cfg, "0014")
        with Session(engine) as db:
            assert db.get(AlarmRule, "R-DGC-RT-004").recover == PATCHES["R-DGC-RT-004"]["before"]
            for code, patch in PATCHES.items():
                if code != "R-DGC-RT-004":
                    assert db.get(AlarmRule, code).recover == patch["after"]
            assert db.get(AlarmRule, "R-DGC-BAT-003").runbook == PATCHES["R-DGC-BAT-003"]["runbook_after"]
            changed_after = db.get(AlarmRule, "R-DGC-BAT-002")
            changed_after.name, changed_after.version, changed_after.updated_by = "edited after migration", 3, "operator"
            db.commit()
        command.downgrade(cfg, "0013")
        with Session(engine) as db:
            for code, patch in PATCHES.items():
                row = db.get(AlarmRule, code)
                if code == "R-DGC-BAT-002":
                    assert row.name == "edited after migration" and row.recover == patch["after"] and row.version == 3
                else:
                    assert row.recover == patch["before"]
            assert db.get(AlarmRule, "R-DGC-BAT-003").runbook == PATCHES["R-DGC-BAT-003"]["runbook_before"]
            assert db.scalars(select(SysSetting).where(SysSetting.key.startswith(PREFIX))).all() == []
        command.upgrade(cfg, "0014")
    finally:
        engine.dispose()


def test_downgrade_does_not_touch_rules_created_after_migration(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0014")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            row = legacy("R-DGC-BAT-002")
            row.recover = PATCHES[row.id]["after"]
            db.add(row)
            db.commit()
        command.downgrade(cfg, "0013")
        with Session(engine) as db:
            assert db.get(AlarmRule, "R-DGC-BAT-002").recover == PATCHES["R-DGC-BAT-002"]["after"]
    finally:
        engine.dispose()


def test_migration_recognizes_startup_repair_backup(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0013")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            db.add(legacy("R-DGC-BAT-002"))
            db.commit()
            original_updated_at = db.get(AlarmRule, "R-DGC-BAT-002").updated_at
            sync_default_rules(db)
        command.upgrade(cfg, "0014")
        command.downgrade(cfg, "0013")
        with Session(engine) as db:
            row = db.get(AlarmRule, "R-DGC-BAT-002")
            assert row.recover == PATCHES[row.id]["before"] and row.version == 1 and row.updated_by == "system"
            assert row.updated_at == original_updated_at
    finally:
        engine.dispose()


def test_baseline_default_runbook_is_upgraded_but_user_runbook_is_preserved(mem_db):
    patch = PATCHES["R-DGC-BAT-003"]
    row = legacy("R-DGC-BAT-003")
    row.runbook = patch.get("runbook_before", "1. 对比该作业近 7 天运行时长（P90）")
    mem_db.add(row)
    mem_db.commit()
    sync_default_rules(mem_db)
    assert "24" in row.runbook and "7 天" not in row.runbook
    assert "p90_min_samples" in row.runbook


def test_custom_baseline_runbook_is_not_overwritten(mem_db):
    row = legacy("R-DGC-BAT-003")
    row.runbook = "operator-specific-runbook"
    mem_db.add(row)
    mem_db.commit()
    sync_default_rules(mem_db)
    assert row.runbook == "operator-specific-runbook"
