"""正式环境首次安装默认关闭采集和巡检，更新及重复初始化保留已有选择。"""
from types import SimpleNamespace

from sqlalchemy import select

from app.models import CollectSwitch, InspectionTask
from app.services import bootstrap as service
from app.services import collect_switch


def production(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(env="prod"))


def test_fresh_production_defaults_close_all_collection_and_tasks(mem_db, monkeypatch):
    production(monkeypatch)
    result = service.bootstrap(mem_db, "isolated-initial-password")
    assert result["admin_created"]
    rows = list(mem_db.scalars(select(CollectSwitch)))
    assert {row.resource_type for row in rows} == set(collect_switch.SWITCH_TYPES)
    assert all(row.tenant_id == 0 and not row.enabled for row in rows)
    tasks = list(mem_db.scalars(select(InspectionTask)))
    assert len(tasks) == len(service.BUILTIN_TASKS)
    assert all(not task.enabled for task in tasks)


def test_repeated_bootstrap_preserves_enabled_task_and_collection(mem_db, monkeypatch):
    production(monkeypatch)
    service.bootstrap(mem_db, "isolated-initial-password")
    task = mem_db.get(InspectionTask, "dcs_status")
    task.enabled, task.interval_sec, task.params = True, 137, {"preserve": True}
    collect_switch.update(mem_db, [{"resource_type": "dcs_instance", "tenant_id": 0, "enabled": True}], "operator")
    mem_db.commit()
    result = service.bootstrap(mem_db, "different-password-must-not-replace-admin")
    assert not result["admin_created"]
    assert collect_switch.load(mem_db).enabled(None, "dcs_instance")
    assert mem_db.get(InspectionTask, "dcs_status").enabled
    assert mem_db.get(InspectionTask, "dcs_status").interval_sec == 137
    assert mem_db.get(InspectionTask, "dcs_status").params == {"preserve": True}


def test_legacy_environment_without_switch_rows_is_not_globally_disabled(mem_db, monkeypatch):
    service.bootstrap(mem_db, "isolated-existing-admin")
    mem_db.get(InspectionTask, "dcs_status").enabled = True
    mem_db.commit()
    production(monkeypatch)
    service.bootstrap(mem_db)
    assert list(mem_db.scalars(select(CollectSwitch))) == []
    assert collect_switch.load(mem_db).enabled(None, "dcs_instance")
    assert mem_db.get(InspectionTask, "dcs_status").enabled


def test_new_task_is_disabled_without_resetting_existing_environment(mem_db, monkeypatch):
    service.bootstrap(mem_db, "isolated-existing-admin")
    mem_db.get(InspectionTask, "dcs_status").enabled = True
    mem_db.delete(mem_db.get(InspectionTask, "ecs_status"))
    mem_db.commit()
    production(monkeypatch)
    service.bootstrap(mem_db)
    assert not mem_db.get(InspectionTask, "ecs_status").enabled
    assert mem_db.get(InspectionTask, "dcs_status").enabled


def test_disabled_initialization_does_not_overwrite_explicit_choices(mem_db):
    mem_db.add(CollectSwitch(tenant_id=0, resource_type="ecs_server", enabled=True))
    mem_db.commit()
    collect_switch.initialize_disabled(mem_db, 0, "bootstrap")
    collect_switch.initialize_disabled(mem_db, 0, "bootstrap")
    mem_db.commit()
    assert collect_switch.load(mem_db).enabled(None, "ecs_server")
    assert len(list(mem_db.scalars(select(CollectSwitch)))) == len(collect_switch.SWITCH_TYPES)
