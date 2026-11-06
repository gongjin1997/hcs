"""CCE 原生 Kubernetes API：经显式配置的 {cluster_id}.Endpoint 网关使用租户 Token / AK/SK。

跨命名空间 list pods/events 需集群 RBAC；抓指标另需 get pods/proxy。这里不代替证书认证的直连 API Server。
"""
import math
import re
from urllib.parse import quote

from app.hcs import HcsClient, HcsError
from app.hcs.pager import response_items

from .semantics import label_pairs, validate_metrics_path

SERVICE = "cce_k8s"
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


class KubeApi:
    def __init__(self, client: HcsClient, cluster_id: str, page_size: int = 500):
        if not 1 <= page_size <= 500:
            raise ValueError("Kubernetes 分页大小须为 1–500")
        self.client, self.cluster_id, self.page_size = client, cluster_id, page_size

    def _list(self, path: str, params: dict | None = None, max_pages: int = 50) -> list[dict]:
        if max_pages < 1:
            raise ValueError("Kubernetes 分页次数须大于零")
        items: list[dict] = []
        cont = None
        seen_tokens = set()
        resource_version: str | None = None
        for _ in range(min(max_pages, 50)):
            p = {**(params or {}), "limit": self.page_size, **({"continue": cont} if cont else {})}
            data = self.client.get(SERVICE, path, params=p, host_vars={"cluster_id": self.cluster_id})
            items.extend(response_items(data, "items"))
            if len(items) > 25000:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "Kubernetes 列表超过条数预算，未使用不完整清单")
            metadata = data.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes 分页元数据格式不正确")
            version = (metadata or {}).get("resourceVersion")
            if version is not None and not isinstance(version, str):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes resourceVersion 格式不正确")
            if resource_version is not None and version != resource_version:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "Kubernetes 分页快照版本改变，未使用不一致清单")
            if version is not None:
                resource_version = version
            cont = (metadata or {}).get("continue")
            if cont is None or cont == "":
                return items
            if not isinstance(cont, str) or cont in seen_tokens:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "Kubernetes 分页标记无效或重复")
            if not version:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "Kubernetes 分页快照版本缺失，未使用不一致清单")
            seen_tokens.add(cont)
        raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "Kubernetes 列表超过分页上限，未取得完整清单")

    def pods(self, namespace: str | None = None) -> list[dict]:
        pods = self._list(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods" if namespace else "/api/v1/pods")
        seen = set()
        for pod in pods:
            metadata = pod.get("metadata")
            if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str) or not metadata[key]
                                                     for key in ("name", "namespace")):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes Pod 标识缺失，未使用不完整清单")
            key = (metadata["namespace"], metadata["name"])
            if key in seen:
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes Pod 清单重复，未使用不一致快照")
            seen.add(key)
            for section in ("spec", "status"):
                if pod.get(section) is not None and not isinstance(pod[section], dict):
                    raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes Pod 状态结构无效")
            for section in ("containerStatuses", "initContainerStatuses"):
                statuses = (pod.get("status") or {}).get(section, [])
                if not isinstance(statuses, list) or any(not isinstance(cs, dict)
                        or not isinstance(cs.get("name"), str) or not cs["name"]
                        or type(cs.get("restartCount")) is not int or cs["restartCount"] < 0
                        or type(cs.get("ready")) is not bool or not isinstance(cs.get("state"), dict)
                        or cs.get("lastState") is not None and not isinstance(cs["lastState"], dict)
                        for cs in statuses):
                    raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes 容器状态或重启次数缺失，未按零重启采集")
        return pods

    def events(self, namespace: str | None = None) -> list[dict]:
        events = self._list(f"/api/v1/namespaces/{quote(namespace, safe='')}/events" if namespace else "/api/v1/events")
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("uid"), str) or not metadata["uid"] \
                    or not isinstance(event.get("involvedObject"), dict) \
                    or event.get("series") is not None and not isinstance(event["series"], dict):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "Kubernetes Event 标识或事件对象缺失，未使用不完整清单")
        return events

    def metrics_text(self, namespace: str, pod: str, port: int, path: str = "/metrics") -> str:
        """经 apiserver 的 Pod 代理抓取指标（无需 Prometheus，也无需开放 Pod 端口到集群外）。"""
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise HcsError(0, "LOCAL.INVALID_PATH", "Pod 代理端口须为 1–65535")
        try:
            validate_metrics_path(path)
        except ValueError as exc:
            raise HcsError(0, "LOCAL.INVALID_PATH", str(exc)) from None
        return self.client.get_text(SERVICE, f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/"
                                    f"{quote(pod, safe='')}:{port}/proxy{path}",
                                    host_vars={"cluster_id": self.cluster_id})


def parse_metric(text: str, metric: str, label_filter: str = "") -> list[float]:
    """从 Prometheus 文本格式中取某个指标的全部样本值；label_filter 形如 "job_name=rt_order,task_name=Sink"。"""
    try:
        want = label_pairs(label_filter)
    except ValueError:
        return []
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
            value = float(m.group(3))
            if not math.isfinite(value):
                return []
            out.append(value)
        except ValueError:
            return []
    return out


def aggregate(values: list[float], agg: str) -> float | None:
    if not values or any(not math.isfinite(value) for value in values):
        return None
    if agg == "avg":
        value = sum(v / len(values) for v in values)
        return value if math.isfinite(value) else None
    if agg == "sum":
        value = sum(values)
        return value if math.isfinite(value) else None
    return max(values)
