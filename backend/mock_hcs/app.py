"""Mock HCS 8.5.1：模拟 IAM、各云服务的只读接口、CCE 集群 K8s API 与租户侧 Prometheus（字段格式参照 HCS / 华为云
OpenAPI 与 kube-apiserver v1），用于本地联调、演示与自动化测试。

启动：uvicorn mock_hcs.app:app --port 9000
- 两个演示租户（tenants.py）：租户A 用户名密码换 Token（X-Auth-Token）；租户B AK/SK 签名（SDK-HMAC-SHA256），
  且局点没有云监控 CES（CES 接口返回 404 APIGW.0101），ECS 使用率由 /prom/tenant-b 模拟的 Prometheus 提供
- 巡检用到的服务（DataArts / MRS / GaussDB / DCS / CES）挂在根路径；台账服务按路径前缀区分：
  /ecs /evs /vpc /elb /rds /dms /cce /dws /css（与 app.hcs.factory.mock_endpoints 对应）
- CCE 集群 K8s API：/k8s/{cluster_id}/api/v1/...（pods / events / namespaces / Pod 代理 /metrics）
- 数据集按 URL 中的 project_id 选择；Token / AK/SK 所属项目与 URL 中的项目不一致时返回 403（租户隔离）；
  未知项目（早期单账号配置）按租户A 处理
"""
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
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
def _catalog(t: dict) -> list[dict]:
    types = {"iam": "identity", "ecs": "compute", "evs": "volumev2", "vpc": "network", "elb": "elb", "rds": "rds",
             "gaussdb": "gaussdb", "dcs": "dcs", "dms": "dms", "mrs": "mrs", "dataarts": "dataarts", "cce": "cce",
             "dws": "dws", "css": "css", "ces": "metric"}
    return [{"id": f"svc-{s}", "name": s, "type": types.get(s, s),
             "endpoints": [{"id": f"ep-{s}-{REGION}", "interface": "public", "region": REGION, "region_id": REGION,
                            "url": f"https://{s}.{REGION}.hcs.example.com"}]} for s in t["services"]]


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
        "catalog": _catalog(t)}})


@app.get("/v3/projects")
def iam_projects(caller: CALLER):
    t = caller.tenant
    return {"projects": [{"id": t["project_id"], "name": t["project_name"], "domain_id": t["domain_id"],
                          "enabled": True, "is_domain": False, "parent_id": t["domain_id"], "description": t["name"]}],
            "links": {"self": "/v3/projects", "next": None, "previous": None}}


@app.get("/v3/auth/catalog")
def iam_catalog(caller: CALLER):
    """服务目录：可用来确认局点是否部署了云监控（catalog 中是否有 type=metric / name=ces 的服务）。"""
    return {"catalog": _catalog(caller.tenant), "links": {"self": "/v3/auth/catalog"}}


# ======================= 巡检相关服务（根路径） =======================
@app.get("/v2/{project_id}/instances")
def dcs_instances(ds: DS, offset: int = 0, limit: int = 1000):
    rows = oa.dcs_instances(ds)
    return {"instance_num": len(rows), "instances": _offset(rows, offset, limit)}


def _require_ces(t: dict) -> None:
    if not has(t, "ces"):
        raise err(404, "The API does not exist or has not been published in the environment (ces)")


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


@app.get("/v1/{project_id}/jobs")
def dataarts_jobs(ds: DS, jobType: str = "REAL_TIME", offset: int = 0, limit: int = 100):  # noqa: N803
    jobs = [{"name": n, "jobType": jobType, "owner": "bigdata", "priority": 0, "path": f"/实时/{n}"}
            for n in getattr(ds, "RT_JOBS", {})] if jobType == "REAL_TIME" else []
    return {"total": len(jobs), "jobs": _offset(jobs, offset, limit)}


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
    return {"total": len(rows), "instances": _offset(rows, offset, limit)}


@app.get("/v1/{project_id}/jobs/{name}/status")
def dataarts_status(ds: DS, name: str):
    jobs = getattr(ds, "RT_JOBS", {})
    if name not in jobs:
        raise err(404, f"job {name} not found", "DLF.0100")
    st = jobs[name]
    nodes = [{"name": f"{name}_src", "status": st},
             {"name": f"{name}_agg", "status": "EXCEPTION" if name == "rt_user_behavior" else st}]
    return {"name": name, "status": st, "nodes": nodes, "startTime": _now_ms() - 3600_000}


@app.get("/v3/{project_id}/instances")
def gaussdb_instances(ds: DS, offset: int = 0, limit: int = 100):
    rows = oa.gaussdb_instances(ds)
    return {"instances": _offset(rows, offset, limit), "total_count": len(rows)}


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
def dms_instances(ds: DS, engine: str = "kafka"):
    rows = oa.kafka_instances(ds) if engine.lower() == "kafka" else []
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
