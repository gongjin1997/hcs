"""API 进程内的请求计数与耗时直方图（/metrics 输出；只统计本进程，多进程部署时 Prometheus 按实例分别抓取）。"""
import threading
from collections import defaultdict

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_lock = threading.Lock()
requests: dict[tuple[str, str], int] = defaultdict(int)          # (method, status) → 次数
latency_buckets = [0] * (len(BUCKETS) + 1)
latency_sum = 0.0
latency_count = 0
SKIP_PREFIX = ("/metrics", "/api/v1/stream/alarms", "/healthz", "/readyz")


def observe(method: str, path: str, status: int, seconds: float) -> None:
    global latency_sum, latency_count
    if path.startswith(SKIP_PREFIX):
        return
    with _lock:
        requests[(method, str(status))] += 1
        latency_sum += seconds
        latency_count += 1
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                latency_buckets[i] += 1
                break
        else:
            latency_buckets[-1] += 1


def snapshot() -> tuple[dict[tuple[str, str], int], list[int], float, int]:
    with _lock:
        return dict(requests), list(latency_buckets), latency_sum, latency_count
