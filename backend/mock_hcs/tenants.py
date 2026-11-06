"""Mock HCS 的演示租户：两个租户接入方式、可用服务不同，Mock HCS、演示数据（seed）与自动化测试共用这份定义。

注意：这里的用户名 / 密码 / AK / SK 都是 Mock 专用的演示值，只能用于本地 Mock HCS；真实局点的凭据在
【系统管理 → 租户接入】页面录入，平台加密存储（AES-GCM），界面与接口只返回脱敏值。
"""
from types import ModuleType

from . import inventory, inventory_b

REGION = "region-01"
EXTERNAL_DOMAIN = "hcs.example.com"

TENANT_A: dict = {
    "code": "tenant-a", "name": "租户A · 核心生产", "short_name": "租户A", "color": "#1d63f0",
    "domain_name": "hcs_core_prod", "domain_id": "3c4d5e6f708192a3b4c5d6e7f8091a2b",
    "auth_type": "PASSWORD", "username": "inspector_ro", "password": "mock-password-for-demo-only",
    "project_id": "0a1b2c3d4e5f40718293a4b5c6d7e8f9", "project_name": f"{REGION}_core-prod",
    "workspaces": ["ws-bigdata"], "metrics_source": "AUTO", "prometheus_url": None,
    "report_mail_to": ["ops-core@example.com"],
    "services": ["iam", "ecs", "evs", "vpc", "elb", "rds", "gaussdb", "dcs", "dms", "mrs", "dataarts", "cce", "dws",
                 "css", "aom"],
    "remark": "核心交易、会员、大数据平台；IAM Token 缓存；AOM 自定义指标仅用于开发联调，无 CES",
    "ds": inventory,
}
TENANT_B: dict = {
    "code": "tenant-b", "name": "租户B · 互联网业务", "short_name": "租户B", "color": "#7a5af8",
    "domain_name": "hcs_web_biz", "domain_id": "8e9f0a1b2c3d4e5f60718293a4b5c6d7",
    "auth_type": "AKSK", "access_key": "MOCK-AK-TENANT-B", "secret_key": "mock-sk-tenant-b-for-demo-only",
    "project_id": "9f8e7d6c5b4a4c3d8e2f1a0b9c8d7e6f", "project_name": f"{REGION}_web-biz",
    "workspaces": [], "metrics_source": "PROMETHEUS", "prometheus_url": "http://prometheus.web.example.internal:9090",
    "report_mail_to": ["ops-web@example.com"],
    "services": ["iam", "ecs", "evs", "vpc", "elb", "rds", "dcs", "dms", "cce", "css", "aom"],  # 没有 CES/MRS/DataArts
    "remark": "门户、CMS、站内搜索；AK/SK 签名接入；局点未部署 CES，ECS 使用率来自租户 Prometheus（node_exporter）",
    "ds": inventory_b,
}
TENANTS = [TENANT_A, TENANT_B]
PROJECTS = {t["project_id"]: t for t in TENANTS}
BY_CODE = {t["code"]: t for t in TENANTS}
BY_DOMAIN = {t["domain_name"]: t for t in TENANTS}
AKSK = {TENANT_B["access_key"]: TENANT_B}


def tenant_for(project_id: str | None) -> dict:
    """按项目 ID 找租户；未知项目（例如早期单账号配置的 p1）按租户A 处理。"""
    return PROJECTS.get(project_id or "", TENANT_A)


def ds_for(project_id: str | None) -> ModuleType:
    return tenant_for(project_id)["ds"]


def has(t: dict, service: str) -> bool:
    return service in t["services"]


def clusters() -> dict[str, tuple[dict, str]]:
    """CCE 集群 ID → (租户, 集群名称)。"""
    return {t["ds"].rid("cce", c[0]): (t, c[0]) for t in TENANTS for c in t["ds"].CCE}
