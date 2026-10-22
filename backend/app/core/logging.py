import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

_EXTRA = ("request_id", "method", "path", "status", "cost_ms", "task", "project")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        for k in _EXTRA:
            if hasattr(record, k):
                data[k] = getattr(record, k)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 / 透传 X-Request-Id，并输出结构化访问日志。"""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        logging.getLogger("access").info(
            "%s %s", request.method, request.url.path,
            extra={"request_id": rid, "method": request.method, "path": request.url.path,
                   "status": response.status_code, "cost_ms": round((time.perf_counter() - start) * 1000, 1)})
        return response
