"""DCS 原生历史与授权；共用查询缓存的通用回归仍独立保留。"""
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app.core.deps import Scope
from app.hcs.query_cache import QueryCache
from app.metrics.dcs_samples import RawMemoryPoint
from app.models import HcsAccount, HcsProject
from app.services import dcs_history_service as service

from .test_dcs_history_api import START, install_aom, params, query, resource, sample
from .test_dcs_history_api import dcs_api as dcs_api


def test_history_export_and_aggregation_intervals_use_same_native_samples_without_cloud(mem_db, dcs_api, monkeypatch):
    client = dcs_api[0]
    a, b = resource(mem_db), resource(mem_db)
    for row in (a, b):
        sample(mem_db, row, 0, 0, used=0, capacity=1024)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)])
    assert query(client, [a, b])["series"][0]["summary"]["average_usage"] == 0
    reversed_data = query(client, [b, a], interval=5)
    assert [s["instance"]["id"] for s in reversed_data["series"]] == [b.id, a.id]
    assert client.get("/api/v1/dcs/export", params=params([a, b])).status_code == 200
    a.name = "新实例名"
    mem_db.commit()
    data = query(client, [a, b])
    assert data["series"][0]["instance"]["name"] == "新实例名"
    assert data["series"][0]["points"][1]["memory_usage"] is None
    assert cloud.calls == dcs_api[3] == []


def test_native_scope_checks_and_same_cloud_id_never_cross_project(mem_db, dcs_api, monkeypatch):
    client, user, _app, _calls = dcs_api
    rows = [resource(mem_db, resource_id="same", project_ref=11),
            resource(mem_db, resource_id="same", project_ref=12),
            resource(mem_db, resource_id="same", tenant_id=2, project_ref=21)]
    for row in rows:
        sample(mem_db, row, 0, row.project_ref)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)], tenant_ids=(1, 2))
    user.tenant_ids = None
    assert [s["summary"]["average_usage"] for s in query(client, rows)["series"]] == [11, 12, 21]
    user.tenant_ids = [1]
    for endpoint in ("history", "export"):
        assert client.get(f"/api/v1/dcs/{endpoint}", params=params([rows[2]])).status_code == 404
    assert [s["summary"]["average_usage"] for s in query(client, rows[:2])["series"]] == [11, 12]
    assert cloud.calls == dcs_api[3] == []


def test_cloud_credentials_endpoint_mapping_and_range_do_not_change_native_history(mem_db, dcs_api, monkeypatch):
    from app.models.inspection import InspectionTask

    client = dcs_api[0]
    row = resource(mem_db)
    sample(mem_db, row, 0, 25)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)])
    tenant, project = mem_db.get(HcsAccount, 1), mem_db.get(HcsProject, 11)
    for field, value in (("secret_cipher", "synthetic-new-cipher"),
                         ("endpoints", {"aom": "https://changed-aom.example.invalid"})):
        setattr(tenant, field, value)
        mem_db.commit()
        assert query(client, [row])["series"][0]["summary"]["average_usage"] == 25
    project.project_id = "changed-project"
    mem_db.get(InspectionTask, "dcs_status").params = {"aom": {"invalid": True}}
    mem_db.commit()
    assert query(client, [row], end=START + timedelta(hours=1))["series"][0]["summary"]["average_usage"] == 25
    sample(mem_db, row, 6, 75)
    assert query(client, [row])["series"][0]["summary"]["average_usage"] == 50
    assert cloud.calls == dcs_api[3] == []


def test_disabled_tenant_retains_owned_native_samples_without_requerying_cloud(mem_db, dcs_api, monkeypatch):
    client = dcs_api[0]
    row = resource(mem_db)
    sample(mem_db, row, 0, 25)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)])
    assert query(client, [row])["series"][0]["source"] == "LOCAL"
    mem_db.get(HcsAccount, 1).enabled = False
    mem_db.commit()
    data = query(client, [row])["series"][0]
    assert data["source"] == "LOCAL" and data["warning"] and data["summary"]["average_usage"] == 25
    assert cloud.calls == dcs_api[3] == []


def test_singleflight_has_one_loader_and_returns_independent_copies():
    cache, calls, barrier = QueryCache(), [], threading.Barrier(8)
    def load():
        calls.append(1)
        time.sleep(0.04)
        return {"failed": False, "rows": {"one": [0, 35]}}
    def get(_index):
        barrier.wait(2)
        return cache.get("synthetic-query-key", load)[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(get, range(8)))
    assert len(calls) == 1 and all(v == values[0] for v in values)
    values[0]["rows"]["one"][0] = 99
    assert cache.get("synthetic-query-key", load)[0]["rows"]["one"][0] == 0


def test_partial_results_have_short_ttl_and_do_not_turn_missing_values_into_zero():
    cache, calls = QueryCache(), []
    def load():
        calls.append(1)
        return {"failed": True, "rows": {"one": [[None, 4]]}}
    assert cache.get("synthetic-partial-key", load)[0]["failed"]
    assert cache.get("synthetic-partial-key", load)[0]["rows"]["one"][0][0] is None
    assert calls == [1]
    entry = cache.entries[(None, "synthetic-partial-key")]
    assert 0 < entry.expires - time.monotonic() <= 2
    entry.expires = 0
    cache.get("synthetic-partial-key", load)
    assert calls == [1, 1]


def test_cache_total_bytes_are_bounded_and_completed_results_only_are_evicted():
    cache, rng = QueryCache(), random.Random(17)
    cache.MAX_CACHE_BYTES = 3000
    for i in range(12):
        text = "".join(rng.choices(string.ascii_letters, k=2000))
        cache.get(str(i), lambda text=text: {"failed": False, "value": text})
        assert cache.bytes <= 3000
        assert cache.bytes == sum(len(entry.value) for entry in cache.entries.values() if entry.value)
    assert len(cache.entries) < 12


def test_byte_eviction_does_not_remove_pending_singleflight():
    cache, started, release, calls = QueryCache(), threading.Event(), threading.Event(), []
    cache.MAX_CACHE_BYTES = 1500
    def pending():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {"failed": False, "value": "pending"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get, "pending", pending)
        assert started.wait(2)
        rng = random.Random(18)
        for i in range(4):
            text = "".join(rng.choices(string.ascii_letters, k=1500))
            cache.get(str(i), lambda text=text: {"failed": False, "value": text})
        second = pool.submit(cache.get, "pending", pending)
        release.set()
        assert first.result()[0] == second.result()[0]
    assert calls == [1] and cache.bytes <= 1500


def test_twenty_projects_full_month_without_mapping_never_make_cloud_requests(mem_db, dcs_api):
    user = dcs_api[1]
    rows = []
    for i in range(20):
        pid = 100 + i
        mem_db.add(HcsProject(id=pid, account_id=1, region_id="test", project_id=f"project-{pid}",
                              external_domain="test.invalid"))
        mem_db.commit()
        rows.append(resource(mem_db, resource_id="same-cloud-id", project_ref=pid,
                             metrics={"memory_usage": 95, "max_memory_mb": 8192}))
    tenant = mem_db.get(HcsAccount, 1)
    tenant.metrics_source = "AOM"
    tenant.endpoints = {"aom": "https://aom.example.invalid"}
    mem_db.commit()
    selection = service.MemoryQuery.create([r.id for r in reversed(rows)], START, START + timedelta(days=31), 60)
    data = service.memory_history(mem_db, Scope([1], False, user), selection)
    assert [s.instance.id for s in data.series] == [r.id for r in reversed(rows)]
    assert len(data.series) == 20 and all(s.warning and s.source == "NONE" for s in data.series)
    assert all(len(s.points) == 31 * 24 and s.valid_points == 0 for s in data.series)
    assert all(p.memory_usage is None and p.max_memory_mb is None for s in data.series for p in s.points)
    assert dcs_api[3] == []
