"""CCE 集群 Kubernetes API（只读）：经 CCE API 网关 https://{cluster_id}.cce.{region}.{domain} 访问，认证沿用租户的
Token / AK/SK；需要租户的 IAM 用户在集群 RBAC 中有 pods / events 的 list 权限（只读 ClusterRole 即可）。"""
import re

from app.hcs import HcsClient

SERVICE = "cce_k8s"
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


class KubeApi:
    def __init__(self, client: HcsClient, cluster_id: str, page_size: int = 500):
        self.client, self.cluster_id, self.page_size = client, cluster_id, page_size

    def _list(self, path: str, params: dict | None = None, max_pages: int = 50) -> list[dict]:
        items: list[dict] = []
        cont = None
        for _ in range(max_pages):
            p = {**(params or {}), "limit": self.page_size, **({"continue": cont} if cont else {})}
            data = self.client.get(SERVICE, path, params=p, host_vars={"cluster_id": self.cluster_id})
            items.extend(data.get("items") or [])
            cont = (data.get("metadata") or {}).get("continue")
            if not cont:
                break
        return items

    def pods(self, namespace: str | None = None) -> list[dict]:
        return self._list(f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods")

    def events(self, namespace: str | None = None) -> list[dict]:
        return self._list(f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events")

    def metrics_text(self, namespace: str, pod: str, port: int, path: str = "/metrics") -> str:
        """经 apiserver 的 Pod 代理抓取指标（无需 Prometheus，也无需开放 Pod 端口到集群外）。"""
        return self.client.get_text(SERVICE, f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy{path}",
                                    host_vars={"cluster_id": self.cluster_id})


def parse_metric(text: str, metric: str, label_filter: str = "") -> list[float]:
    """从 Prometheus 文本格式中取某个指标的全部样本值；label_filter 形如 "job_name=rt_order,task_name=Sink"。"""
    want = dict(x.split("=", 1) for x in [s.strip() for s in label_filter.split(",")] if "=" in x)
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m or m.group(1) != metric:
            continue
        labels = dict(_LABEL.findall(m.group(2) or ""))
        if any(labels.get(k.strip()) != v.strip() for k, v in want.items()):
            continue
        try:
            out.append(float(m.group(3)))
        except ValueError:
            continue
    return out


def aggregate(values: list[float], agg: str) -> float | None:
    if not values:
        return None
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "sum":
        return sum(values)
    return max(values)
