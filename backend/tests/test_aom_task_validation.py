"""AOM 全局停用：所有任务拒绝新映射，普通编辑保留旧值，null 仅清除旧映射。"""
import pytest
from sqlalchemy import select

from app.models import AuditLog, InspectionTask

from .test_user_tenant_review import management_api as management_api


@pytest.mark.parametrize("code,metrics,units", [
    ("ecs_status", {"cpu_usage": "remote_cpu"}, {}),
    ("dcs_status", {"password": "forbidden-value"}, {}),
    ("ecs_status", {"cpu_util": "remote_cpu"}, {"other": "Percent"}),
    ("mrs_job_status", {"run_minutes": "remote_elapsed"}, {}),
    ("data_cleanup", {"cpu_util": "remote_cpu"}, {}),
])
def test_unknown_task_aliases_are_rejected_before_any_edit_or_audit(mem_db, management_api, code, metrics, units):
    client = management_api[0]
    task = InspectionTask(code=code, name="test", service="test", interval_sec=120, enabled=True, params={"keep": 1})
    mem_db.add(task)
    mem_db.commit()
    response = client.put(f"/api/v1/system/tasks/{code}", json={"enabled": False, "aom": {
        "namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": metrics, "units": units}})
    assert response.status_code == 400
    assert task.params == {"keep": 1} and task.enabled is True
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None


@pytest.mark.parametrize("metric", ["memory_usage", "cpu_usage", "connected_clients"])
def test_dcs_rejects_new_aom_mapping_without_partial_task_edit(mem_db, management_api, metric):
    client = management_api[0]
    task = InspectionTask(code="dcs_status", name="DCS", service="dcs", interval_sec=300,
                          enabled=True, params={"keep": 1})
    mem_db.add(task)
    mem_db.commit()
    response = client.put("/api/v1/system/tasks/dcs_status", json={"enabled": False, "aom": {
        "namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": {metric: "synthetic_metric"}}})
    assert response.status_code == 400 and "AOM 已全局停用" in response.text
    assert task.enabled and task.params == {"keep": 1}
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None


def test_dcs_schedule_edit_preserves_legacy_mapping_and_explicit_clear_is_allowed(mem_db, management_api):
    client = management_api[0]
    mapping = {"namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": {"memory_usage": "memory_percent"}}
    task = InspectionTask(code="dcs_status", name="DCS", service="dcs", interval_sec=300,
                          enabled=True, params={"keep": 1, "aom": mapping})
    mem_db.add(task)
    mem_db.commit()
    assert client.put("/api/v1/system/tasks/dcs_status", json={"interval_sec": 600}).status_code == 200
    assert task.interval_sec == 600 and task.params == {"keep": 1, "aom": mapping}
    assert client.put("/api/v1/system/tasks/dcs_status", json={"aom": None}).status_code == 200
    assert task.params == {"keep": 1}


@pytest.mark.parametrize("metric", ["cpu_usage", "mem_usage", "disk_usage"])
def test_gaussdb_rejects_mapping_before_schedule_edit_and_audit(mem_db, management_api, metric):
    client = management_api[0]
    task = InspectionTask(code="gaussdb_status", name="GaussDB", service="gaussdb", interval_sec=300,
                          enabled=True, params={"keep": {"window_hours": 6}})
    mem_db.add(task)
    mem_db.commit()
    response = client.put("/api/v1/system/tasks/gaussdb_status", json={"enabled": False, "interval_sec": 600, "aom": {
        "namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": {metric: "synthetic_metric"}}})
    assert response.status_code == 400 and "AOM 已全局停用" in response.text
    mem_db.refresh(task)
    assert task.enabled and task.interval_sec == 300 and task.params == {"keep": {"window_hours": 6}}
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None


@pytest.mark.parametrize("legacy", [
    {"namespace": "CUSTOM.Test", "metrics": {"cpu_usage": "old_cpu"}}, "invalid-old-mapping", {},
])
def test_gaussdb_schedule_and_clear_preserve_other_params_and_are_idempotent(mem_db, management_api, legacy):
    client = management_api[0]
    params = {"aom": legacy, "window_hours": 6, "native": {"slow_sql": True}}
    task = InspectionTask(code="gaussdb_status", name="GaussDB", service="gaussdb", interval_sec=300,
                          enabled=True, params=params)
    mem_db.add(task)
    mem_db.commit()
    response = client.put("/api/v1/system/tasks/gaussdb_status", json={"interval_sec": 600, "enabled": False})
    assert response.status_code == 200 and response.json()["data"]["params"] == params
    assert task.params == params and task.interval_sec == 600 and not task.enabled
    cleared = client.put("/api/v1/system/tasks/gaussdb_status", json={"aom": None})
    assert cleared.status_code == 200 and task.params == {"window_hours": 6, "native": {"slow_sql": True}}
    assert cleared.json()["data"]["changed"]
    again = client.put("/api/v1/system/tasks/gaussdb_status", json={"aom": None})
    assert again.status_code == 200 and not again.json()["data"]["changed"]


@pytest.mark.parametrize("code", ["ecs_status", "ecs_metrics", "dcs_status", "gaussdb_status", "custom_task"])
@pytest.mark.parametrize("value", [{}, {"namespace": "CUSTOM.Test", "metrics": {"cpu_util": "cpu"}},
                                   "old-mapping", [], False, 0])
def test_every_task_rejects_any_non_null_aom_before_edit_or_audit(mem_db, management_api, code, value):
    client = management_api[0]
    task = InspectionTask(code=code, name="任务", service="test", interval_sec=300, enabled=True,
                          params={"keep": {"window_hours": 6}})
    mem_db.add(task)
    mem_db.commit()
    response = client.put(f"/api/v1/system/tasks/{code}", json={"enabled": False, "interval_sec": 600, "aom": value})
    assert response.status_code == 400 and "AOM 已全局停用" in response.text
    mem_db.refresh(task)
    assert task.interval_sec == 300 and task.enabled and task.params == {"keep": {"window_hours": 6}}
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None


@pytest.mark.parametrize("code", ["ecs_status", "ecs_metrics", "custom_task"])
@pytest.mark.parametrize("legacy", [{"metrics": {"cpu_util": "old"}}, "old-mapping", {}, None])
def test_any_task_can_clear_legacy_aom_without_losing_other_params(mem_db, management_api, code, legacy):
    client = management_api[0]
    params = {"aom": legacy, "native": {"keep": True}, "window_hours": 6}
    task = InspectionTask(code=code, name="任务", service="test", interval_sec=300, enabled=True, params=params)
    mem_db.add(task)
    mem_db.commit()
    response = client.put(f"/api/v1/system/tasks/{code}", json={"interval_sec": 600})
    assert response.status_code == 200 and response.json()["data"]["params"] == params
    cleared = client.put(f"/api/v1/system/tasks/{code}", json={"aom": None})
    assert cleared.status_code == 200 and task.params == {"native": {"keep": True}, "window_hours": 6}
    assert client.put(f"/api/v1/system/tasks/{code}", json={"aom": None}).status_code == 200
