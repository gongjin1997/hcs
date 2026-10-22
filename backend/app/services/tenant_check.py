"""租户连通性测试与能力探测（系统管理 - 租户接入 - 测试连接）。

1. 认证：PASSWORD → 强制换取一次 Token；AKSK → 用 AK/SK 签名请求调用 IAM（GET /v3/projects）
2. 服务探测：ECS / CCE / DCS / GaussDB / MRS / DataArts 列表接口各调用一次（limit=1）
3. 能力探测（写入租户 capabilities，决定监控指标来源，见 app.metrics）：
   - 云监控 CES：GET /V1.0/{project_id}/metrics?limit=1 —— 局点未部署 CES 时一般返回 404 或域名无法解析
   - Prometheus（配置了地址时）：查询 up
   - CCE K8s API：对第一个集群 GET /api/v1/namespaces?limit=1（容器事件采集需要）
"""
import time
from collections.abc import Callable
from functools import partial

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.hcs import HcsClient, HcsError, client_for
from app.hcs.auth import TokenAuth
from app.k8s.kube import SERVICE as K8S_SERVICE
from app.metrics import effective, mark, service_missing
from app.metrics.prometheus import PromClient, PromError
from app.models import HcsAccount, HcsProject
from app.models.base import utcnow

SERVICE_PROBES: list[tuple[str, str, str, dict]] = [
    ("ecs", "ECS 云服务器列表", "/v1/{project_id}/cloudservers/detail", {"limit": 1, "offset": 1}),
    ("cce", "CCE 集群列表", "/api/v3/projects/{project_id}/clusters", {}),
    ("dcs", "DCS 实例列表", "/v2/{project_id}/instances", {"limit": 1}),
    ("gaussdb", "GaussDB 实例列表", "/v3/{project_id}/instances", {"limit": 1}),
    ("mrs", "MRS 集群列表", "/v1.1/{project_id}/cluster_infos", {"pageSize": 1, "currentPage": 1}),
    ("dataarts", "DataArts 作业列表", "/v1/{project_id}/jobs", {"limit": 1}),
]


def _token_check(client: HcsClient) -> str:
    if isinstance(client.auth, TokenAuth):
        client.auth.check()                      # 强制换取一次 Token，验证用户名 / 密码 / 域
    return "Token 获取成功"


def _probe(client: HcsClient, svc: str, path: str, params: dict, headers: dict | None) -> str | None:
    client.get(svc, path, params=params, headers=headers)
    return None


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def run(self, name: str, kind: str, fn: Callable[[], str | None]) -> bool:
        started = time.perf_counter()
        try:
            detail = fn()
        except (HcsError, PromError, KeyError) as e:
            self.items.append({"name": name, "kind": kind, "ok": False, "message": str(e)[:200]})
            return False
        self.items.append({"name": name, "kind": kind, "ok": True, "detail": detail or "",
                           "cost_ms": round((time.perf_counter() - started) * 1000)})
        return True


def _ces(c: _Checks, tenant: HcsAccount, client: HcsClient) -> None:
    started = time.perf_counter()
    try:
        client.get("ces", "/V1.0/{project_id}/metrics", params={"limit": 1})
    except HcsError as e:
        missing = service_missing(e)
        if missing:
            mark(tenant, "ces", False, f"未开通或不可达（{e.status or '网络错误'}）")
        hint = "局点未部署 CES 或 Endpoint 不可达，ECS 使用率将改用 Prometheus / 只巡检状态：" if missing else "CES 调用失败："
        c.items.append({"name": "云监控 CES", "kind": "capability", "ok": False, "state": "missing" if missing else "error",
                        "message": hint + str(e)[:160]})
        return
    mark(tenant, "ces", True, "连通性测试：接口可用")
    c.items.append({"name": "云监控 CES", "kind": "capability", "ok": True, "state": "available", "detail": "接口可用",
                    "cost_ms": round((time.perf_counter() - started) * 1000)})


def _prometheus(c: _Checks, tenant: HcsAccount, client: HcsClient) -> None:
    if not tenant.prometheus_url:
        return
    base = f"{client.mock_url}/prom/{tenant.code}" if client.mock_url else tenant.prometheus_url
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


def _cce_api(c: _Checks, tenant: HcsAccount, client: HcsClient) -> None:
    try:
        clusters = client.get("cce", "/api/v3/projects/{project_id}/clusters").get("items") or []
    except HcsError:
        return                                   # 服务探测里已经体现
    if not clusters:
        return
    meta = clusters[0].get("metadata") or {}
    started = time.perf_counter()
    try:
        client.get(K8S_SERVICE, "/api/v1/namespaces", params={"limit": 1}, host_vars={"cluster_id": meta.get("uid")})
    except HcsError as e:
        mark(tenant, "cce_api", False, f"{meta.get('name')}：{e.message[:120]}")
        c.items.append({"name": "CCE K8s API", "kind": "capability", "ok": False, "state": "error",
                        "message": f"集群 {meta.get('name')}：{str(e)[:160]}（容器事件采集需要集群只读 RBAC）"})
        return
    mark(tenant, "cce_api", True, f"集群 {meta.get('name')} 可访问")
    c.items.append({"name": "CCE K8s API", "kind": "capability", "ok": True, "state": "available",
                    "detail": f"集群 {meta.get('name')}（共 {len(clusters)} 个）",
                    "cost_ms": round((time.perf_counter() - started) * 1000)})


def check_tenant(db: Session, tenant: HcsAccount, *, http: httpx.Client | None = None,
                 mock_url: str | None = None) -> dict:
    c = _Checks()
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
            if tenant.auth_type == "AKSK":
                auth_ok = c.run("IAM 认证（AK/SK 签名）", "auth", lambda: "签名校验通过，可见项目 {} 个".format(
                    len(client.get("iam", "/v3/projects").get("projects") or [])))
            else:
                auth_ok = c.run("IAM 认证（用户名密码换 Token）", "auth", partial(_token_check, client))
            if auth_ok:
                for svc, name, path, params in SERVICE_PROBES:
                    if svc == "dataarts" and not project.workspaces:
                        continue
                    headers = {"workspace": project.workspaces[0]} if svc == "dataarts" else None
                    c.run(name, "service", partial(_probe, client, svc, path, params, headers))
                _ces(c, tenant, client)
                _prometheus(c, tenant, client)
                _cce_api(c, tenant, client)
    now = utcnow()
    essential = [x for x in c.items if x["kind"] in ("auth", "service")]
    result = {"at": now.isoformat(timespec="seconds"), "ok": bool(essential) and all(x["ok"] for x in essential),
              "checks": c.items, "metrics_source": effective(tenant), "hcs_version": get_settings().hcs_version}
    tenant.last_check, tenant.last_check_at = result, now
    db.commit()
    return result
