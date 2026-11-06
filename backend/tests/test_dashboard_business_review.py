"""总览巡检对象、异常分子及文档定义的样本口径。"""
from datetime import timedelta

import pytest

from app.models import AlarmEvent, Resource
from app.models.base import utcnow
from app.services import dashboard_service


def test_abnormal_resource_count_uses_same_population_as_inspected_objects(mem_db):
    rows = [Resource(id=i, tenant_id=1, service="ecs", resource_type="ecs_server", resource_id=f"r-{i}",
                     name=f"r-{i}", health="MAJOR", monitored=i != 2,
                     lifecycle="DELETED" if i == 3 else "ACTIVE") for i in (1, 2, 3)]
    mem_db.add_all(rows)
    mem_db.add_all([AlarmEvent(tenant_id=1, resource_ref=i, fingerprint=f"fp-{i}", rule_id="rule", service="ecs",
                              resource_type="ecs_server", resource_name=f"r-{i}", severity="P2", status="FIRING",
                              title="CPU", check_item="cpu") for i in (1, 2, 3, 999)])
    mem_db.commit()
    data = dashboard_service.summary(mem_db, tenant_ids=[1])
    assert data["kpis"]["resources"] == 1 and data["kpis"]["abnormal_resources"] == 1
    assert sum(group["abnormal"] for group in data["groups"]) == 1


def test_documented_resolved_sample_is_new_alarms_with_recovery(mem_db):
    now = utcnow()
    for index, first_seen in enumerate([now - timedelta(days=2), now - timedelta(hours=2)]):
        mem_db.add(AlarmEvent(tenant_id=1, fingerprint=f"resolved-{index}", rule_id="rule", service="ecs",
                              resource_type="ecs_server", resource_name="server", severity="P2", status="RESOLVED",
                              title="CPU", check_item="cpu", first_seen=first_seen, last_seen=now,
                              resolved_at=now - timedelta(hours=1)))
    mem_db.commit()
    assert dashboard_service.summary(mem_db, now, tenant_ids=[1])["kpis"]["resolved_24h"] == 1


@pytest.mark.parametrize("tenant_ids", [[], [2]])
def test_empty_or_other_tenant_scope_cannot_see_alarm_totals(mem_db, tenant_ids):
    mem_db.add(AlarmEvent(tenant_id=1, fingerprint="foreign", rule_id="rule", service="ecs",
                          resource_type="ecs_server", resource_name="server", severity="P2", status="FIRING",
                          title="CPU", check_item="cpu"))
    mem_db.commit()
    data = dashboard_service.summary(mem_db, tenant_ids=tenant_ids)
    assert data["kpis"]["active_alarms"] == 0 and data["top_alarms"] == []


def test_documented_platform_score_keeps_group_population_weighting(mem_db):
    now = utcnow()
    resources = [Resource(id=i, tenant_id=1, service="ecs" if i < 3 else "dcs",
                          resource_type="ecs_server" if i < 3 else "dcs_instance", resource_id=f"r-{i}",
                          name=f"r-{i}", health="CRITICAL" if i == 1 else "OK", critical=i == 1,
                          attrs={"inspection_monitoring": {"evaluated_at": now.isoformat(),
                              "stale_after_sec": 900, "checked_rules": 1, "unavailable_rules": 0}})
                 for i in (1, 2, 3)]
    mem_db.add_all(resources)
    mem_db.add(AlarmEvent(tenant_id=1, resource_ref=1, fingerprint="critical", rule_id="rule", service="ecs",
                          resource_type="ecs_server", resource_name="server", severity="P1", status="FIRING",
                          title="CPU", check_item="cpu"))
    mem_db.commit()
    assert dashboard_service.summary(mem_db, tenant_ids=[1])["health_score"] == 50.0
    assert dashboard_service._score(resources, {1: "P1"}) == 40.0
