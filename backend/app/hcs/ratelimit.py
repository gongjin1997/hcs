import threading
import time


class TokenBucket:
    """令牌桶限流：按服务维度控制调用 HCS API 的 QPS，避免巡检流量冲击 API 网关。"""

    def __init__(self, rate: float, capacity: int | None = None):
        self.rate = rate
        self.capacity = capacity or max(1, int(rate))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
                wait = (1 - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(wait)
