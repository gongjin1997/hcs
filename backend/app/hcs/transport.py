"""有界只读响应：不隐式跳转，限制大小及读取时间，避免请求超过在途租约。"""
import time

import httpx

from .errors import HcsError
from .ratelimit import remaining_time


def send_bounded(http: httpx.Client, request: httpx.Request, timeout: float = 10) -> httpx.Response:
    timeout = min(10, max(0.001, remaining_time(timeout)))
    timeouts = request.extensions.get("timeout", {})
    request.extensions["timeout"] = {key: min(timeouts.get(key) or timeout, timeout)
                                      for key in ("connect", "read", "write", "pool")}
    deadline = time.monotonic() + timeout
    response = http.send(request, stream=True, follow_redirects=False)
    try:
        content = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout("上游响应超过读取时间预算", request=request)
            if len(content) + len(chunk) > 16 * 1024 * 1024:
                raise HcsError(0, "LOCAL.RESPONSE_BUDGET", "上游响应超过单次大小预算，未使用不完整结果")
            content.extend(chunk)
        headers = [(key, value) for key, value in response.headers.multi_items()
                   if key.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(response.status_code, headers=headers, content=bytes(content),
                              request=request, extensions=response.extensions)
    finally:
        response.close()
