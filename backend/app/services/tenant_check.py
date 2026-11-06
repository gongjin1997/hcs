"""租户连通性测试与能力探测（系统管理 - 租户接入 - 测试连接）。

当前按首个启用项目及其首个集群进行连通性烟测，不代表所有项目都已验证。

1. 认证：PASSWORD → 强制换取一次 Token；AKSK → 用 AK/SK 签名请求调用 IAM（GET /v3/projects）
2. 服务探测：ECS / CCE / DCS / GaussDB / MRS / DataArts 列表接口各调用一次（limit=1）
3. 能力探测（写入租户 capabilities，决定监控指标来源，见 app.metrics）：
   - AOM：全局停用；旧地址或探测结果不发起请求，仅返回跳过说明
   - 云监控 CES：仅全局显式启用且租户显式选择 CES 时测试；AUTO 从不探测 CES
   - Prometheus（配置了地址时）：查询 up
   - CCE K8s API：对第一个集群 GET /api/v1/namespaces?limit=1（容器事件采集需要）
"""
import time
from collections.abc import Callable
from contextlib import closing, nullcontext
from functools import partial
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.hcs import HcsClient, HcsError, client_for
from app.hcs.auth import TokenAuth
from app.hcs.cce_api import available_cluster, management_items
from app.hcs.endpoints import configuration_warnings
from app.hcs.gaussdb_api import gaussdb_instances_path
from app.hcs.pager import response_items, response_total
from app.k8s.kube import SERVICE as K8S_SERVICE
from app.metrics import effective, mark, service_missing, use_ces
from app.metrics.prometheus import PromClient, PromError
from app.models import HcsAccount, HcsProject
from app.models.base import utcnow

SERVICE_PROBES: list[tuple[str, str, str, dict]] = [
    ("ecs", "ECS 云服务器列表", "/v1/{project_id}/cloudservers/detail", {"limit": 1, "offset": 1}),
    ("cce", "CCE 集群列表", "/api/v3/projects/{project_id}/clusters", {}),
    ("dcs", "DCS 实例列表", "/v2/{project_id}/instances", {"limit": 1}),
    ("gaussdb", "GaussDB 实例列表", gaussdb_instances_path(), {"limit": 1}),
    ("mrs", "MRS 集群列表", "/v1.1/{project_id}/cluster_infos", {"pageSize": 1, "currentPage": 1}),
    ("dataarts", "DataArts 作业列表", "/v1/{project_id}/jobs", {"limit": 1}),
]


def _token_check(client: HcsClient) -> str:
    if isinstance(client.auth, TokenAuth):
        client.auth.check()                      # 强制换取一次 Token，验证用户名 / 密码 / 域
    return "Token 获取成功"


SERVICE_LIST_KEYS = {"ecs": "servers", "cce": "items", "dcs": "instances", "gaussdb": "instances",
                     "mrs": "clusters", "dataarts": "jobs"}


def _probe(client: HcsClient, svc: str, path: str, params: dict, headers: dict | None,
           responses: dict[str, dict]) -> str | None:
    data = client.get(svc, path, params=params, headers=headers)
    items = management_items(data) if svc == "cce" else response_items(data, SERVICE_LIST_KEYS[svc])
    total_key = {"ecs": "count", "mrs": "clusterTotal", "dataarts": "total"}.get(svc)
    if total_key:
        total = response_total(data, total_key, required=True, allow_string=svc == "mrs")
        if total is not None and (len(items) > total or not items and total > 0):
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "列表记录数与已声明总数不一致")
    responses[svc] = data
    return None


def _endpoint(client: HcsClient, service: str) -> str | None:
    try:
        target = urlsplit(client.resolver.base(service))
    except (KeyError, ValueError):
        return None
    return urlunsplit((target.scheme, target.netloc, "", "", ""))


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def run(self, name: str, kind: str, fn: Callable[[], str | None], *, endpoint: str | None = None,
            endpoint_source: str | None = None) -> bool:
        started = time.perf_counter()
        context = {"endpoint": endpoint, "endpoint_source": endpoint_source} if endpoint else {}
        try:
            detail = fn()
        except (HcsError, PromError, KeyError) as e:
            network = {"network_reason": e.reason, "retryable": e.retryable} if isinstance(e, HcsError) and e.reason else {}
            self.items.append({"name": name, "kind": kind, "ok": False, "message": str(e)[:200],
                               "cost_ms": round((time.perf_counter() - started) * 1000), **context, **network})
            return False
        self.items.append({"name": name, "kind": kind, "ok": True, "detail": detail or "",
                           "cost_ms": round((time.perf_counter() - started) * 1000), **context})
        return True


def _ces(c: _Checks, tenant: HcsAccount, client: HcsClient) -> None:
    if not use_ces(tenant):
        return
    started = time.perf_counter()
    try:
        client.get("ces", "/V1.0/{project_id}/metrics", params={"limit": 1})
    except HcsError as e:
        missing = service_missing(e)
        if missing:
            mark(tenant, "ces", False, f"未开通或不可达（{e.status or '网络错误'}）")
        hint = "显式配置的 CES 未部署或 Endpoint 不可达，保留服务原生字段：" if missing else "CES 调用失败："
        c.items.append({"name": "云监控 CES", "kind": "capability", "ok": False, "state": "missing" if missing else "error",
                        "message": hint + str(e)[:160]})
        return
    mark(tenant, "ces", True, "连通性测试：接口可用")
    c.items.append({"name": "云监控 CES", "kind": "capability", "ok": True, "state": "available", "detail": "接口可用",
                    "cost_ms": round((time.perf_counter() - started) * 1000)})


def _aom(c: _Checks, tenant: HcsAccount) -> None:
    """旧配置仅提示停用；不解析地址、不认证、不探测，也不覆盖历史能力记录。"""
    if (str(tenant.metrics_source or "AUTO").upper() == "AOM" or "aom" in (tenant.endpoints or {})
            or "aom" in (tenant.capabilities or {}) or "aom" in get_settings().hcs_endpoints):
        c.items.append({"name": "应用运维管理 AOM", "kind": "capability", "ok": True,
                        "state": "skipped", "available": False, "disabled": True,
                        "detail": "AOM 已全局停用，未发起认证或接口请求；旧配置和探测结果不参与选源"})


def _prometheus(c: _Checks, tenant: HcsAccount, client: HcsClient) -> None:
    url = str(tenant.prometheus_url or "").strip()
    if not url:
        return
    base = f"{client.mock_url}/prom/{tenant.code}" if client.mock_url else url
    started = time.perf_counter()
    try:
        n = PromClient(base, http=client.http).ping()
    except PromError as e:
        mark(tenant, "prometheus", False, str(e)[:160])
        c.items.append({"name": "Prometheus", "kind": "capability", "ok": False, "state": "error", "message": str(e)[:200]})
        return
    mark(tenant, "prometheus", True, f"{n} 个抓取目标")
    c.items.append({"name": "Prometheus", "kind": "capability", "ok": True, "state": "available",
                    "detail": f"{n} 个抓取目标", "cost_ms": round((time.perf_counter() - started) * 1000)})


def _cce_api(c: _Checks, tenant: HcsAccount, client: HcsClient, clusters: list[dict]) -> None:
    cluster = available_cluster(clusters)
    if cluster is None:
        reason = "当前项目没有可用集群，本次未验证 K8s API"
        mark(tenant, "cce_api", False, reason)
        c.items.append({"name": "CCE K8s API", "kind": "capability", "ok": False, "state": "unverified", "message": reason})
        return
    meta = cluster["metadata"]
    started = time.perf_counter()
    try:
        data = client.get(K8S_SERVICE, "/api/v1/namespaces", params={"limit": 1}, host_vars={"cluster_id": meta["uid"]})
        response_items(data, "items")
    except HcsError as e:
        mark(tenant, "cce_api", False, f"{meta.get('name')}：{e.message[:120]}")
        c.items.append({"name": "CCE K8s API", "kind": "capability", "ok": False, "state": "error",
                        "message": f"集群 {meta.get('name')}：{str(e)[:160]}（容器事件采集需要集群只读 RBAC）"})
        return
    mark(tenant, "cce_api", True, f"集群 {meta.get('name')} 可访问")
    c.items.append({"name": "CCE K8s API", "kind": "capability", "ok": True, "state": "available",
                    "detail": (f"集群 {meta.get('name') or meta['uid']} 的一个命名空间样本通过；"
                               f"列表共 {len(clusters)} 个集群，其余未验证"),
                    "cost_ms": round((time.perf_counter() - started) * 1000)})


def check_tenant(db: Session, tenant: HcsAccount, *, http: httpx.Client | None = None,
                 mock_url: str | None = None) -> dict:
    c = _Checks()
    client: HcsClient | None = None
    project = db.scalar(select(HcsProject).where(HcsProject.account_id == tenant.id, HcsProject.enabled.is_(True))
                        .order_by(HcsProject.id))
    if project is None:
        c.items.append({"name": "项目配置", "kind": "auth", "ok": False, "message": "租户下没有启用的项目"})
    else:
        try:
            client = client_for(tenant, project, http=http, mock_url=mock_url)
        except HcsError as e:
            client = None
            c.items.append({"name": "访问凭据", "kind": "auth", "ok": False, "message": str(e)[:200]})
        if client is not None:
            with closing(client.http) if http is None else nullcontext():
                if tenant.auth_type == "AKSK":
                    auth_ok = c.run("IAM 认证（AK/SK 签名）", "auth", lambda: "签名校验通过，可见项目 {} 个".format(
                        len(client.get("iam", "/v3/projects").get("projects") or [])))
                else:
                    auth_ok = c.run("IAM 认证（用户名密码换 Token）", "auth", partial(_token_check, client))
                if auth_ok:
                    responses: dict[str, dict] = {}
                    for svc, name, path, params in SERVICE_PROBES:
                        if svc == "dataarts" and not project.workspaces:
                            continue
                        headers = {"workspace": project.workspaces[0]} if svc == "dataarts" else None
                        source = "mock" if client.mock_url else "tenant" if svc in (tenant.endpoints or {}) else "global"
                        c.run(name, "service", partial(_probe, client, svc, path, params, headers, responses),
                              endpoint=_endpoint(client, svc), endpoint_source=source)
                    _ces(c, tenant, client)
                    _prometheus(c, tenant, client)
                    if "cce" in responses:
                        _cce_api(c, tenant, client, responses["cce"]["items"])
    _aom(c, tenant)
    now = utcnow()
    essential = [x for x in c.items if x["kind"] in ("auth", "service")]
    result = {"at": now.isoformat(timespec="seconds"), "ok": bool(essential) and all(x["ok"] for x in essential),
              "checks": c.items, "metrics_source": effective(tenant, client=client), "hcs_version": get_settings().hcs_version}
    result["configuration_warnings"] = configuration_warnings(
        project.region_id, project.external_domain, project.project_name) if project else []
    tenant.last_check, tenant.last_check_at = result, now
    db.commit()
    return result
