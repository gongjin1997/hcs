"""HcsClient 构建：按租户（hcs_account）+ 项目（hcs_project）生成客户端，认证方式由租户的 auth_type 决定。"""
import json

import httpx

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt

from .auth import AkSkAuth, Auth, TokenAuth, TokenProvider
from .client import HcsClient
from .endpoints import EndpointResolver
from .errors import HcsError

DEFAULT_QPS: dict[str, float] = {"dataarts": 5, "mrs": 5, "gaussdb": 5, "dcs": 5, "ces": 10, "ecs": 5, "evs": 5, "vpc": 5,
                                 "elb": 5, "cce": 5, "cce_k8s": 10, "dws": 5, "iam": 5}
ROOT_SERVICES = ("dataarts", "mrs", "gaussdb", "dcs", "ces")      # Mock HCS 中挂在根路径的服务


def mock_endpoints(base: str) -> dict[str, str]:
    """开发联调：全部服务指向 Mock HCS（巡检服务在根路径，台账服务按 /{service} 前缀区分）。"""
    base = base.rstrip("/")
    eps = {svc: base for svc in ROOT_SERVICES}
    eps.update({svc: f"{base}/{svc}" for svc in ("ecs", "evs", "vpc", "elb", "cce", "dws")})
    eps["cce_k8s"] = f"{base}/k8s/{{cluster_id}}"
    eps["iam"] = base
    return eps


def _http(s: Settings) -> httpx.Client:
    # 局点 API 网关多为内部 CA 签发：配置 INSPECTOR_HCS_CA_FILE（PEM，挂载到容器 /app/certs）后按该 CA 校验
    verify: bool | str = s.hcs_ca_file if (s.hcs_verify and s.hcs_ca_file) else s.hcs_verify
    return httpx.Client(verify=verify, timeout=httpx.Timeout(10.0, connect=5.0))


def _mock(s: Settings, mock_url: str | None) -> str | None:
    return mock_url or (s.hcs_mock_url if s.env != "prod" else None)


def build_client(settings: Settings | None = None, http: httpx.Client | None = None) -> HcsClient:
    """兜底：按环境变量里的单账号配置构建（数据库中一个租户都没有时使用；测试中注入 Mock HCS 的 TestClient）。"""
    s = settings or get_settings()
    http = http or _http(s)
    templates = {**s.hcs_endpoints, "iam": s.hcs_iam_url}
    tokens = TokenProvider(http, s.hcs_iam_url, s.hcs_domain, s.hcs_username, s.hcs_password, s.hcs_project_id,
                           endpoint_scope=json.dumps([templates, s.hcs_region, s.hcs_external_domain, s.hcs_scheme],
                                                     sort_keys=True))
    resolver = EndpointResolver(templates, s.hcs_region, s.hcs_external_domain, s.hcs_scheme)
    return HcsClient(http, TokenAuth(tokens), resolver, s.hcs_project_id, qps=DEFAULT_QPS, tenant=s.hcs_domain)


def tenant_auth(tenant, project_id: str, http: httpx.Client, iam_url: str, *, project_ref: int | None = None,
                endpoint_scope: str = "") -> Auth:
    secret = decrypt(tenant.secret_cipher) if tenant.secret_cipher else ""
    if tenant.auth_type == "AKSK":
        return AkSkAuth(tenant.access_key or "", secret, project_id=project_id, domain_id=tenant.domain_id)
    return TokenAuth(TokenProvider(http, iam_url, tenant.domain_name, tenant.username or "", secret, project_id,
                                   domain_id=tenant.domain_id, tenant_id=tenant.id, project_ref=project_ref,
                                   endpoint_scope=endpoint_scope))


def client_for(tenant, project, http: httpx.Client | None = None, *, mock_url: str | None = None,
               settings: Settings | None = None) -> HcsClient:
    """租户 + 项目 → HcsClient。Endpoint = 全局模板 ← 租户自定义覆盖；mock_url 仅用于开发 / 演示 / 测试。"""
    s = settings or get_settings()
    if getattr(project, "account_id", tenant.id) != tenant.id:
        raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "项目与租户归属不一致，未构建云客户端")
    http = http or _http(s)
    mock = _mock(s, mock_url)
    iam_url = mock or tenant.iam_endpoint
    templates = {**s.hcs_endpoints, **(tenant.endpoints or {}), "iam": tenant.iam_endpoint}
    if mock:
        templates.update(mock_endpoints(mock))
    resolver = EndpointResolver(templates, project.region_id, project.external_domain,
                                "http" if mock and mock.startswith("http://") else s.hcs_scheme)
    endpoint_scope = json.dumps([templates, project.region_id, project.external_domain, resolver.scheme], sort_keys=True)
    return HcsClient(http, tenant_auth(tenant, project.project_id, http, iam_url, project_ref=project.id,
                                      endpoint_scope=endpoint_scope),
                     resolver, project.project_id, qps=DEFAULT_QPS, tenant=tenant.code, mock_url=mock,
                     tenant_id=tenant.id, project_ref=project.id,
                     ces_enabled=str(getattr(tenant, "metrics_source", None) or "AUTO").upper() == "CES")

