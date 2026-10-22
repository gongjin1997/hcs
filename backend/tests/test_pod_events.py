"""容器事件：CCE Pod 重启 / OOM / CrashLoop / 驱逐 / 健康检查失败 / 背压，按租户采集；列表、统计、Pod 时间线、背压探针配置。"""
from collections import Counter

from sqlalchemy import func, select

from app.models import PodEvent, PodState, Resource
from app.services.pipeline import run_task
from app.services.tenancy import resolve_targets

from .conftest import MOCK_URL, demo_login


def _collect(db, code, http):
    tg = next(t for t in resolve_targets(db) if t.code == code)
    return run_task(db, "cce_pod_events", client=tg.client(http=http, mock_url=MOCK_URL), target=tg)


def test_collect_pod_events_per_tenant(world, mem_db, mock_http):
    runs = {code: _collect(mem_db, code, mock_http) for code in world}
    assert {r.status for r in runs.values()} == {"SUCCESS"}
    types = {code: Counter(e.event_type for e in mem_db.scalars(select(PodEvent).where(PodEvent.tenant_id == t.id)))
             for code, t in world.items()}
    assert {"RESTART", "OOM_KILLED", "CRASH_LOOP", "EVICTED", "PROBE_FAILED", "IMAGE_PULL_FAILED", "SCHEDULE_FAILED",
            "NODE_PRESSURE"} <= set(types["tenant-a"])
    assert {"RESTART", "BACKPRESSURE", "PROBE_FAILED"} <= set(types["tenant-b"])
    pods = Counter(p.tenant_id for p in mem_db.scalars(select(PodState)))
    assert pods[world["tenant-a"].id] > 0 and pods[world["tenant-b"].id] > 0
    workloads = mem_db.scalars(select(Resource).where(Resource.resource_type == "cce_workload")).all()
    assert workloads and all(w.tenant_id is not None for w in workloads)
    before = mem_db.scalar(select(func.count()).select_from(PodEvent))
    assert _collect(mem_db, "tenant-a", mock_http).status == "SUCCESS"        # 再采一次：同一事件只累加，不重复记录
    assert mem_db.scalar(select(func.count()).select_from(PodEvent)) == before


def test_pod_events_api(client, admin, tenant_ids):
    st = client.get("/api/v1/pod-events/stats", headers=admin).json()["data"]
    assert st["total"] > 0 and st["restarts"] > 0 and st["ongoing"] > 0 and st["pods"]["total"] > 0
    assert [t["type"] for t in st["by_type"]][:4] == ["RESTART", "OOM_KILLED", "CRASH_LOOP", "BACKPRESSURE"]
    assert len(st["trend"]["labels"]) in (24, 25) and set(st["trend"]["series"]) == {"restart", "backpressure", "other"}
    assert st["top_workloads"][0]["critical"] >= st["top_workloads"][-1]["critical"]
    page = client.get("/api/v1/pod-events", params={"event_type": ["RESTART", "OOM_KILLED"], "page_size": 100},
                      headers=admin).json()["data"]
    assert page["total"] > 0 and {x["event_type"] for x in page["items"]} <= {"RESTART", "OOM_KILLED"}
    ongoing = client.get("/api/v1/pod-events", params={"ongoing": True}, headers=admin).json()["data"]["items"]
    assert ongoing and all(x["ongoing"] and x["end_time"] is None for x in ongoing)
    b = client.get("/api/v1/pod-events", params={"page_size": 100},
                   headers={**admin, "X-Tenant-Id": str(tenant_ids["tenant-b"])}).json()["data"]["items"]
    assert b and {x["tenant_name"] for x in b} == {"租户B"}
    opts = client.get("/api/v1/pod-events/options", headers=admin).json()["data"]
    assert {c["cluster_name"] for c in opts["clusters"]} == {"cce-prod-01", "cce-test-01", "cce-web-01"}
    ev = next(x for x in page["items"] if x["event_type"] == "RESTART")
    key = f"{ev['cluster_id']}/{ev['namespace']}/{ev['pod']}"
    tl = client.get("/api/v1/pod-events/pod", params={"key": key}, headers=admin).json()["data"]
    assert tl["events"] and tl["restarts_in_window"] >= 1 and tl["state"]["namespace"] == ev["namespace"]
    assert client.get("/api/v1/pod-events/pod", params={"key": "bad"}, headers=admin).status_code == 400
    other = "wangfang" if ev["tenant_id"] == tenant_ids["tenant-a"] else "lina"
    assert client.get("/api/v1/pod-events/pod", params={"key": key},
                      headers=demo_login(client, other)).status_code == 404


def test_probe_config_requires_permission(client, tenant_ids):
    body = {"name": "测试背压探针", "namespace": "trade", "selector": "app=order-service", "port": 9100,
            "metric": "order_queue_lag", "agg": "max", "op": ">=", "threshold": 1000, "unit": " 条"}
    oncall = demo_login(client, "oncall", tenant_ids["tenant-a"])
    assert client.post("/api/v1/pod-events/probes", json=body, headers=oncall).status_code == 403
    lead = demo_login(client, "zhaolei", tenant_ids["tenant-a"])
    bad = client.post("/api/v1/pod-events/probes", json={**body, "metric": "bad metric"}, headers=lead)
    assert bad.status_code == 400
    r = client.post("/api/v1/pod-events/probes", json=body, headers=lead)
    assert r.status_code == 200 and r.json()["data"]["tenant_id"] == tenant_ids["tenant-a"]
    pid = r.json()["data"]["id"]
    up = client.put(f"/api/v1/pod-events/probes/{pid}", json={**body, "threshold": 2000, "enabled": False}, headers=lead)
    assert up.status_code == 200 and up.json()["data"]["threshold"] == 2000 and up.json()["data"]["enabled"] is False
    wangfang = demo_login(client, "wangfang")
    assert client.put(f"/api/v1/pod-events/probes/{pid}", json=body, headers=wangfang).status_code in (403, 404)
    assert client.delete(f"/api/v1/pod-events/probes/{pid}", headers=lead).status_code == 200
    admin = demo_login(client, "admin")
    actions = [a["action"] for a in client.get("/api/v1/system/audit-logs", params={"action": ["probe.*"]},
                                                 headers=admin).json()["data"]["items"]]
    assert actions.count("probe.save") >= 2 and "probe.delete" in actions
