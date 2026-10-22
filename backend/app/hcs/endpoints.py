class EndpointResolver:
    """HCS 服务地址解析。

    HCS 的 Endpoint 一般形如 {service}.{region}.{external_domain}，各局点可能不同，
    因此按服务配置模板（可写完整 URL 覆盖），例如 "dcs.{region}.{domain}" 或 "http://127.0.0.1:9000"。
    模板里还可以带调用方传入的变量，例如 CCE 集群 Kubernetes API："{cluster_id}.cce.{region}.{domain}"。
    """

    def __init__(self, templates: dict[str, str], region: str, domain: str, scheme: str = "https"):
        self.templates, self.region, self.domain, self.scheme = templates, region, domain, scheme

    def base(self, service: str, **host_vars: str) -> str:
        tpl = self.templates.get(service)
        if not tpl:
            raise KeyError(f"未配置服务 Endpoint：{service}")
        host = tpl.format(region=self.region, domain=self.domain, **host_vars)
        return (host if "://" in host else f"{self.scheme}://{host}").rstrip("/")

    def url(self, service: str, path: str, **host_vars: str) -> str:
        return f"{self.base(service, **host_vars)}/{path.lstrip('/')}"
