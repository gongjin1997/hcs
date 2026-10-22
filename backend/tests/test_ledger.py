"""资源台账：分类与汇总、筛选、维护管理字段（记录手工变更）、变更记录、导出 Excel、同步比对逻辑。"""
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.config import Settings
from app.hcs import build_client
from app.ledger.sync import sync_ledger
from app.models import Resource, ResourceChange
from app.services.pipeline import run_task
from mock_hcs import inventory
from mock_hcs.app import app as mock_app

ENDPOINTS = {**{k: "mock" for k in ("dataarts", "mrs", "gaussdb", "dcs", "ces")},
             **{k: f"mock/{k}" for k in ("ecs", "evs", "vpc", "elb", "rds", "dms", "cce", "dws", "css")}}
SETTINGS = Settings(hcs_scheme="http", hcs_iam_url="http://mock", hcs_password="pwd", hcs_project_id="p1",
                    hcs_endpoints=ENDPOINTS)


def _client(settings: Settings = SETTINGS):
    return build_client(settings, http=TestClient(mock_app, base_url="http://mock"))


# ---------------- API（演示数据） ----------------
def test_types_and_summary(client, admin, tenant_ids):
    meta = client.get("/api/v1/resources/types", headers=admin).json()["data"]
    cats = {c["code"]: c for c in meta["categories"]}
    assert list(cats) == ["compute", "storage", "network", "database", "bigdata", "job", "container"]
    types = {t["code"]: t for t in meta["types"]}
    assert types["ecs_server"]["total"] == 49 and types["ecs_server"]["ledger"] is True
    assert types["dgc_realtime_job"]["ledger"] is False and types["dcs_instance"]["inspected"] is True
    assert types["cce_workload"]["ledger"] is False and types["cce_workload"]["inspected"] is True
    assert [c["label"] for c in types["evs_volume"]["columns"]] == ["磁盘类型", "挂载云服务器", "系统盘"]
    assert [c["label"] for c in types["ecs_server"]["columns"]][-3:] == ["CPU(%)", "内存(%)", "磁盘(%)"]
    s = client.get("/api/v1/resources/summary", headers=admin).json()["data"]
    assert s["total"] == 196 and s["type_count"] == 14 and s["unassigned"] == 7
    assert s["idle"] == {"evs_available": 4, "eip_unbound": 2, "ecs_stopped": 4}
    assert s["compute"]["ecs"] == 49 and s["compute"]["vcpus"] > 300 and s["storage_gb"] > 10000
    assert s["ecs_status"] == {"ACTIVE": 43, "SHUTOFF": 4, "ERROR": 1}
    assert {"STATUS", "REBOOT"} <= set(s["changes_7d"]) and s["last_sync"]["status"] == "SUCCESS"
    a = client.get("/api/v1/resources/summary", headers={**admin, "X-Tenant-Id": str(tenant_ids["tenant-a"])})
    b = client.get("/api/v1/resources/summary", headers={**admin, "X-Tenant-Id": str(tenant_ids["tenant-b"])})
    assert (a.json()["data"]["total"], b.json()["data"]["total"]) == (138, 58)
    assert (a.json()["data"]["compute"]["ecs"], b.json()["data"]["compute"]["ecs"]) == (34, 15)


def test_list_filters(client, admin):
    def q(**params):
        return client.get("/api/v1/resources", params=params, headers=admin).json()["data"]

    assert {r["resource_type"] for r in q(category="compute", page_size=100)["items"]} == {"ecs_server", "cce_cluster"}
    assert [r["name"] for r in q(resource_type="ecs_server", keyword="nginx")["items"]] == ["ecs-nginx-01",
                                                                                         "ecs-nginx-02"]
    unassigned = q(category="ledger", unassigned=True, page_size=50)
    assert unassigned["total"] == 7 and "ecs-portal-03" in {r["name"] for r in unassigned["items"]}
    assert q(category="ledger", env_type="TEST")["total"] >= 8 and q(category="ledger", env_type="DEV")["total"] == 2
    assert {r["name"] for r in q(lifecycle="DELETED", page_size=50)["items"]} == {
        "ecs-portal-old-01", "ecs-tmp-build-01", "eip-campaign-01", "eip-web-campaign", "vol-migrate-tmp-02"}
    evs = q(resource_type="evs_volume", keyword="ecs-etl-01-data")["items"][0]
    assert evs["attrs"]["attached_to"] == "ecs-etl-01" and evs["storage_gb"] == 1000
    ecs = q(resource_type="ecs_server", keyword="ecs-search-01")["items"][0]
    assert ecs["status_raw"] == "ERROR" and ecs["tenant_id"] is not None and ecs["attrs"]["metrics_source"] == "PROMETHEUS"
    assert client.get("/api/v1/resources", params={"resource_type": "nope"}, headers=admin).status_code == 400


def test_patch_records_manual_change(client, admin):
    rid = client.get("/api/v1/resources", params={"resource_type": "ecs_server", "keyword": "ecs-nginx-01"},
                     headers=admin).json()["data"]["items"][0]["id"]
    r = client.patch(f"/api/v1/resources/{rid}", headers=admin,
                     json={"owner": "张伟", "business": "运维管理", "env_type": "TEST", "remark": "  "})
    assert r.status_code == 200 and r.json()["data"]["owner"] == "张伟" and r.json()["data"]["remark"] is None
    detail = client.get(f"/api/v1/resources/{rid}", headers=admin).json()["data"]
    last = detail["changes"][0]
    assert last["source"] == "MANUAL" and last["operator"] == "系统管理员"
    assert {c["field"] for c in last["changes"]} == {"owner", "business", "env_type"}
    bad = client.patch(f"/api/v1/resources/{rid}", headers=admin, json={"env_type": "UAT"})
    assert bad.status_code == 400
    audit = client.get("/api/v1/system/audit-logs", params={"action": ["ledger.update"], "page_size": 1},
                       headers=admin).json()["data"]["items"][0]
    assert audit["target_name"] == "ecs-nginx-01" and audit["operator"] == "系统管理员"


def test_changes_and_export(client, admin):
    page = client.get("/api/v1/resources/changes", params={"days": 30, "page_size": 100}, headers=admin).json()["data"]
    kinds = {c["change_type"] for c in page["items"]}
    assert kinds == {"CREATED", "UPDATED", "DELETED", "STATUS", "REBOOT"} and page["total"] >= 40
    upd = client.get("/api/v1/resources/changes", params={"keyword": "ecs-portal-02", "change_type": "UPDATED"},
                     headers=admin).json()["data"]["items"]
    assert any(c["field"] == "vcpus" and c["old"] == 4 and c["new"] == 8 for c in upd[0]["changes"])
    st = client.get("/api/v1/resources/changes", params={"keyword": "ecs-search-01", "change_type": "STATUS"},
                    headers=admin).json()["data"]["items"]
    first = st[0]["changes"][0]
    assert (first["field"], first["old"], first["new"]) == ("status_raw", "ACTIVE", "ERROR")
    r = client.get("/api/v1/resources/export", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[0] == "汇总" and "云服务器 ECS" in wb.sheetnames and len(wb.sheetnames) == 15
    assert wb["云服务器 ECS"].max_row == 50
    one = load_workbook(io.BytesIO(client.get("/api/v1/resources/export", params={"resource_type": "eip"},
                                              headers=admin).content))
    assert one.sheetnames == ["汇总", "弹性公网 IP"]


# ---------------- 同步比对逻辑（独立内存库） ----------------
def test_sync_detects_create_update_delete(mem_db, monkeypatch):
    first = sync_ledger(mem_db, client=_client())
    assert first.status == "SUCCESS" and first.resource_total == 138 and first.abnormal_total == 138
    assert sync_ledger(mem_db, client=_client()).abnormal_total == 0          # 无变化时不产生变更记录

    orig = list(inventory.DCS)
    monkeypatch.setitem(inventory.find("dcs", "redis-rank-01"), "cap", 8)       # 扩容 4G → 8G
    monkeypatch.setattr(inventory, "DCS", [d for d in orig if d["name"] != "redis-lock-01"])   # 释放
    run = sync_ledger(mem_db, client=_client(), types=["dcs_instance"])
    assert run.stats["dcs_instance"] == {"total": 5, "created": 0, "updated": 1, "deleted": 1, "skipped_delete": False}
    changes = {c.name: c for c in mem_db.scalars(select(ResourceChange).where(ResourceChange.detected_at >= run.started_at))}
    expand = {x["field"]: (x["old"], x["new"]) for x in changes["redis-rank-01"].changes}
    assert expand == {"spec": ("Redis 6.0 | 4 GB", "Redis 6.0 | 8 GB"), "memory_gb": (4.0, 8.0)}
    assert changes["redis-lock-01"].change_type == "DELETED"
    lock = mem_db.scalar(select(Resource).where(Resource.name == "redis-lock-01"))
    assert lock.lifecycle == "DELETED" and lock.deleted_at is not None

    monkeypatch.setattr(inventory, "DCS", orig)                                  # 同一资源 ID 再次出现
    back = sync_ledger(mem_db, client=_client(), types=["dcs_instance"])
    assert back.stats["dcs_instance"]["created"] == 1 and lock.lifecycle == "ACTIVE" and lock.deleted_at is None


def test_sync_guards_against_bad_api(mem_db, monkeypatch):
    sync_ledger(mem_db, client=_client())
    monkeypatch.setattr(inventory, "DCS", [])                                     # 接口突然返回空列表
    run = sync_ledger(mem_db, client=_client(), types=["dcs_instance"])
    assert run.stats["dcs_instance"]["skipped_delete"] is True and run.stats["dcs_instance"]["deleted"] == 0
    bad = Settings(**{**SETTINGS.model_dump(), "hcs_endpoints": {**ENDPOINTS, "css": "mock/not-exist"}})
    run = sync_ledger(mem_db, client=_client(bad), types=["css_cluster", "dws_cluster"])
    assert run.status == "PARTIAL" and "CSS" in run.error_summary               # 某类失败：跳过该类，不做释放判定
    css = mem_db.scalars(select(Resource).where(Resource.resource_type == "css_cluster")).all()
    assert len(css) == 2 and all(r.lifecycle == "ACTIVE" for r in css)


@pytest.mark.parametrize("task", ["ledger_sync"])
def test_ledger_sync_runs_as_task(mem_db, task):
    run = run_task(mem_db, task, client=_client(), trigger="MANUAL")
    assert run.task_code == "ledger_sync" and run.status == "SUCCESS" and run.stats["ecs_server"]["total"] == 34
