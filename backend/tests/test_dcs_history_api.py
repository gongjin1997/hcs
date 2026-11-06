"""DCS 内存历史/API/XLSX 契约：独立库、合成观测，不访问真实 HCS。"""

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.v1.dcs import router
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.hcs import ratelimit
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.main import app as production_app
from app.metrics.dcs_samples import RawMemoryPoint
from app.models import HcsAccount, HcsProject, Resource
from app.models.dcs import DcsMemorySample
from app.models.inspection import InspectionTask
from app.schemas.dcs import DcsHistoryOut, DcsInstancesOut

START = datetime(2026, 10, 1, 0, 2, tzinfo=UTC)


@pytest.fixture()
def dcs_api(mem_db, monkeypatch):
    for tid in (1, 2):
        mem_db.add(
            HcsAccount(
                id=tid,
                code=f"dcs-test-{tid}",
                name=f"租户{tid}",
                iam_endpoint="https://example.invalid",
                domain_name=f"tenant-{tid}",
                enabled=True,
                metrics_source="AUTO",
            )
        )
    mem_db.flush()
    for pid, tid in ((11, 1), (12, 1), (21, 2)):
        mem_db.add(
            HcsProject(
                id=pid,
                account_id=tid,
                region_id="region-test",
                project_id=f"project-{pid}",
                project_name=f"项目{pid}",
                external_domain="example.invalid",
                enabled=True,
            )
        )
    mem_db.commit()
    calls = []
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    monkeypatch.setattr(get_settings(), "hcs_endpoints", {})
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", False)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 100000)
    monkeypatch.setattr(ratelimit, "_shared", {})

    def forbidden_cloud(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("DCS 历史和导出不应调用任何云接口")

    monkeypatch.setattr(HcsClient, "request", forbidden_cloud)
    user = CurrentUser(id=1, username="dcs-reader", display_name="DCS 报表读者", permissions=["report:view"], tenant_ids=[1])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, user, app, calls


# 合成自定义指标，仅用于验证显式映射；不声称 HCS 内置此 namespace/dimension。
AOM_MAPPING = {"namespace": "CUSTOM.DcsTest", "dimension": "cache_id",
               "metrics": {"memory_usage": "memory_used_percent"}}


def enable_aom(db, tenant_ids=(1,), mapping=None):
    for tid in tenant_ids:
        tenant = db.get(HcsAccount, tid)
        tenant.metrics_source = "AOM"
        tenant.endpoints = {"aom": "https://aom.example.invalid"}
    task = db.get(InspectionTask, "dcs_status")
    if task is None:
        task = InspectionTask(code="dcs_status", name="DCS 状态", service="dcs", interval_sec=300, params={})
        db.add(task)
    task.params = {**(task.params or {}), "aom": mapping if mapping is not None else AOM_MAPPING}
    db.commit()


class AomCloud:
    """仅替换 HTTP 传输；服务、目录发现、样本解析、缓存和权限均运行真实实现。"""

    def __init__(self, points=None):
        self.points = points if points is not None else []
        self.calls = []
        self.catalogue_unit = "Percent"
        self.point_unit = "Percent"
        self.metadata_status = 200
        self.sample_status = 200
        self.fail_sample_after = None
        self.sample_calls = 0
        self.catalogue_rows = None

    def client(self, tenant, project):
        def respond(request):
            body = json.loads(request.content)
            self.calls.append({"tenant_id": tenant.id, "project_ref": project.id, "path": request.url.path,
                               "endpoint": str(request.url), "body": body, "params": dict(request.url.params)})
            if request.url.path == f"/v1/{project.project_id}/ams/metrics":
                if self.metadata_status != 200:
                    return httpx.Response(self.metadata_status, json={"error": {"code": "AOM.UNAVAILABLE"}})
                assert set(body) == {"metricItems"}
                rows = [{"namespace": item["namespace"], "metricName": item["metricName"],
                         "dimensions": item["dimensions"], "unit": self.catalogue_unit}
                        for item in body["metricItems"]]
                if self.catalogue_rows is not None:
                    rows = self.catalogue_rows
                return httpx.Response(200, json={"metrics": rows, "metaData": {"count": len(rows), "start": None,
                                                                            "total": len(rows)}})
            assert request.url.path == f"/v2/{project.project_id}/samples", request.url
            assert dict(request.url.params) == {"fill_value": "null"}
            assert set(body) == {"samples", "period", "statistics", "time_range"}
            assert body["period"] == 300 and body["statistics"] == ["average", "maximum"]
            self.sample_calls += 1
            if self.sample_status != 200 or (self.fail_sample_after is not None
                                             and self.sample_calls > self.fail_sample_after):
                return httpx.Response(self.sample_status if self.sample_status != 200 else 503,
                                      json={"error": {"code": "AOM.UNAVAILABLE"}})
            first, last, _minutes = map(int, body["time_range"].split("."))
            samples = []
            for item in body["samples"]:
                points = self.points(tenant, project, item, first, last) if callable(self.points) else self.points
                values = []
                for point in points:
                    stats = [{"statistic": key, "value": value} for key, value in
                             (("average", point.memory_usage), ("maximum", point.memory_max)) if value is not None]
                    values.append({"timestamp": int(point.at.timestamp() * 1000), "unit": self.point_unit,
                                   "statistics": stats})
                samples.append({"sample": item, "data_points": values})
            return httpx.Response(200, json={"samples": samples})

        http = httpx.Client(transport=httpx.MockTransport(respond))
        return HcsClient(http, AkSkAuth(f"synthetic-ak-{tenant.id}", tenant.secret_cipher or "synthetic-sk"),
                         EndpointResolver(tenant.endpoints, project.region_id, project.external_domain),
                         project.project_id, tenant_id=tenant.id, project_ref=project.id,
                         qps={"aom": 100000}, max_retries=0)


def install_aom(db, monkeypatch, points=None, tenant_ids=(1,), mapping=None):
    enable_aom(db, tenant_ids, mapping)
    cloud = AomCloud(points)

    return cloud


def resource(db, **extra):
    row = Resource(
        **{
            "service": "dcs",
            "resource_type": "dcs_instance",
            "resource_id": uuid4().hex,
            "name": "缓存实例",
            "tenant_id": 1,
            "project_ref": 11,
            "region": "region-test",
            "status_raw": "RUNNING",
            "lifecycle": "ACTIVE",
            "attrs": {"engine": "Redis"},
            "metrics": {},
            **extra,
        }
    )
    db.add(row)
    db.commit()
    return row


def sample(db, row, minutes, usage, *, used=None, capacity=None, **extra):
    point = DcsMemorySample(
        **{
            "resource_ref": row.id,
            "tenant_id": row.tenant_id,
            "project_ref": row.project_ref,
            "observed_at": START + timedelta(minutes=minutes),
            "memory_usage": usage,
            "used_memory_mb": used,
            "max_memory_mb": capacity,
            **extra,
        }
    )
    db.add(point)
    db.commit()
    return point


def params(rows, *, start=START, end=None, interval=10):
    end_value = end if end is not None else START + timedelta(minutes=25)
    return [("resource_ids", row.id) for row in rows] + [
        ("start_at", start.isoformat() if isinstance(start, datetime) else start),
        ("end_at", end_value.isoformat() if isinstance(end_value, datetime) else end_value),
        ("interval_minutes", interval),
    ]


def query(client, rows, **options):
    response = client.get("/api/v1/dcs/history", params=params(rows, **options))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0 and body["message"] == "ok"
    DcsHistoryOut.model_validate(body["data"])
    return body["data"]


def listed(client, **options):
    response = client.get("/api/v1/dcs/instances", params=options)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0 and body["message"] == "ok"
    DcsInstancesOut.model_validate(body["data"])
    return body["data"]


def sheet_rows(sheet):
    rows = list(sheet.iter_rows())
    keys = [cell.value for cell in rows[0]]
    return [{key: cell for key, cell in zip(keys, row, strict=True)} for row in rows[1:]]


def test_list_filters_scope_pagination_and_snapshot_sanitization(mem_db, dcs_api):
    client, user, _, calls = dcs_api
    same_time = START - timedelta(days=3)
    first = resource(
        mem_db,
        name="needle",
        resource_id="cloud-a",
        last_seen=same_time,
        metrics={"memory_usage": 0, "used_memory_mb": 0, "max_memory_mb": 2048},
    )
    second = resource(
        mem_db,
        name="needle",
        resource_id="cloud-b",
        project_ref=12,
        metrics={"memory_usage": "not-a-number", "used_memory_mb": False, "max_memory_mb": -1},
    )
    ordinary = resource(mem_db, name="other", resource_id="cloud-c")
    other = resource(mem_db, name="needle", resource_id="cloud-d", tenant_id=2, project_ref=21)
    resource(mem_db, name="needle-deleted", lifecycle="DELETED")
    resource(mem_db, name="needle-ecs", service="ecs", resource_type="ecs_server")
    legacy = resource(mem_db, name="legacy", tenant_id=None, project_ref=None)
    first_page = listed(client, keyword="  NEEDLE ", page_size=1)
    next_page = listed(client, keyword="needle", page=2, page_size=1)
    assert first_page["total"] == next_page["total"] == 2
    assert (first_page["page"], first_page["page_size"]) == (1, 1)
    assert first_page["items"][0]["id"] == first.id and next_page["items"][0]["id"] == second.id
    item = first_page["items"][0]
    assert (item["tenant_name"], item["project_name"], item["status_raw"]) == ("租户1", "项目11", "RUNNING")
    assert (item["memory_usage"], item["used_memory_mb"], item["max_memory_mb"]) == (None, None, None)
    assert item["freshness"] == "NO_DATA"
    assert datetime.fromisoformat(item["snapshot_at"]) == same_time
    assert all(next_page["items"][0][key] is None for key in ("memory_usage", "used_memory_mb", "max_memory_mb"))
    assert listed(client, keyword="cloud-b")["items"][0]["id"] == second.id
    assert listed(client, project_ref=12)["items"][0]["id"] == second.id
    assert listed(client, project_ref=21)["total"] == 0
    assert listed(client, keyword="not-found")["items"] == []
    assert listed(client, page=999)["total"] == 3 and listed(client, page=999)["items"] == []
    assert client.get("/api/v1/dcs/instances", params={"tenant_id": 2}).status_code == 404
    assert client.get("/api/v1/dcs/instances", headers={"X-Tenant-Id": "2"}).status_code == 403
    user.tenant_ids = None
    assert {r["id"] for r in listed(client)["items"]} == {first.id, second.id, ordinary.id, other.id, legacy.id}
    selected = client.get("/api/v1/dcs/instances", headers={"X-Tenant-Id": "2"}).json()["data"]
    assert [r["id"] for r in selected["items"]] == [other.id]
    user.tenant_ids = []
    assert listed(client)["total"] == 0
    assert calls == []  # 清单只读取快照，不触发云请求或伪造历史。


@pytest.mark.parametrize("endpoint", ["instances", "history", "export"])
def test_all_endpoints_require_report_view_and_login(mem_db, dcs_api, endpoint):
    client, user, app, _ = dcs_api
    row = resource(mem_db)
    query_params = {} if endpoint == "instances" else params([row])
    user.permissions = ["ledger:view"]
    denied = client.get(f"/api/v1/dcs/{endpoint}", params=query_params)
    assert denied.status_code == 403 and denied.json()["code"] == 40301
    user.permissions = ["report:view"]
    allowed = client.get(f"/api/v1/dcs/{endpoint}", params=query_params)
    assert allowed.status_code == 200, allowed.text
    app.dependency_overrides.pop(get_current_user)
    anonymous = client.get(f"/api/v1/dcs/{endpoint}", params=query_params)
    assert anonymous.status_code == 401 and anonymous.json()["code"] == 40101


@pytest.mark.parametrize(
    "options",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"project_ref": 0},
        {"tenant_id": -1},
        {"keyword": "x" * 201},
    ],
)
def test_list_rejects_invalid_filters(dcs_api, options):
    response = dcs_api[0].get("/api/v1/dcs/instances", params=options)
    assert response.status_code == 400 and response.json()["code"] == 40001


@pytest.mark.parametrize("endpoint", ["history", "export"])
@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "too_many",
        "nonpositive",
        "external_id",
        "no_start",
        "no_end",
        "naive_start",
        "naive_end",
        "equal",
        "backwards",
        "too_long",
        "interval",
    ],
)
def test_history_and_export_share_query_validation(mem_db, dcs_api, endpoint, bad):
    client = dcs_api[0]
    row = resource(mem_db)
    values = params([row])
    replacement = {
        "no_start": ("start_at", None),
        "no_end": ("end_at", None),
        "naive_start": ("start_at", START.replace(tzinfo=None).isoformat()),
        "naive_end": ("end_at", (START + timedelta(hours=1)).replace(tzinfo=None).isoformat()),
        "equal": ("end_at", START.isoformat()),
        "backwards": ("end_at", (START - timedelta(seconds=1)).isoformat()),
        "too_long": ("end_at", (START + timedelta(days=31, seconds=1)).isoformat()),
        "interval": ("interval_minutes", 7),
    }
    if bad in replacement:
        key, value = replacement[bad]
        values = [(k, v) for k, v in values if k != key]
        if value is not None:
            values.append((key, value))
    else:
        values = [(k, v) for k, v in values if k != "resource_ids"]
        ids = [] if bad == "missing" else [row.id] * 21 if bad == "too_many" else [0] if bad == "nonpositive" else ["cloud-id"]
        values += [("resource_ids", value) for value in ids]
    response = client.get(f"/api/v1/dcs/{endpoint}", params=values)
    assert response.status_code == 400 and response.json()["code"] == 40001


@pytest.mark.parametrize("interval", [5, 10, 15, 30, 60])
def test_supported_intervals_normalize_offsets_and_preserve_relative_bucket_origin(mem_db, dcs_api, interval):
    row = resource(mem_db)
    start = START.astimezone(ZoneInfo("Asia/Tokyo"))
    data = query(dcs_api[0], [row, row], start=start, end=start + timedelta(minutes=61), interval=interval)
    assert len(data["series"]) == 1  # 重复本地 ID 去重，不产生重复序列。
    assert datetime.fromisoformat(data["start_at"]) == START
    series = data["series"][0]
    assert series["total_points"] == (61 + interval - 1) // interval
    times = [datetime.fromisoformat(p["at"]) for p in series["points"]]
    assert times == [START + timedelta(minutes=interval * i) for i in range(len(times))]
    assert all(time < START + timedelta(minutes=61) for time in times)


def test_maximum_range_and_selection_are_accepted(mem_db, dcs_api):
    rows = [resource(mem_db, name=f"cache-{i}") for i in range(20)]
    selected = query(dcs_api[0], rows, end=START + timedelta(hours=1), interval=60)
    assert len(selected["series"]) == 20
    maximum = query(dcs_api[0], rows[:1], end=START + timedelta(days=31), interval=60)
    assert maximum["series"][0]["total_points"] == 31 * 24


@pytest.mark.parametrize("endpoint", ["history", "export"])
def test_unavailable_resource_ids_are_indistinguishable_and_do_not_call_upstream(mem_db, dcs_api, endpoint):
    client, user, _, calls = dcs_api
    own = resource(mem_db)
    foreign = resource(mem_db, tenant_id=2, project_ref=21)
    deleted = resource(mem_db, lifecycle="DELETED")
    not_dcs = resource(mem_db, service="ecs", resource_type="ecs_server")
    missing = SimpleNamespace(id=max(own.id, foreign.id, deleted.id, not_dcs.id) + 100)
    errors = []
    for row in (foreign, deleted, not_dcs, missing):
        response = client.get(f"/api/v1/dcs/{endpoint}", params=params([own, row]))
        assert response.status_code == 404
        errors.append(response.json())
    assert all(error == errors[0] for error in errors)
    assert errors[0]["code"] == 40401
    assert calls == []
    user.tenant_ids = []
    assert client.get(f"/api/v1/dcs/{endpoint}", params=params([own])).status_code == 404


def test_current_snapshot_does_not_fabricate_history_and_empty_is_null(mem_db, dcs_api):
    row = resource(
        mem_db,
        metrics={"memory_usage": 97.5, "used_memory_mb": 9984, "max_memory_mb": 10240},
        last_seen=START + timedelta(minutes=1),
    )
    series = query(dcs_api[0], [row])["series"][0]
    assert series["instance"]["memory_usage"] is None  # 无原生来源和采样时间的快照不能作为当前值。
    assert (series["source"], series["status"], series["valid_points"], series["total_points"]) == ("NONE", "NO_DATA", 0, 3)
    assert series["warning"]
    assert series["summary"] == {"average_usage": None, "peak_usage": None, "latest_usage": None, "latest_at": None}
    for point in series["points"]:
        assert point["sample_count"] == 0
        assert all(point[key] is None for key in ("memory_usage", "memory_max", "used_memory_mb", "max_memory_mb"))


@pytest.mark.parametrize("reason", ["no_mapping", "no_endpoint", "aom_disabled", "legacy_ces", "invalid_mapping",
                                    "tenant_disabled", "project_disabled", "no_data", "failed", "exception", "wrong_unit"])
def test_local_actual_samples_are_independent_of_legacy_aom_configuration(mem_db, dcs_api, monkeypatch, reason):
    client, _, _, forbidden_calls = dcs_api
    cloud = None if reason == "no_mapping" else install_aom(mem_db, monkeypatch)
    row = resource(mem_db, metrics={"memory_usage": 88})
    sample(mem_db, row, -1, 99)
    sample(mem_db, row, 1, 0, used=0, capacity=1024)
    sample(mem_db, row, 6, 20, used=200, capacity=2048)
    sample(mem_db, row, 11, 40, used=400, capacity=4096)
    sample(mem_db, row, 25, 100)  # end_at 右开，不应进入结果。
    tenant = mem_db.get(HcsAccount, 1)
    if reason == "no_mapping":
        tenant.metrics_source, tenant.endpoints = "AOM", {"aom": "https://aom.example.invalid"}
    elif reason == "no_endpoint":
        tenant.endpoints = {}
    elif reason in ("aom_disabled", "legacy_ces"):
        tenant.metrics_source = "NONE" if reason == "aom_disabled" else "CES"
    elif reason == "invalid_mapping":
        mem_db.get(InspectionTask, "dcs_status").params = {"aom": {"namespace": "CUSTOM.DcsTest"}}
    elif reason == "tenant_disabled":
        tenant.enabled = False
    elif reason == "project_disabled":
        mem_db.get(HcsProject, 11).enabled = False
    elif reason == "failed":
        cloud.sample_status = 503
    elif reason == "exception":
        cloud.metadata_status = 429
    elif reason == "wrong_unit":
        cloud.points = [RawMemoryPoint(START, 99, 100)]
        cloud.catalogue_unit = cloud.point_unit = "Count"
    mem_db.commit()
    series = query(client, [row])["series"][0]
    assert series["source"] == "LOCAL" and series["status"] == "PARTIAL"
    assert bool(series["warning"]) == (reason in ("tenant_disabled", "project_disabled"))
    assert [p["sample_count"] for p in series["points"]] == [2, 1, 0]
    assert [p["memory_usage"] for p in series["points"]] == [10, 40, None]
    assert series["summary"]["average_usage"] == 20  # 三个原始点加权，非桶平均值 25。
    assert series["summary"]["peak_usage"] == 40
    assert datetime.fromisoformat(series["summary"]["latest_at"]) == START + timedelta(minutes=11)
    assert series["points"][0]["used_memory_mb"] == 100
    assert series["points"][0]["max_memory_mb"] == 2048
    assert forbidden_calls == []
    assert cloud is None or cloud.calls == []


def test_zero_is_a_valid_observation_not_missing(mem_db, dcs_api):
    row = resource(mem_db)
    sample(mem_db, row, 0, 0, used=0, capacity=1024)
    series = query(dcs_api[0], [row], end=START + timedelta(minutes=5), interval=5)["series"][0]
    assert series["source"] == "LOCAL" and series["status"] == "AVAILABLE"
    assert series["valid_points"] == series["total_points"] == 1
    assert series["points"][0]["memory_usage"] == series["summary"]["average_usage"] == 0
    assert series["points"][0]["used_memory_mb"] == series["summary"]["peak_usage"] == 0


@pytest.mark.parametrize("legacy", ["no_project", "wrong_project", "no_tenant"])
def test_legacy_or_inconsistent_project_uses_only_owned_local_records(mem_db, dcs_api, legacy):
    client, user, _, calls = dcs_api
    overrides = {"project_ref": None} if legacy == "no_project" else {"project_ref": 21}
    if legacy == "no_tenant":
        overrides = {"project_ref": None, "tenant_id": None}
        user.tenant_ids = None
    row = resource(mem_db, **overrides)
    sample(mem_db, row, 1, 25)
    # 历史异常关联记录不得污染均值，即使 resource_ref 正好相同。
    sample(mem_db, row, 2, 99, tenant_id=2)
    sample(mem_db, row, 3, 98, project_ref=12)
    series = query(client, [row])["series"][0]
    assert series["source"] == "LOCAL" and series["summary"]["average_usage"] == 25
    assert series["points"][0]["sample_count"] == 1
    assert series["instance"]["project_name"] is None
    assert series["warning"] and calls == []


def test_native_samples_aggregate_all_raw_points_relative_to_start_and_preserve_peak(mem_db, dcs_api, monkeypatch):
    row = resource(mem_db)
    points = [(7, 40, 400, 2048), (1, 0, 0, 1024), (12, 60, 600, 8192),
              (3, 20, 200, 4096), (-1, 99, 999, 99999), (25, 99, 999, 99999)]
    for minute, value, used, capacity in points:
        sample(mem_db, row, minute, value, used=used, capacity=capacity)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)])
    series = query(dcs_api[0], [row])["series"][0]
    assert series["source"] == "LOCAL" and series["status"] == "PARTIAL" and series["warning"] is None
    assert [p["memory_usage"] for p in series["points"]] == [20, 60, None]
    assert [p["sample_count"] for p in series["points"]] == [3, 1, 0]
    assert [p["memory_max"] for p in series["points"]] == [40, 60, None]
    assert series["summary"]["average_usage"] == 30
    assert series["summary"]["peak_usage"] == series["summary"]["latest_usage"] == 60
    assert datetime.fromisoformat(series["summary"]["latest_at"]) == START + timedelta(minutes=12)
    assert series["points"][0]["used_memory_mb"] == 200
    assert series["points"][0]["max_memory_mb"] == 2048
    assert cloud.calls == dcs_api[3] == []


def test_legacy_aom_cannot_fill_native_history_gaps_or_current_snapshot(mem_db, dcs_api, monkeypatch):
    row = resource(mem_db, metrics={"memory_usage": 85, "max_memory_mb": 4096})
    sample(mem_db, row, 12, 99, used=4055, capacity=4096)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 0, 2)])
    series = query(dcs_api[0], [row])["series"][0]
    assert series["source"] == "LOCAL" and series["status"] == "PARTIAL"
    assert series["valid_points"] == 1 and series["summary"]["average_usage"] == 99
    assert [p["memory_usage"] for p in series["points"]] == [None, 99, None]
    assert series["points"][0]["max_memory_mb"] is None
    assert series["points"][1]["max_memory_mb"] == 4096
    assert cloud.calls == dcs_api[3] == []


def test_native_samples_isolate_same_cloud_id_per_project_tenant_and_engine(mem_db, dcs_api, monkeypatch):
    client, user, _, _ = dcs_api
    user.tenant_ids = None
    a = resource(mem_db, resource_id="same-cloud-id", project_ref=11)
    b = resource(mem_db, resource_id="same-cloud-id", project_ref=12)
    c = resource(mem_db, resource_id="same-cloud-id", tenant_id=2, project_ref=21)
    memcached = resource(mem_db, resource_id="memcached-id", attrs={"engine": "Memcached"})
    for row, value in ((a, 11), (b, 12), (c, 21), (memcached, 31)):
        sample(mem_db, row, 0, value)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)], tenant_ids=(1, 2))
    data = query(client, [b, c, a, memcached], end=START + timedelta(minutes=5), interval=5)
    assert [s["instance"]["id"] for s in data["series"]] == [b.id, c.id, a.id, memcached.id]
    assert [s["summary"]["average_usage"] for s in data["series"]] == [12, 21, 11, 31]
    assert all(s["source"] == "LOCAL" and s["status"] == "AVAILABLE" for s in data["series"])
    user.tenant_ids = [1]
    for endpoint in ("history", "export"):
        denied = client.get(f"/api/v1/dcs/{endpoint}", params=params([b, c]))
        assert denied.status_code == 404
    assert cloud.calls == dcs_api[3] == []


@pytest.mark.parametrize("legacy_aom", [False, True])
def test_excel_contains_complete_selection_and_range_with_native_numbers_and_text(mem_db, dcs_api, monkeypatch, legacy_aom):
    client = dcs_api[0]
    tenant = mem_db.get(HcsAccount, 1)
    project = mem_db.get(HcsProject, 11)
    tenant.name, project.project_name = "+tenant-value", "=project-value"
    mem_db.commit()
    names = ['=HYPERLINK("https://example.invalid","text")', "+SUM(1,2)", "-1+2", "@external", "\t=formula"]
    rows = [resource(mem_db, name=name, resource_id=f"=cloud-id-{i}", region="@region") for i, name in enumerate(names)]
    points = [
        RawMemoryPoint(START, 0, 10, 0, 1024),
        RawMemoryPoint(START + timedelta(minutes=1), 20, 35, 200, 2048),
        RawMemoryPoint(START + timedelta(minutes=12), 40, 80, 400, 4096),
    ]
    rows = [rows[3], rows[1], rows[4], rows[0], rows[2]]  # 导出保持用户选择顺序。
    if legacy_aom:
        install_aom(mem_db, monkeypatch, points)
    for row in rows:
        for point in points:
            minutes = (point.at - START).total_seconds() / 60
            sample(mem_db, row, minutes, point.memory_usage, used=point.used_memory_mb, capacity=point.max_memory_mb)
    assert len(listed(client, page_size=1)["items"]) == 1
    history = query(client, rows)
    response = client.get("/api/v1/dcs/export", params=params(rows) + [("page", 2), ("page_size", 1)])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "attachment;" in response.headers["content-disposition"]
    workbook = load_workbook(io.BytesIO(response.content), data_only=False)
    try:
        assert workbook.sheetnames == ["查询条件", "实例汇总", "内存指标"]
        conditions = {a.value: b for a, b in list(workbook["查询条件"].iter_rows())[1:]}
        assert conditions["实例数"].value == len(rows) and conditions["实例数"].data_type == "n"
        assert conditions["时间粒度（分钟）"].value == 10
        zone = ZoneInfo(history["timezone"])
        assert conditions["起始时间（含）"].value == START.astimezone(zone).replace(tzinfo=None)
        assert conditions["结束时间（不含）"].value == (START + timedelta(minutes=25)).astimezone(zone).replace(tzinfo=None)
        summaries = sheet_rows(workbook["实例汇总"])
        assert len(summaries) == len(rows)
        for saved, series in zip(summaries, history["series"], strict=True):
            assert saved["来源"].value == "DCS 接口采样"
            assert saved["实例 ID"].value == series["instance"]["resource_id"]
            assert saved["实例名称"].value == series["instance"]["name"]
            for key, expected in (
                ("平均使用率（%）", series["summary"]["average_usage"]),
                ("峰值使用率（%）", series["summary"]["peak_usage"]),
                ("最近使用率（%）", series["summary"]["latest_usage"]),
                ("有效时段", series["valid_points"]),
                ("总时段", series["total_points"]),
            ):
                assert saved[key].value == pytest.approx(expected) and saved[key].data_type == "n"
        trend = sheet_rows(workbook["内存指标"])
        assert len(trend) == len(rows) * 3  # 全选择、全时段，不受上方实例列表分页影响。
        for index, saved in enumerate(trend):
            series = history["series"][index // 3]
            point = series["points"][index % 3]
            assert saved["实例 ID"].value == series["instance"]["resource_id"]
            at = datetime.fromisoformat(point["at"])
            assert saved["时段起始时间"].value == at.astimezone(zone).replace(tzinfo=None)
            stop = min(at + timedelta(minutes=10), START + timedelta(minutes=25))
            assert saved["时段结束时间（不含）"].value == stop.astimezone(zone).replace(tzinfo=None)
            for column, key in (
                ("平均使用率（%）", "memory_usage"),
                ("峰值使用率（%）", "memory_max"),
                ("平均已用内存（MB）", "used_memory_mb"),
                ("末次容量（MB）", "max_memory_mb"),
            ):
                assert saved[column].value == point[key]
                if point[key] is not None:
                    assert saved[column].data_type == "n"
            assert saved["有效采样数"].value == point["sample_count"]
        assert trend[0]["平均使用率（%）"].value == 10
        assert trend[0]["峰值使用率（%）"].value == 20
        assert trend[2]["平均使用率（%）"].value is None
        assert trend[2]["有效采样数"].value == 0
        for sheet in workbook:
            for cells in sheet.iter_rows():
                for cell in cells:
                    assert cell.data_type != "f"
                    if isinstance(cell.value, str):
                        assert cell.data_type == "s", (sheet.title, cell.coordinate, cell.value)
    finally:
        workbook.close()


def test_excel_no_data_stays_blank_and_zero_remains_numeric(mem_db, dcs_api):
    no_data = resource(mem_db, name="empty", metrics={"memory_usage": 95})
    zero = resource(mem_db, name="zero")
    sample(mem_db, zero, 0, 0, used=0, capacity=1024)
    response = dcs_api[0].get(
        "/api/v1/dcs/export", params=params([no_data, zero], end=START + timedelta(minutes=5), interval=5)
    )
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.content), data_only=False)
    try:
        summaries = sheet_rows(workbook["实例汇总"])
        trend = sheet_rows(workbook["内存指标"])
        assert summaries[0]["来源"].value == "暂无数据" and summaries[0]["状态"].value == "无数据"
        for column in ("平均使用率（%）", "峰值使用率（%）", "最近使用率（%）"):
            assert summaries[0][column].value is None
            assert summaries[1][column].value == 0 and summaries[1][column].data_type == "n"
        assert trend[0]["平均使用率（%）"].value is None
        assert trend[1]["平均使用率（%）"].value == 0 and trend[1]["平均使用率（%）"].data_type == "n"
        assert trend[1]["平均已用内存（MB）"].value == 0 and trend[1]["末次容量（MB）"].value == 1024
    finally:
        workbook.close()
