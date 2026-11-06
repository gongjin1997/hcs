"""Mock HCS 8.5.1：模拟 IAM、各云服务的只读接口、CCE 集群 K8s API 与租户侧 Prometheus（字段格式参照 HCS / 华为云
OpenAPI 与 kube-apiserver v1），用于本地联调、演示与自动化测试。

启动：uvicorn mock_hcs.app:app --port 9000
- 两个演示租户（tenants.py）：租户A 用户名密码换 Token（X-Auth-Token）；租户B AK/SK 签名（SDK-HMAC-SHA256），
  且局点没有云监控 CES（CES 接口返回 404 APIGW.0101），ECS 使用率由 /prom/tenant-b 模拟的 Prometheus 提供
- 巡检用到的服务（DataArts / MRS / GaussDB / DCS / CES）挂在根路径；台账服务按路径前缀区分：
  /ecs /evs /vpc /elb /rds /dms /cce /dws /css（与 app.hcs.factory.mock_endpoints 对应）
- AOM 仅在开发 Mock 前缀 /aom 下提供：CUSTOM.HCS_INSPECTOR_TEST 是自定义演示指标，
  其确定性样本不是生产历史，也不表示 HCS 内置 SYS.DCS 到 AOM 的映射
- CCE 集群 K8s API：/k8s/{cluster_id}/api/v1/...（pods / events / namespaces / Pod 代理 /metrics）
- 数据集按 URL 中的 project_id 选择；Token / AK/SK 所属项目与 URL 中的项目不一致时返回 403（租户隔离）；
  未知项目（早期单账号配置）按租户A 处理
"""
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.hcs import signer

from . import k8s
from . import openapi as oa
from .tenants import AKSK, BY_CODE, BY_DOMAIN, PROJECTS, REGION, TENANTS, clusters, has, tenant_for

app = FastAPI(title="Mock HCS 8.5.1")
TOKENS: dict[str, str] = {}                       # Token → scope 项目 ID
DEFAULT_CODES = {401: "APIGW.0301", 403: "APIGW.0302", 404: "APIGW.0101", 429: "APIGW.0308"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def err(status: int, msg: str, code: str | None = None) -> HTTPException:
    return HTTPException(status, {"error_code": code or DEFAULT_CODES.get(status, "APIGW.0000"), "error_msg": msg})


@app.exception_handler(StarletteHTTPException)
async def _hcs_error(_: Request, e: StarletteHTTPException):
    if isinstance(e.detail, dict):
        content: dict[str, Any] = dict(e.detail)
    elif e.status_code == 404:
        content = {"error_code": "APIGW.0101", "error_msg": "The API does not exist or has not been published"}
    else:
        content = {"error_code": DEFAULT_CODES.get(e.status_code, "APIGW.0000"), "error_msg": str(e.detail)}
    content.setdefault("request_id", f"mock-{_now_ms()}")
    return JSONResponse(status_code=e.status_code, content=content)


# ======================= 认证：X-Auth-Token 或 AK/SK 签名 =======================
@dataclass
class Caller:
    tenant: dict
    project_id: str
    method: str                                   # token / aksk


def _secret_of(ak: str) -> str | None:
    t = AKSK.get(ak)
    return t["secret_key"] if t else None


async def auth(request: Request) -> Caller:
    token = request.headers.get("x-auth-token")
    if token:
        if token not in TOKENS:
            raise err(401, "Incorrect IAM authentication information: token is invalid or expired")
        return Caller(tenant_for(TOKENS[token]), TOKENS[token], "token")
    if request.headers.get("authorization"):
        body = await request.body()
        try:
            ak = signer.verify(request.method, request.url.path, request.query_params.multi_items(),
                               dict(request.headers), body, _secret_of)
        except ValueError as e:
            raise err(401, f"Incorrect IAM authentication information: {e}") from e
        t = AKSK[ak]
        pid = request.headers.get("x-project-id") or t["project_id"]
        if pid in PROJECTS and pid != t["project_id"]:
            raise err(403, f"X-Project-Id {pid} does not belong to the domain of the access key")
        return Caller(t, pid, "aksk")
    raise err(401, "Incorrect IAM authentication information: x-auth-token or authorization not found")


def dataset(project_id: str, caller: Annotated[Caller, Depends(auth)]) -> ModuleType:
    """URL 中的项目 → 数据集；凭据所属项目与 URL 项目不一致时拒绝（模拟 HCS 的项目级鉴权）。"""
    if project_id in PROJECTS and caller.project_id in PROJECTS and caller.project_id != project_id:
        raise err(403, f"The token/AK scope project {caller.project_id} does not match the request project {project_id}")
    return tenant_for(project_id)["ds"]


def tenant_of(project_id: str, caller: Annotated[Caller, Depends(auth)]) -> dict:
    dataset(project_id, caller)
    return tenant_for(project_id)


DS = Annotated[ModuleType, Depends(dataset)]
TENANT = Annotated[dict, Depends(tenant_of)]
CALLER = Annotated[Caller, Depends(auth)]


def _offset(rows: list, offset: int, limit: int) -> list:
    return rows[offset:offset + limit]


def _page(rows: list, page: int, limit: int) -> list:
    return rows[(max(page, 1) - 1) * limit: max(page, 1) * limit]


def _marker(rows: list, marker: str | None, limit: int) -> list:
    if marker:
        ids = [r["id"] for r in rows]
        rows = rows[ids.index(marker) + 1:] if marker in ids else []
    return rows[:limit]


# ======================= IAM =======================
def _catalog(t: dict, mock_base: str = "http://mock") -> list[dict]:
    types = {"iam": "identity", "ecs": "compute", "evs": "volumev2", "vpc": "network", "elb": "elb", "rds": "rds",
             "gaussdb": "gaussdb", "dcs": "dcs", "dms": "dms", "mrs": "mrs", "dataarts": "dataarts", "cce": "cce",
             "dws": "dws", "css": "css", "ces": "metric", "aom": "aom"}
    return [{"id": f"svc-{s}", "name": s, "type": types.get(s, s),
             "endpoints": [{"id": f"ep-{s}-{REGION}", "interface": "public", "region": REGION, "region_id": REGION,
                            "url": f"{mock_base.rstrip('/')}/aom" if s == "aom"
                            else f"https://{s}.{REGION}.hcs.example.com"}]}
            for s in dict.fromkeys([*t["services"], "aom"])]


@app.post("/v3/auth/tokens", status_code=201)
async def issue_token(request: Request):
    body = await request.json()
    ident = body.get("auth", {}).get("identity", {})
    user = (ident.get("password") or {}).get("user") or {}
    if "password" not in ident.get("methods", []) or not user.get("password"):
        raise err(401, "The username or password is wrong.", "IAM.0101")
    project_id = ((body["auth"].get("scope") or {}).get("project") or {}).get("id") or ""
    domain = (user.get("domain") or {}).get("name") or ""
    t = PROJECTS.get(project_id) or BY_DOMAIN.get(domain) or tenant_for(project_id)
    project_id = project_id or t["project_id"]
    token = f"mock-token-{len(TOKENS) + 1}"
    TOKENS[token] = project_id
    now = datetime.now(UTC)
    expires = (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    return JSONResponse(status_code=201, headers={"X-Subject-Token": token}, content={"token": {
        "expires_at": expires, "issued_at": now.isoformat().replace("+00:00", "Z"), "methods": ["password"],
        "user": {"name": user.get("name"), "domain": {"name": domain or t["domain_name"], "id": t["domain_id"]}},
        "project": {"id": project_id, "name": t["project_name"], "domain": {"id": t["domain_id"], "name": t["domain_name"]}},
        "catalog": _catalog(t, str(request.base_url))}})


@app.get("/v3/projects")
def iam_projects(caller: CALLER):
    t = caller.tenant
    return {"projects": [{"id": t["project_id"], "name": t["project_name"], "domain_id": t["domain_id"],
                          "enabled": True, "is_domain": False, "parent_id": t["domain_id"], "description": t["name"]}],
            "links": {"self": "/v3/projects", "next": None, "previous": None}}


@app.get("/v3/auth/catalog")
def iam_catalog(request: Request, caller: CALLER):
    """服务目录：可用来确认局点是否部署了云监控（catalog 中是否有 type=metric / name=ces 的服务）。"""
    return {"catalog": _catalog(caller.tenant, str(request.base_url)), "links": {"self": "/v3/auth/catalog"}}


# ======================= 巡检相关服务（根路径） =======================
@app.get("/v2/{project_id}/instances")
def dcs_instances(ds: DS, offset: int = 0, limit: int = 1000):
    rows = oa.dcs_instances(ds)
    return {"instance_num": len(rows), "instances": _offset(rows, offset, limit)}


def _query_tenant(project_id: str, caller: Caller) -> dict:
    """新查询保留旧单账号项目兼容，但不允许跨项目或借未知项目 fallback 读取另一租户。"""
    if caller.project_id != project_id:
        raise err(403, "The query project does not match the authentication scope")
    tenant = tenant_of(project_id, caller)
    if tenant["code"] != caller.tenant["code"]:
        raise err(403, "The query project does not belong to the authenticated tenant")
    return tenant


@app.get("/v2/{project_id}/instances/statistic")
def dcs_statistics(project_id: str, caller: CALLER):
    """8.5.1 原生当前统计：只返回 RUNNING；内存为 MB，used_cpu 为 String 累计秒数，非百分比。"""
    tenant = _query_tenant(project_id, caller)
    return {"statistics": [{"instance_id": row["instance_id"], "used_memory": row["used_memory"],
                            "max_memory": row["max_memory"],
                            "used_cpu": str(oa.stable(row["instance_id"], "mock-cumulative-cpu-seconds", 3600, 86400))}
                           for row in oa.dcs_instances(tenant["ds"]) if row["status"] == "RUNNING"]}


def _require_ces(t: dict) -> None:
    if not has(t, "ces"):
        raise err(404, "The API does not exist or has not been published in the environment (ces)")


def _dcs_memory_history(tenant: dict, metric: dict, body: dict) -> list[dict]:
    """仅 Mock：按请求时间生成可重复的演示内存曲线，不写入生产历史表。"""
    dims = metric.get("dimensions") or []
    if len(dims) != 1 or dims[0].get("name") != "dcs_instance_id":
        return []
    instance = next((i for i in oa.dcs_instances(tenant["ds"]) if i["instance_id"] == dims[0].get("value")), None)
    if not instance or not instance.get("max_memory"):
        return []
    now = _now_ms()
    try:
        start, end = int(body.get("from", now - 600000)), min(int(body.get("to", now)), now)
        step = int(body.get("period", 300)) * 1000
    except (TypeError, ValueError) as exc:
        raise err(400, "Invalid metric time range") from exc
    if step != 300000 or end < start or end - start > 86400000:
        raise err(400, "DCS demo history requires period 300 and at most one day per request")
    field = body.get("filter", "average")
    if field not in ("average", "max"):
        raise err(400, "Unsupported DCS demo aggregation")
    base = instance["used_memory"] * 100 / instance["max_memory"]
    phase = sum(ord(c) for c in instance["instance_id"]) % 360
    points = []
    stamp = ((start + step - 1) // step) * step
    while stamp < end:
        angle = stamp / 3600000 + phase
        value = base + 8 * math.sin(angle) + 3 * math.sin(angle / 24)
        if field == "max":
            value += 2 + abs(math.sin(angle * 2)) * 3
        points.append({"timestamp": stamp, field: round(max(0, min(100, value)), 3)})
        stamp += step
    return points


@app.post("/V1.0/{project_id}/batch-query-metric-data")
async def ces_batch(t: TENANT, request: Request):
    _require_ces(t)
    body = await request.json()
    values = oa.ces_values(t["ds"])
    disks = {(m["dimensions"][0]["value"], m["dimensions"][1]["value"]): m["_value"] for m in oa.ces_disk_metrics(t["ds"])}
    out = []
    for m in body.get("metrics", []):
        dims = {d["name"]: d["value"] for d in m.get("dimensions") or []}
        if m["metric_name"] == "disk_usedPercent":
            v = disks.get((dims.get("instance_id"), dims.get("mount_point")))
        else:
            v = values.get(next(iter(dims.values()), ""), {}).get(m["metric_name"])
        if m.get("namespace") == "SYS.DCS" and m["metric_name"] == "memory_usage":
            points = _dcs_memory_history(t, m, body)
        else:
            points = [] if v is None else [{"average": round(v, 2), "timestamp": _now_ms() - 60000}]
        out.append({**m, "datapoints": points, "unit": "count" if m["metric_name"] == "connected_clients" else "%"})
    return {"metrics": out}


@app.get("/V1.0/{project_id}/metrics")
def ces_metrics(t: TENANT, namespace: str | None = None, metric_name: str | None = None, start: str | None = None,
                limit: int = 1000):
    """查询指标列表（start 为分页标记 meta_data.marker）。"""
    _require_ces(t)
    rows = [{k: v for k, v in m.items() if k != "_value"} for m in oa.ces_disk_metrics(t["ds"])]
    rows += [{"namespace": "SYS.ECS", "metric_name": "cpu_util", "unit": "%",
              "dimensions": [{"name": "instance_id", "value": u["id"]}]} for u in oa.ecs_usage(t["ds"]).values()]
    rows = [r for r in rows if (not namespace or r["namespace"] == namespace)
            and (not metric_name or r["metric_name"] == metric_name)]

    def key(r: dict) -> str:
        return ".".join([r["namespace"], r["metric_name"], *(f"{d['name']}:{d['value']}" for d in r["dimensions"])])

    keys = [key(r) for r in rows]
    begin = keys.index(start) + 1 if start in keys else 0
    page = rows[begin:begin + limit]
    more = begin + limit < len(rows)
    return {"metrics": page, "meta_data": {"count": len(page), "total": len(rows),
                                           "marker": key(page[-1]) if page and more else ""}}


# ======================= 开发 AOM（自定义演示指标，非生产 / 内置 DCS 映射） =======================
AOM_MOCK_NAMESPACE = "CUSTOM.HCS_INSPECTOR_TEST"
AOM_STATISTICS = {"average", "maximum", "minimum", "sum", "sampleCount"}


def _demo_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _aom_catalogue(tenant: dict) -> list[dict]:
    """复用开发 DCS / ECS / GaussDB 数值和 native ID；不映射任何生产 namespace。"""
    ds = tenant["ds"]
    fixture = {ds.rid("dcs", row["name"]): row for row in ds.DCS}
    rows = []

    def append(rid: str, values: dict) -> None:
        dimensions = [{"name": "instance_id", "value": rid}]
        dimension_hash = hashlib.sha256(json.dumps(dimensions, sort_keys=True).encode()).hexdigest()
        for metric, raw in values.items():
            value = _demo_number(raw)
            if value is not None:
                rows.append({"namespace": AOM_MOCK_NAMESPACE, "metric_name": metric, "dimensions": dimensions,
                             "unit": "Count" if metric == "connected_clients" else "Percent",
                             "dimension_value_hash": dimension_hash, "_value": value})

    for instance in oa.dcs_instances(ds):
        if instance["status"] != "RUNNING":
            continue
        rid = instance["instance_id"]
        # 旧 fixture 的 ces 元组仅保存演示数值；这里不查询 CES，也不要求租户启用 CES。
        cpu, connections = fixture[rid]["ces"]
        used, total = _demo_number(instance["used_memory"]), _demo_number(instance["max_memory"])
        values = {"cpu_usage": _demo_number(cpu), "connected_clients": _demo_number(connections),
                  "memory_usage": round(100 * used / total, 3) if used is not None and total else None}
        append(rid, values)
    # 自定义开发目录复用现有 usage fixture，不继承旧 CES Agent 开关，也不触发 CES 接口。
    for usage in oa.ecs_usage(ds).values():
        disks = [_demo_number(value) for value in usage["disks"].values()]
        # 开发实例级 disk_util 与既有 ECS 指标选择一致：取现有各挂载点的最大使用率，不填造零值。
        disk = max((value for value in disks if value is not None), default=None)
        append(usage["id"], {"cpu_util": usage["cpu"], "mem_util": usage["mem"], "disk_util": disk})
    for name, *_fields, metrics, _created in ds.GAUSSDB:
        cpu, memory, disk = metrics
        append(oa.gaussdb_id(ds, name), {"cpu_usage": cpu, "mem_usage": memory, "disk_usage": disk})
    return rows


async def _aom_body(request: Request, fields: set[str]) -> dict:
    if len(await request.body()) > 12 * 1024 * 1024:
        raise err(413, "AOM mock query body exceeds 12 MB")
    try:
        body = await request.json()
    except ValueError as exc:
        raise err(400, "Invalid AOM query JSON") from exc
    if not isinstance(body, dict) or set(body) != fields:
        raise err(400, "Invalid AOM query fields for this API version")
    return body


def _aom_selectors(body: dict, key: str, *, camel: bool = False, samples: bool = False) -> list[dict]:
    items = body[key]
    maximum = 20 if samples else 100  # 目录的 100 项上限也作为本开发 Mock 的 v1 查询预算。
    if not isinstance(items, list) or not 1 <= len(items) <= maximum:
        raise err(400, "Invalid AOM query item count")
    metric_field = "metricName" if camel else "metric_name"
    queries = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"namespace", metric_field, "dimensions"}:
            raise err(400, "Invalid AOM metric descriptor for this API version")
        namespace, name = item.get("namespace"), item.get(metric_field)
        if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 255:
            raise err(400, "AOM namespace is required")
        if ((samples or metric_field in item)
                and (not isinstance(name, str) or not name or len(name) > (1000 if camel else 255))):
            raise err(400, "Invalid AOM metric name")
        dimensions = item.get("dimensions", [])
        if not isinstance(dimensions, list) or len(dimensions) > 32 or samples and not dimensions:
            raise err(400, "Invalid AOM metric dimensions")
        names = set()
        for dimension in dimensions:
            if (not isinstance(dimension, dict) or set(dimension) - {"name", "value"}
                    or (camel or samples) and set(dimension) != {"name", "value"}):
                raise err(400, "Invalid AOM dimension fields")
            if "name" in dimension:
                dim_name = dimension["name"]
                if (not isinstance(dim_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", dim_name)
                        or dim_name in names):
                    raise err(400, "Invalid or duplicate AOM dimension name")
                names.add(dim_name)
            if "value" in dimension and (not isinstance(dimension["value"], str) or not dimension["value"]
                                         or len(dimension["value"]) > 1024):
                raise err(400, "Invalid AOM dimension value")
        queries.append({"namespace": namespace, "metric_name": name, "dimensions": dimensions})
    return queries


def _aom_matches(row: dict, query: dict) -> bool:
    return (row["namespace"] == query["namespace"] and
            (query["metric_name"] is None or row["metric_name"] == query["metric_name"]) and
            all(any(all(actual.get(key) == value for key, value in requested.items()) for actual in row["dimensions"])
                for requested in query["dimensions"]))


def _aom_rows(tenant: dict, selectors: list[dict]) -> list[dict]:
    # 开发 Mock 对多个显式选择器取精确匹配的并集并去重；不是对生产模糊 / AND / OR 语义的承诺。
    return [row for row in _aom_catalogue(tenant) if any(_aom_matches(row, query) for query in selectors)]


def _aom_descriptor(row: dict, *, camel: bool = False) -> dict:
    return {"namespace": row["namespace"], "metricName" if camel else "metric_name": row["metric_name"],
            "dimensions": row["dimensions"], "unit": row["unit"],
            "dimensionvaluehash" if camel else "dimension_value_hash": row["dimension_value_hash"]}


@app.post("/aom/v1/{project_id}/ams/metrics")
async def aom_metrics(project_id: str, caller: CALLER, request: Request):
    tenant = _query_tenant(project_id, caller)
    if set(request.query_params) - {"limit", "start"}:
        raise err(400, "AOM mock supports regular metricItems queries with limit/start")
    try:
        limit, start = int(request.query_params.get("limit", "1000")), int(request.query_params.get("start", "0"))
    except ValueError as exc:
        raise err(400, "Invalid AOM catalogue pagination") from exc
    if not 1 <= limit <= 1000 or start < 0:
        raise err(400, "Invalid AOM catalogue pagination")
    body = await _aom_body(request, {"metricItems"})
    rows = _aom_rows(tenant, _aom_selectors(body, "metricItems", camel=True))
    page = rows[start:start + limit]
    return {"errorCode": "SVCSTG_AMS_2000000", "errorMessage": "success",
            "metrics": [_aom_descriptor(row, camel=True) for row in page],
            "metaData": {"count": len(page), "total": len(rows), "start": start + limit if start + limit < len(rows) else None}}


@app.post("/aom/v2/{project_id}/series")
async def aom_series(project_id: str, caller: CALLER, request: Request):
    tenant = _query_tenant(project_id, caller)
    if request.query_params:
        raise err(400, "No v2 series request pagination is defined by this mock contract")
    body = await _aom_body(request, {"series"})
    rows = _aom_rows(tenant, _aom_selectors(body, "series"))
    return {"error_code": "SVCSTG_AMS_2000000", "error_msg": "success",
            "series": [_aom_descriptor(row) for row in rows],
            "meta_data": {"count": len(rows), "total": len(rows), "offset": None}}


def _aom_window(body: dict) -> tuple[int, int, int, list[str]]:
    period, statistics, time_range = body["period"], body["statistics"], body["time_range"]
    if type(period) is not int or period not in (60, 300, 900, 3600):
        raise err(400, "Invalid AOM period in seconds")
    if (not isinstance(statistics, list) or not 1 <= len(statistics) <= len(AOM_STATISTICS)
            or any(not isinstance(item, str) or item not in AOM_STATISTICS for item in statistics)):
        raise err(400, "Invalid AOM statistics")
    if not isinstance(time_range, str) or len(time_range) > 64:
        raise err(400, "Invalid AOM time_range")
    try:
        start, end, duration = map(int, time_range.split("."))
    except ValueError as exc:
        raise err(400, "Invalid AOM time_range") from exc
    if start < -1 or end < -1 or duration <= 0 or duration * 60 > period * 1440:
        raise err(400, "AOM time range exceeds the supported point budget")
    if start == -1 and end == -1:
        end = _now_ms()
        start = end - duration * 60000
    elif start == -1:
        start = end - duration * 60000
    elif end == -1:
        end = min(start + duration * 60000, _now_ms())
    if start < 0 or end <= start or duration * 60000 < end - start - 60000:
        raise err(400, "Invalid AOM time range bounds or duration")
    step = period * 1000
    first = (start + step - 1) // step * step
    if max(0, (end - first + step - 1) // step) > 1440:
        raise err(400, "AOM mock response exceeds 1440 points per series")
    return first, end, step, list(dict.fromkeys(statistics))


@app.post("/aom/v2/{project_id}/samples")
async def aom_samples(project_id: str, caller: CALLER, request: Request):
    tenant = _query_tenant(project_id, caller)
    if (set(request.query_params) - {"fill_value"}
            or request.query_params.get("fill_value", "-1") not in ("-1", "0", "null", "average")):
        raise err(400, "Invalid AOM fill_value")
    body = await _aom_body(request, {"samples", "period", "statistics", "time_range"})
    selectors = _aom_selectors(body, "samples", samples=True)
    first, end, step, statistics = _aom_window(body)
    rows = []
    for row in _aom_rows(tenant, selectors):
        # 每桶一个确定性的开发样本；不把插值或其他租户 / 未发现指标填为真实零值。
        points = [{"timestamp": stamp, "unit": row["unit"], "statistics": [
            {"statistic": name, "value": 1.0 if name == "sampleCount" else row["_value"]} for name in statistics]}
                  for stamp in range(first, end, step)]
        rows.append({"sample": {key: row[key] for key in ("namespace", "metric_name", "dimensions")}, "data_points": points})
    return {"error_code": "SVCSTG_AMS_2000000", "error_msg": "success", "samples": rows}


@app.get("/v1/{project_id}/jobs")
def dataarts_jobs(ds: DS, jobType: str = "BATCH", offset: int = 0, limit: int = 100):  # noqa: N803
    jobs = [{"name": n, "jobType": jobType, "owner": "bigdata", "priority": 0, "path": f"/实时/{n}"}
            for n in getattr(ds, "RT_JOBS", {})] if jobType == "REAL_TIME" else []
    return {"total": len(jobs), "jobs": _offset(jobs, offset * limit, limit)}


@app.get("/v1/{project_id}/jobs/instances/detail")
def dataarts_instances(ds: DS, minPlanTime: int = 0, maxPlanTime: int | None = None,  # noqa: N803
                       offset: int = 0, limit: int = 100):
    now = _now_ms()
    rows = []
    for idx, (name, status, plan_ago, run_min) in enumerate(getattr(ds, "BATCH_JOBS", [])):
        plan = now - plan_ago * 60_000
        if plan < minPlanTime or plan > (maxPlanTime or now):
            continue
        start = plan + 30_000
        rows.append({"jobName": name, "instanceId": 880_000 + idx, "status": status, "planTime": plan,
                     "submitTime": plan, "startTime": start,
                     "endTime": None if run_min is None else start + run_min * 60_000,
                     "executeTime": None if run_min is None else run_min * 60_000, "instanceType": 0})
    return {"total": len(rows), "instances": _offset(rows, offset * limit, limit)}


@app.get("/v1/{project_id}/jobs/{name:path}/status")
def dataarts_status(ds: DS, name: str):
    jobs = getattr(ds, "RT_JOBS", {})
    if name not in jobs:
        raise err(404, f"job {name} not found", "DLF.0100")
    st = jobs[name]
    nodes = [{"name": f"{name}_src", "status": st},
             {"name": f"{name}_agg", "status": "EXCEPTION" if name == "rt_user_behavior" else st}]
    return {"name": name, "status": st, "nodes": nodes, "startTime": _now_ms() - 3600_000}


@app.get("/gaussdb/v3.3/{project_id}/instances")
@app.get("/gaussdb/v3.2/{project_id}/instances")
@app.get("/gaussdb/v3.1/{project_id}/instances")
@app.get("/opengauss/v3/{project_id}/instances")
def gaussdb_instances(ds: DS, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    rows = oa.gaussdb_instances(ds)
    return {"instances": _offset(rows, offset, limit), "total_count": len(rows)}


def _gauss_native_instance(project_id: str, instance_id: str, caller: Caller) -> tuple[ModuleType, dict]:
    """仅开发 Mock：确认项目和实例归属，不表示正式环境具备这些诊断能力。"""
    tenant = _query_tenant(project_id, caller)
    ds = tenant["ds"]
    instance = next((row for row in oa.gaussdb_instances(ds) if row["id"] == instance_id), None)
    if instance is None:
        raise err(404, "GaussDB instance does not belong to the request project")
    return ds, instance


@app.get("/gaussdb/v3/{project_id}/instances/{instance_id}/volume-usage")
def gaussdb_volume_usage(project_id: str, instance_id: str, caller: CALLER):
    """上传25.1.30 §4.2.1：used/total为String、GB；数值仅由开发数据集合成。"""
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    row = next(row for row in ds.GAUSSDB if oa.gaussdb_id(ds, row[0]) == instance_id)
    total = float(instance["volume"]["size"])
    return {"used": str(round(total * row[5][2] / 100, 2)), "total": str(total)}


def _gauss_native_nodes(ds: ModuleType, instance: dict) -> list[dict]:
    """开发数据集的确定性节点/组件关系；不伪称真实实例已开启诊断。"""
    zones = instance["availability_zone"].split(",")
    return [{"id": ds.rid("gaussdb-native-node", f"{instance['id']}/{node['name']}"),
             "name": node["name"], "availability_zone_id": zones[index % len(zones)],
             "description": "Development Mock node", "status": node["status"],
             "components": [{"id": f"dn_{6001 + index}", "type": "DN", "role": node["role"],
                             "status": "Primary" if node["role"] == "master" else "Standby",
                             "distributed_id": ds.rid("gaussdb-native-shard", instance["id"])}]}
            for index, node in enumerate(instance["nodes"])]


async def _gauss_native_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise err(400, "GaussDB query body must be a JSON object") from exc
    if not isinstance(body, dict):
        raise err(400, "GaussDB query body must be a JSON object")
    return body


def _gauss_native_ms(value: Any) -> int:
    if type(value) is not int or not 10**12 <= value < 10**13:
        raise err(400, "GaussDB query time must be a 13-digit UTC millisecond integer")
    return value


def _gauss_native_slow_query(ds: ModuleType, instance: dict, body: dict, *, detail: bool) -> list[dict]:
    if body.get("instance_id") != instance["id"]:
        raise err(400, "Body instance_id does not match the request instance")
    start, end = _gauss_native_ms(body.get("start_time")), _gauss_native_ms(body.get("end_time"))
    if end <= start:
        raise err(400, "GaussDB query end_time must be after start_time")
    nodes = _gauss_native_nodes(ds, instance)
    selected = body.get("node_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(nid, str) for nid in selected):
        raise err(400, "node_ids must be a nonempty string array")
    if not set(selected) <= {node["id"] for node in nodes}:
        raise err(404, "GaussDB query node does not belong to the instance")
    threshold = body.get("threshold", 0 if detail else None)
    if type(threshold) is not int or not (0 if detail and "threshold" not in body else 1) <= threshold <= 99:
        raise err(400, "Slow SQL threshold must be an integer from 1 to 99 seconds")
    sql_id = body.get("sql_id")
    if (detail and not sql_id) or (sql_id is not None and not isinstance(sql_id, str)):
        raise err(400, "sql_id must be a string and is required for slow SQL details")
    rows = []
    for index, node in enumerate(nodes):
        duration = 1_500_000 + index * 500_000
        stamp = _now_ms() - 120_000 - index * 1000
        identity = str(int(hashlib.sha256(node["id"].encode()).hexdigest()[:12], 16))
        if (node["id"] not in selected or not start <= stamp <= end or duration < threshold * 1_000_000
                or sql_id is not None and sql_id != identity):
            continue
        row = {"sql_id": identity, "node_id": node["id"], "node_name": node["name"],
               "user_name": "mock_readonly", "sql_text": "SELECT 1 /* development mock; never executed */",
               "query_plan": "Result (development mock)", "buffer_hit_ratio": "99.5", "sql_hit_ratio": "100"}
        if detail:
            row.update(client_ip="192.0.2.10", client_port="5432", returned_rows=1, fetched_rows=1,
                       start_time=datetime.fromtimestamp(stamp / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S +0000"),
                       finish_time=datetime.fromtimestamp((stamp + duration / 1000) / 1000, UTC).strftime(
                           "%Y-%m-%d %H:%M:%S +0000"),
                       total_time=str(duration), cpu_time=str(duration // 2), io_time=str(duration // 4),
                       plan_time="1000", fetched_pages=1, hit_pages=1, lock_count=0, lock_time="0")
        else:
            row.update(calls=2, avg_exec_time=str(duration), avg_cpu_time=str(duration // 2),
                       avg_io_time=str(duration // 4), avg_returned_rows=1, avg_fetched_rows=1)
        rows.append(row)
    return rows


@app.get("/gaussdb/v3/{project_id}/instances/{instance_id}/components")
def gaussdb_components(project_id: str, instance_id: str, caller: CALLER,
                       offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                       component_type: str = Query("ALL", pattern="^(ALL|CN|DN|CM|GTM|ETCD)$"),
                       availability_zone_id: str = "ALL"):
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    nodes = _gauss_native_nodes(ds, instance)
    nodes = [node for node in nodes if component_type == "ALL" or
             any(component["type"] == component_type for component in node["components"])]
    if availability_zone_id != "ALL":
        # 合成DN属于一个分片：按主组件AZ筛选时同时保留该分片的所有副本。
        master = next((node for node in nodes if any(c["role"] == "master" for c in node["components"])), None)
        if master is None or master["availability_zone_id"] != availability_zone_id:
            nodes = []
    return {"nodes": _offset(nodes, offset, limit), "total_count": len(nodes)}


@app.post("/gaussdb/v3/{project_id}/instances/{instance_id}/slow-sql-execute-node")
async def gaussdb_slow_nodes(project_id: str, instance_id: str, caller: CALLER, request: Request):
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    body = await _gauss_native_body(request)
    if body.get("action") != "slow":
        raise err(400, "Only the read-only slow node query is supported")
    return {"nodes": [{"node_id": node["id"], "node_name": node["name"], "instance_id": instance_id,
                       "role": component["role"], "component_type": component["type"]}
                      for node in _gauss_native_nodes(ds, instance) for component in node["components"]
                      if component["type"] in ("CN", "DN")]}


@app.post("/gaussdb/v3/{project_id}/instances/{instance_id}/slow-sql-list")
async def gaussdb_slow_sql_list(project_id: str, instance_id: str, caller: CALLER, request: Request):
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    rows = _gauss_native_slow_query(ds, instance, await _gauss_native_body(request), detail=False)
    return {"slow_sql_infos": rows, "total": len(rows)}


@app.post("/gaussdb/v3/{project_id}/instances/{instance_id}/slow-sql-detail")
async def gaussdb_slow_sql_detail(project_id: str, instance_id: str, caller: CALLER, request: Request):
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    rows = _gauss_native_slow_query(ds, instance, await _gauss_native_body(request), detail=True)
    return {"slow_sql_details": rows, "total": len(rows)}


@app.post("/gaussdb/v3/{project_id}/instances/{instance_id}/real-time-session")
async def gaussdb_real_time_session(project_id: str, instance_id: str, caller: CALLER, request: Request):
    ds, instance = _gauss_native_instance(project_id, instance_id, caller)
    body = await _gauss_native_body(request)
    if body.get("is_statistic_open") != "false":
        raise err(400, "Development Mock only supports explicit read-only is_statistic_open=false")
    node = next((node for node in _gauss_native_nodes(ds, instance) if node["id"] == body.get("node_id")), None)
    if node is None or not any(c["id"] == body.get("component_id") and c["type"] in ("CN", "DN")
                               for c in node["components"]):
        raise err(404, "GaussDB session node/component does not belong to the instance")
    limit = body.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise err(400, "Session limit must be an integer from 1 to 200")
    filters = body.get("query_info", {})
    allowed = {"database_name", "client_ip", "user_name", "wait", "session_id", "state", "wait_event",
               "exec_time", "query_runtime", "unique_sql_id"}
    if not isinstance(filters, dict) or not set(filters) <= allowed or any(not isinstance(v, str) for v in filters.values()):
        raise err(400, "Invalid session query_info")
    if "wait" in filters and filters["wait"] not in ("t", "f"):
        raise err(400, "Session wait filter must be t or f")
    rows = [{"session_id": str(700 + index), "pid": str(9000 + index), "unique_sql_id": str(8000 + index),
             "database_name": "postgres", "client_ip": f"192.0.2.{10 + index}", "user_name": "mock_readonly",
             "wait": "f" if index == 0 else "t", "block_session": "" if index == 0 else "700",
             "wait_event": "none" if index == 0 else "Lock", "state": "active",
             "query_runtime": "00:00:02", "query": "SELECT 1 /* development mock; never executed */",
             "back_end_start": _now_ms() - 120_000, "transaction_start": _now_ms() - 5000,
             "query_start": _now_ms() - 2000, "application_name": "hcs-inspector-development-mock", "exec_time": "120"}
            for index in range(2)]
    # 不生成统计开关默认开启时才产生的trans_num/rollback_num/sql_num，不执行SQL或修改数据。
    rows = [row for row in rows if all(row.get(key) == value for key, value in filters.items())]
    return {"sessions": rows[:limit]}


def _gauss_native_inspection_records(ds: ModuleType) -> list[dict]:
    """无存储副作用的已完成开发巡检；时间及记录标识按UTC日期固定。"""
    start = (_now_ms() // 86_400_000) * 86_400_000 - 3_600_000
    rows = []
    for instance in oa.gaussdb_instances(ds):
        source = next(row for row in ds.GAUSSDB if oa.gaussdb_id(ds, row[0]) == instance["id"])
        abnormal = sum(value >= threshold for value, threshold in zip(source[5], (70, 80, 85), strict=True))
        rows.append({"record_id": ds.rid("gaussdb-native-inspection", f"{instance['id']}/{start}"),
                     "instance_id": instance["id"], "instance_name": instance["name"],
                     "inspection_policy": "periodic", "inspection_scope": "daily", "task_name": "Development Mock",
                     "start_time": start, "end_time": start + 60_000, "status": "completed", "progress": "100",
                     "total_count": 3, "normal_count": 3 - abnormal, "abnormal_count": abnormal, "failed_count": 0})
    return rows


@app.post("/gaussdb/v3/{project_id}/inspections/records")
async def gaussdb_inspection_records(project_id: str, caller: CALLER, request: Request):
    ds = _query_tenant(project_id, caller)["ds"]
    body = await _gauss_native_body(request)
    limit, offset = body.get("limit"), body.get("offset")
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise err(400, "Inspection records require limit 1..100 and nonnegative offset")
    rows = _gauss_native_inspection_records(ds)
    for key in ("instance_name", "status", "task_name"):
        if key in body:
            if not isinstance(body[key], str):
                raise err(400, "Invalid inspection query filter")
            rows = [row for row in rows if row[key] == body[key]]
    bounds = body.get("inspection_start_time_range")
    if bounds is not None:
        if not isinstance(bounds, dict):
            raise err(400, "Invalid inspection time range")
        start = _gauss_native_ms(bounds["start_time"]) if "start_time" in bounds else None
        end = _gauss_native_ms(bounds["end_time"]) if "end_time" in bounds else None
        if start is not None and end is not None and end < start:
            raise err(400, "Invalid inspection time range")
        rows = [row for row in rows if (start is None or row["start_time"] >= start)
                and (end is None or row["start_time"] <= end)]
    sort, order = body.get("sort", "start_time"), body.get("order", "desc")
    if sort not in ("instance_name", "task_name", "start_time", "end_time") or order not in ("asc", "desc"):
        raise err(400, "Invalid inspection sort")
    rows.sort(key=lambda row: row[sort], reverse=order == "desc")
    return {"records": _offset(rows, offset, limit), "total_count": len(rows)}


@app.get("/gaussdb/v3/{project_id}/inspections/records/{record_id}")
def gaussdb_inspection_detail(project_id: str, record_id: str, caller: CALLER):
    ds = _query_tenant(project_id, caller)["ds"]
    record = next((row for row in _gauss_native_inspection_records(ds) if row["record_id"] == record_id), None)
    if record is None:
        raise err(404, "GaussDB inspection record does not belong to the request project")
    _, instance = _gauss_native_instance(project_id, record["instance_id"], caller)
    source = next(row for row in ds.GAUSSDB if oa.gaussdb_id(ds, row[0]) == instance["id"])
    checks = []
    for (label, item, threshold), value in zip((("CPU使用率", "os_cpu_usage", 70),
                  ("内存使用率", "mock_memory_usage", 80), ("磁盘使用率", "mock_disk_usage", 85)), source[5], strict=True):
        points = [round(max(0, value - 1), 2), float(value), round(min(100, value + 0.5), 2)]
        checks.append({"name": label, "item": item, "description": "开发Mock合成巡检项，不代表生产采样",
                       "inspection_category": "系统资源", "status": "abnormal" if value >= threshold else "normal",
                       "inspection_result": "Development Mock result", "possible_causes": "Development Mock",
                       "suggestions": "仅供开发联调", "indicators": [{"name": label, "unit": "%",
                           "node": None, "remarks": "开发测试数据", "metrics": [{"ip": instance["private_ips"][0],
                               "name": instance["nodes"][0]["name"], "max": max(points), "min": min(points),
                               "average": round(sum(points) / len(points), 2), "p95": max(points),
                               "data": {"time": [record["start_time"] - 120_000, record["start_time"] - 60_000,
                                                 record["start_time"]], "metrics": points}}]}]})
    return {"instance_id": record["instance_id"], "start_time": record["start_time"], "end_time": record["end_time"],
            "inspection_results": checks, "total_count": len(checks)}


@app.get("/v1.1/{project_id}/cluster_infos")
def mrs_clusters(ds: DS, pageSize: int = 10, currentPage: int = 1):  # noqa: N803
    rows = oa.mrs_clusters(ds)
    return {"clusterTotal": len(rows), "clusters": _page(rows, currentPage, pageSize)}


@app.get("/v2/{project_id}/clusters/{cluster_id}/job-executions", status_code=202)
def mrs_jobs(ds: DS, cluster_id: str, offset: int = 1, limit: int = 100):
    now = _now_ms()
    name = next((c["clusterName"] for c in oa.mrs_clusters(ds) if c["clusterId"] == cluster_id), None)
    jobs = getattr(ds, "MRS_JOBS", {}).get(name, []) if name else []
    rows = [{"job_id": ds.rid("mrs-job", f"{name}/{j}"), "job_name": j, "job_state": st, "job_result": res,
             "job_type": jt, "queue": "default", "submitted_time": now - ago * 60_000, "elapsed_time": el}
            for j, jt, st, res, ago, el in jobs]
    return JSONResponse(status_code=202, content={"total_record": len(rows), "job_list": rows})


# ======================= 台账相关服务（按路径前缀区分） =======================
def service(prefix: str) -> APIRouter:
    return APIRouter(prefix=prefix)


ecs, evs, vpc, elb, rds = service("/ecs"), service("/evs"), service("/vpc"), service("/elb"), service("/rds")
dms, cce, dws, css = service("/dms"), service("/cce"), service("/dws"), service("/css")


@ecs.get("/v1/{project_id}/cloudservers/detail")
def ecs_servers(ds: DS, offset: int = 1, limit: int = 25):    # offset 为页码，从 1 开始
    rows = oa.ecs_servers(ds)
    return {"count": len(rows), "servers": _page(rows, offset, limit)}


@ecs.get("/v1/{project_id}/cloudservers/limits")
def ecs_limits(ds: DS):
    rows = oa.ecs_servers(ds)
    return {"absolute": {"maxTotalInstances": 500, "totalInstancesUsed": len(rows),
        "maxTotalCores": 4096, "totalCoresUsed": sum(int((r.get("flavor") or {}).get("vcpus") or 0) for r in rows),
        "maxTotalRAMSize": 8388608, "totalRAMUsed": sum(int((r.get("flavor") or {}).get("ram") or 0) for r in rows),
        "maxTotalKeypairs": -1, "maxServerGroups": 20, "maxServerGroupMembers": 32, "totalServerGroupsUsed": 0}}


@ecs.get("/v1/{project_id}/cloudservers/{server_id}")
def ecs_server(ds: DS, server_id: str):
    row = next((item for item in oa.ecs_servers(ds) if item["id"] == server_id), None)
    if row is None:
        raise HTTPException(404, "云服务器不存在")
    return {"server": row}


@evs.get("/v2/{project_id}/cloudvolumes/detail")
def evs_volumes(ds: DS, offset: int = 0, limit: int = 1000):
    rows = oa.evs_volumes(ds)
    return {"count": len(rows), "volumes": _offset(rows, offset, limit)}


def _marker_api(key: str, source):
    def handler(ds: DS, limit: int = 2000, marker: str | None = None):
        return {key: _marker(source(ds), marker, limit)}
    return handler


vpc.add_api_route("/v1/{project_id}/vpcs", _marker_api("vpcs", oa.vpcs), methods=["GET"])
vpc.add_api_route("/v1/{project_id}/subnets", _marker_api("subnets", oa.subnets), methods=["GET"])
vpc.add_api_route("/v1/{project_id}/publicips", _marker_api("publicips", oa.publicips), methods=["GET"])
elb.add_api_route("/v3/{project_id}/elb/loadbalancers", _marker_api("loadbalancers", oa.loadbalancers), methods=["GET"])


@rds.get("/v3/{project_id}/instances")
def rds_instances(ds: DS, offset: int = 0, limit: int = 100):
    rows = oa.rds_instances(ds)
    return {"instances": _offset(rows, offset, limit), "total_count": len(rows)}


@dms.get("/v2/{project_id}/instances")
def dms_instances(ds: DS, engine: str = Query(pattern="^kafka$"), offset: int = Query(0, ge=0),
                  limit: int = Query(10, ge=1, le=50)):
    rows = _offset(oa.kafka_instances(ds), offset, limit)
    # 文档未承诺 instance_num 是全量总数；模拟页内数量，防止客户端靠它提前停页。
    return {"instances": rows, "instance_num": len(rows)}


@cce.get("/api/v3/projects/{project_id}/clusters")
def cce_clusters(ds: DS):
    return {"kind": "Cluster", "apiVersion": "v3", "items": oa.cce_clusters(ds)}


@cce.get("/api/v3/projects/{project_id}/clusters/{cluster_id}/nodes")
def cce_nodes(ds: DS, cluster_id: str):
    return {"kind": "List", "apiVersion": "v3", "items": oa.cce_nodes(ds, cluster_id)}


@dws.get("/v1.0/{project_id}/clusters")
def dws_clusters(ds: DS):
    rows = oa.dws_clusters(ds)
    return {"clusters": rows, "count": len(rows)}


@css.get("/v1.0/{project_id}/clusters")
def css_clusters(ds: DS, start: int = 1, limit: int = 10):
    rows = oa.css_clusters(ds)
    return {"clusters": _page(rows, start, limit), "totalSize": len(rows)}


for r in (ecs, evs, vpc, elb, rds, dms, cce, dws, css):
    app.include_router(r)


# ======================= CCE 集群 K8s API（经 CCE API 网关，只读） =======================
kube = APIRouter(prefix="/k8s/{cluster_id}")


def cluster_of(cluster_id: str, caller: CALLER) -> tuple[ModuleType, str]:
    hit = clusters().get(cluster_id)
    if hit is None:
        raise HTTPException(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 404,
                                  "reason": "NotFound", "message": f"cluster {cluster_id} not found"})
    t, name = hit
    if caller.project_id in PROJECTS and caller.project_id != t["project_id"]:
        raise HTTPException(403, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 403,
                                  "reason": "Forbidden", "message": "cluster does not belong to the caller's project"})
    return t["ds"], name


CLUSTER = Annotated[tuple[ModuleType, str], Depends(cluster_of)]


def _k8s_list(kind: str, items: list[dict], limit: int | None, cont: str | None) -> dict:
    start = int(cont) if cont and cont.isdigit() else 0
    end = start + limit if limit else len(items)
    meta: dict[str, Any] = {"resourceVersion": str(9_000_000 + len(items))}
    if end < len(items):
        meta["continue"], meta["remainingItemCount"] = str(end), len(items) - end
    return {"kind": kind, "apiVersion": "v1", "metadata": meta, "items": items[start:end]}


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@kube.get("/api/v1/namespaces")
def k8s_namespaces(cl: CLUSTER, limit: int | None = None, cont: Annotated[str | None, "continue"] = None):
    return _k8s_list("NamespaceList", k8s.namespaces(*cl), limit, cont)


def _pods(cl: tuple[ModuleType, str], ns: str | None) -> list[dict]:
    return [p for p in k8s.pods(cl[0], cl[1], _now()) if ns is None or p["metadata"]["namespace"] == ns]


def _events(cl: tuple[ModuleType, str], ns: str | None) -> list[dict]:
    return [e for e in k8s.events(cl[0], cl[1], _now()) if ns is None or e["metadata"].get("namespace") == ns]


@kube.get("/api/v1/pods")
def k8s_pods(cl: CLUSTER, request: Request, limit: int | None = None):
    return _k8s_list("PodList", _pods(cl, None), limit, request.query_params.get("continue"))


@kube.get("/api/v1/events")
def k8s_events(cl: CLUSTER, request: Request, limit: int | None = None):
    return _k8s_list("EventList", _events(cl, None), limit, request.query_params.get("continue"))


@kube.get("/api/v1/namespaces/{ns}/pods")
def k8s_ns_pods(cl: CLUSTER, ns: str, request: Request, limit: int | None = None):
    return _k8s_list("PodList", _pods(cl, ns), limit, request.query_params.get("continue"))


@kube.get("/api/v1/namespaces/{ns}/events")
def k8s_ns_events(cl: CLUSTER, ns: str, request: Request, limit: int | None = None):
    return _k8s_list("EventList", _events(cl, ns), limit, request.query_params.get("continue"))


@kube.get("/api/v1/namespaces/{ns}/pods/{pod_port}/proxy/metrics")
def k8s_pod_metrics(cl: CLUSTER, ns: str, pod_port: str):
    pod = pod_port.split(":", 1)[0]
    text = k8s.metrics_text(cl[0], cl[1], ns, pod)
    if text is None:
        raise HTTPException(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 404,
                                  "reason": "NotFound", "message": f"pod {ns}/{pod} does not expose metrics"})
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


app.include_router(kube)


# ======================= 租户侧 Prometheus（node_exporter，仅租户B） =======================
PROM_KEYS = (("node_cpu", "cpu_util"), ("MemAvailable", "mem_util"), ("node_filesystem", "disk_util"),
             ("node_boot_time", "boot_time"))


@app.get("/prom/{code}/api/v1/query")
def prom_query(code: str, query: str = ""):
    t = BY_CODE.get(code)
    if t is None or not t.get("prometheus_url"):
        return JSONResponse(status_code=404, content={"status": "error", "errorType": "not_found",
                                                      "error": f"no prometheus for {code}"})
    key = next((k for frag, k in PROM_KEYS if frag in query), "up" if query.strip() == "up" else "")
    result = oa.prometheus_series(t["ds"], key) if key else []
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


__all__ = ["app", "TOKENS", "TENANTS"]
