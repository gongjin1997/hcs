from urllib.parse import urlsplit


def configuration_warnings(region: str, domain: str, project_name: str | None) -> list[str]:
    """只提示底座参数差异，不根据控制台地址或资源空间名称改写正式配置。"""
    warnings = []
    if domain.lower().removeprefix("https://").removeprefix("http://").startswith("console."):
        warnings.append("外部域名包含 console. 前缀，请按底座导出表核对 external_global_domain_name")
    if project_name and region.endswith("_" + project_name):
        warnings.append("区域可能填成资源空间名称，请按底座导出表核对 region0_id，不自动截取名称")
    return warnings


class EndpointResolver:
    """HCS 服务地址解析。

    HCS 的 Endpoint 一般形如 {service}.{region}.{external_domain}，各局点可能不同，
    因此按服务配置模板（可写完整 URL 覆盖），例如 "dcs.{region}.{domain}" 或 "http://127.0.0.1:9000"。
    模板里还可以带调用方传入的变量，例如 CCE 集群 Kubernetes API："{cluster_id}.cce.{region}.{domain}"。
    """

    def __init__(self, templates: dict[str, str], region: str, domain: str, scheme: str = "https"):
        self.templates, self.region, self.domain, self.scheme = templates, region, domain, scheme

    def base(self, service: str, **host_vars: str) -> str:
        if any(not isinstance(value, str) or not value or any(c.isspace() or c in "/?#@\\{}%" for c in value)
               for value in host_vars.values()):
            raise ValueError("服务 Endpoint 变量不得改变地址结构")
        tpl = self.templates.get(service)
        if not tpl:
            raise KeyError(f"未配置服务 Endpoint：{service}")
        host = tpl.format(region=self.region, domain=self.domain, **host_vars)
        base = (host if "://" in host else f"{self.scheme}://{host}").rstrip("/")
        target = urlsplit(base)
        if (target.scheme not in ("http", "https") or not target.hostname or target.username is not None
                or target.password is not None or target.query or target.fragment or "?" in base or "#" in base
                or any(c.isspace() or c in "{}" for c in base)):
            raise ValueError("服务 Endpoint 须为不含凭据、查询串或片段的 HTTP(S) 地址")
        _ = target.port  # 在认证/发请求前拒绝格式错误和越界端口。
        return base

    def url(self, service: str, path: str, **host_vars: str) -> str:
        return f"{self.base(service, **host_vars)}/{path.lstrip('/')}"
