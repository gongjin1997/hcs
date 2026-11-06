"""GaussDB 实例列表与概况：只读本地台账与最近一次巡检（磁盘、组件、告警），异常在前；不请求云。"""
from datetime import timedelta

from app.models import AlarmEvent, Resource
from app.models.base import utcnow
from tests.test_gaussdb_monitoring import BASE, path
from tests.test_gaussdb_monitoring import gauss_api as gauss_api


def collected(db, resource_ref, *, usage=None, status="AVAILABLE", ago=60, components=None, **changes):
    row = db.get(Resource, resource_ref)
    at = (utcnow() - timedelta(seconds=ago)).isoformat()
    row.attrs = {**row.attrs, "type": "主备", "version": "GaussDB 3.208",
                 "nodes": [{"name": "dn-1", "role": "master", "status": "ACTIVE"}],
                 "gaussdb_monitoring": {"source": "GAUSSDB", "status": status, "observed_at": at, "stale_after_sec": 900},
                 **({"gaussdb_components": components} if components is not None else {})}
    row.metrics = {**row.metrics, "disk_usage": usage, "disk_used_gb": 85.0 if usage else None, "disk_total_gb": 100.0}
    for key, value in changes.items():
        setattr(row, key, value)
    db.commit()
    return at


def components(down=0, ago=60, unknown=0):
    items = [{"node_id": "node-1", "node_name": "dn-1", "component_id": "dn_6001", "component_type": "DN",
              "role": "master", "status": "Down" if down else "Primary"}]
    return {"state": "AVAILABLE", "observed_at": (utcnow() - timedelta(seconds=ago)).isoformat(), "total": 1,
            "down": down, "unknown": unknown, "by_type": {"DN": 1}, "warning": None, "items": items}


def alarm(db, resource_ref, status="FIRING"):
    db.add(AlarmEvent(tenant_id=1, fingerprint=f"fp-{resource_ref}-{status}", rule_id="R-GS-006", resource_ref=resource_ref,
                      service="gaussdb", resource_type="gaussdb_instance", resource_name="x", severity="P2",
                      status=status, title="磁盘", check_item="disk"))
    db.commit()


def test_list_reports_collected_disk_components_and_alarms_with_provenance(gauss_api):
    g = gauss_api
    at = collected(g.db, 111, usage=85.0, components=components(down=1))
    alarm(g.db, 111)
    alarm(g.db, 111, status="RESOLVED")                                  # 已恢复的不计入
    items = {row["id"]: row for row in g.client.get(path("")).json()["data"]["items"]}
    row = items[111]
    assert (row["disk_usage"], row["disk_used_gb"], row["disk_total_gb"]) == (85.0, 85.0, 100.0)
    assert row["disk_state"] == "AVAILABLE" and row["disk_freshness"] == "FRESH"
    assert row["disk_observed_at"].startswith(at[:19])
    assert row["components"]["down"] == 1 and row["components"]["freshness"] == "FRESH"
    assert (row["deploy_type"], row["version"], row["node_count"], row["active_alarms"]) == ("主备", "GaussDB 3.208", 1, 1)
    assert row["nodes"] is None and row["component_items"] is None          # 清单只在详情返回
    # 没有采集来源的旧快照（fixture 里的 disk_usage=97）不能冒充当前用量
    assert items[112]["disk_usage"] is None and items[112]["disk_freshness"] == "NO_DATA"
    assert items[112]["components"] is None and items[112]["active_alarms"] == 0
    assert g.cloud.requests == [] and g.cloud.contexts == []


def test_failed_or_old_collections_are_marked_and_not_counted_as_current(gauss_api):
    g = gauss_api
    collected(g.db, 111, usage=None, status="ERROR")
    collected(g.db, 112, usage=95.0, ago=7200, components=components(down=1, ago=7200))
    items = {row["id"]: row for row in g.client.get(path("")).json()["data"]["items"]}
    assert items[111]["disk_state"] == "ERROR" and items[111]["disk_usage"] is None
    assert items[111]["disk_freshness"] == "NO_DATA"
    assert items[112]["disk_freshness"] == "STALE" and items[112]["components"]["freshness"] == "STALE"
    summary = g.client.get(f"{BASE}/summary").json()["data"]
    assert summary["disk_high"] == 0 and summary["components_down"] == 0       # 过期的采集不计入高水位


def test_abnormal_instances_come_first_and_the_status_group_filter_matches_the_cards(gauss_api):
    g = gauss_api
    row = g.db.get(Resource, 112)
    row.status_raw = "STORAGE FULL"
    g.db.commit()
    data = g.client.get(path("")).json()["data"]
    assert [item["id"] for item in data["items"]] == [112, 111]
    assert [item["status_group"] for item in data["items"]] == ["abnormal", "normal"]
    abnormal = g.client.get(path(""), params={"status_group": "abnormal"}).json()["data"]
    assert abnormal["total"] == 1 and abnormal["items"][0]["id"] == 112
    normal = g.client.get(path(""), params={"status_group": "normal"}).json()["data"]
    assert [item["id"] for item in normal["items"]] == [111]
    # 有活动告警的正常实例也算异常
    g.db.get(Resource, 111).health = "MAJOR"
    g.db.commit()
    assert g.client.get(path(""), params={"status_group": "normal"}).json()["data"]["total"] == 0
    assert g.client.get(path(""), params={"status_group": "bogus"}).status_code == 400


def test_transitional_stopped_and_unknown_states_are_grouped():
    from app.services.gaussdb_monitoring import status_group

    assert status_group("OK", "ACTIVE") == "normal"
    assert status_group("UNKNOWN", "REBOOTING") == "transitional"
    assert status_group("UNKNOWN", "BACKING UP") == "transitional"
    assert status_group("OK", "SHUTDOWN") == "stopped"
    assert status_group("MAJOR", "SHUTDOWN") == "abnormal"                 # 非预期停止已告警
    assert status_group("OK", "FAILED") == "abnormal"
    assert status_group("OK", "SOMETHING NEW") == "abnormal"                # 未识别不当作正常
    assert status_group("OK", None) == "abnormal"


def test_detail_returns_the_last_collected_components_and_nodes(gauss_api):
    g = gauss_api
    collected(g.db, 111, usage=10.0, components=components(down=1))
    data = g.client.get(path("") + "/111").json()["data"]
    assert data["component_items"] == [{"node_id": "node-1", "node_name": "dn-1", "availability_zone_id": None,
                                        "component_id": "dn_6001", "component_type": "DN", "role": "master",
                                        "status": "Down", "distributed_id": None}]
    assert data["nodes"] == [{"name": "dn-1", "role": "master", "status": "ACTIVE"}]
    assert g.cloud.requests == []


def test_summary_counts_the_visible_scope_only(gauss_api):
    g = gauss_api
    collected(g.db, 111, usage=90.0, components=components(down=1))
    g.db.get(Resource, 112).status_raw = "REBOOTING"
    g.db.get(Resource, 211).status_raw = "FAILED"                          # 其他租户，不可见
    g.db.commit()
    alarm(g.db, 111)
    data = g.client.get(f"{BASE}/summary").json()["data"]
    assert (data["total"], data["normal"], data["transitional"], data["abnormal"]) == (2, 1, 1, 0)
    assert (data["disk_high"], data["components_down"], data["active_alarms"]) == (1, 1, 1)
    assert data["last_collected_at"] is not None
    assert g.client.get(f"{BASE}/summary", params={"project_ref": 12}).json()["data"]["total"] == 1
    assert g.client.get(f"{BASE}/summary", params={"tenant_id": 2}).status_code == 404
    assert g.cloud.requests == []
