"""共享任务设置权限与平台状态口径，使用独立内存库。"""

from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest

from app.models import InspectionRun, InspectionTask
from app.models.base import utcnow

from . import test_user_tenant_review

management_api = test_user_tenant_review.management_api


def test_metrics_export_uses_current_inspection_evidence_without_clearing_alarm(mem_db, monkeypatch):
    from app.api import metrics
    from app.models import Resource

    now = utcnow()
    for index, (health, age, missing) in enumerate((("OK", 60, 0), ("OK", 901, 0),
                                                  ("OK", 60, 1), ("MAJOR", 901, 0)), 1):
        mem_db.add(Resource(resource_id=str(index), name=str(index), service="ecs", resource_type="ecs_server",
                            health=health, monitored=True, attrs={"inspection_monitoring": {
                                "evaluated_at": (now - timedelta(seconds=age)).isoformat(),
                                "checked_rules": 1, "unavailable_rules": missing, "stale_after_sec": 900}}))
    mem_db.commit()
    monkeypatch.setattr(metrics, "utcnow", lambda: now)
    monkeypatch.setattr(metrics, "SessionLocal", lambda: nullcontext(mem_db))
    rows = [line for line in metrics.collect().splitlines() if line.startswith("hcs_inspector_resources{")]
    assert len(rows) == 3
    assert any('health="OK"' in line and line.endswith(" 1") for line in rows)
    assert any('health="UNKNOWN"' in line and line.endswith(" 2") for line in rows)
    assert any('health="MAJOR"' in line and line.endswith(" 1") for line in rows)


def test_batch_p90_minimum_is_configurable_without_overwriting_other_parameters(mem_db, management_api):
    client, user, _, _ = management_api
    user.permissions, user.tenant_ids = ["task:edit"], None
    task = InspectionTask(code="dgc_batch_instance", name="批作业", service="dataarts", interval_sec=300,
                          params={"keep": "synthetic"}, enabled=True)
    mem_db.add(task)
    mem_db.commit()
    response = client.put("/api/v1/system/tasks/dgc_batch_instance", json={"p90_min_samples": 8})
    assert response.status_code == 200, response.text
    assert task.params == {"keep": "synthetic", "p90_min_samples": 8}
    assert response.json()["data"]["p90_min_samples"] == 8
    response = client.put("/api/v1/system/tasks/dgc_batch_instance", json={"p90_min_samples": None})
    assert response.status_code == 200 and response.json()["data"]["p90_min_samples"] == 5
    assert task.params == {"keep": "synthetic"}


@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5, 10001])
def test_batch_p90_rejects_invalid_minimum(mem_db, management_api, value):
    client, user, _, _ = management_api
    user.permissions, user.tenant_ids = ["task:edit"], None
    mem_db.add(InspectionTask(code="dgc_batch_instance", name="批作业", service="dataarts", interval_sec=300))
    mem_db.commit()
    assert client.put("/api/v1/system/tasks/dgc_batch_instance", json={"p90_min_samples": value}).status_code == 400


def test_p90_setting_cannot_apply_to_other_task_or_scoped_editor(mem_db, management_api):
    client, user, _, _ = management_api
    user.permissions, user.tenant_ids = ["task:edit"], None
    for code in ("dgc_batch_instance", "ecs_status"):
        mem_db.add(InspectionTask(code=code, name=code, service="test", interval_sec=300))
    mem_db.commit()
    assert client.put("/api/v1/system/tasks/ecs_status", json={"p90_min_samples": 8}).status_code == 400
    user.tenant_ids = [1]
    assert client.put("/api/v1/system/tasks/dgc_batch_instance", json={"p90_min_samples": 8}).status_code == 403


def test_scoped_task_editor_cannot_disable_global_schedule(mem_db, management_api):
    client, cu, _, _ = management_api
    task = InspectionTask(code="dcs_status", name="缓存巡检", service="dcs", interval_sec=120, enabled=True)
    mem_db.add(task)
    mem_db.commit()
    cu.permissions, cu.tenant_ids = ["task:edit"], [1]
    response = client.put("/api/v1/system/tasks/dcs_status", json={"enabled": False})
    assert response.status_code == 403
    assert task.enabled
    cu.tenant_ids = None
    allowed = client.put("/api/v1/system/tasks/dcs_status", json={"enabled": False}, headers={"X-Tenant-Id": "1"})
    assert allowed.status_code == 200 and not task.enabled


def test_partial_run_is_not_success_and_platform_health_is_warning(mem_db, management_api):
    client, cu, _, _ = management_api
    cu.permissions, cu.tenant_ids = ["task:view"], [1]
    now = utcnow()
    mem_db.add(InspectionTask(code="dcs_status", name="缓存巡检", service="dcs", interval_sec=120, enabled=True))
    successful = now - timedelta(hours=2)
    for at, status in ((successful, "SUCCESS"), (now - timedelta(hours=1), "PARTIAL")):
        mem_db.add(
            InspectionRun(
                task_code="dcs_status",
                tenant_id=1,
                trigger_type="MANUAL",
                status=status,
                started_at=at,
                finished_at=at + timedelta(seconds=2),
            )
        )
    mem_db.commit()
    response = client.get("/api/v1/system/platform")
    assert response.status_code == 200, response.text
    task = response.json()["data"]["tasks"][0]
    assert task["status"] == "WARN"
    assert task["tenants"][0]["last_status"] == "PARTIAL"
    assert datetime.fromisoformat(task["tenants"][0]["last_success_at"]) == successful + timedelta(seconds=2)
