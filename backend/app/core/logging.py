"""本机结构化日志：请求/任务关联、脱敏、按服务轮转，不依赖外部日志服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
_SECRET = re.compile(
    r"password|passwd|authorization|cookie|secret|credential|private_?key|access_?key|secret_?key|"
    r"(^|_)(token|ticket|jwt|ak|sk|dsn|headers|body|payload|params|parameters)($|_)", re.I)
_ASSIGNMENT = re.compile(
    r'''(?ix)(?<![\w-])(["']?[\w-]{0,64}(?:password|passwd|authorization|cookie|secret|credentials?|access[_-]?key|'''
    r'''secret[_-]?key|private[_-]?key|token|ticket|jwt|ak|sk)(?:[_-][\w-]{1,32})?["']?\s*[:=]\s*)'''
    r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s,;\}\]\n]+)''')
_URL = re.compile(r"(?:https?|postgres(?:ql)?(?:\+\w+)?|redis(?:s)?)://[^\s<>\"']+", re.I)
_RID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,63}$")
_MAX_TEXT = 32768
_MAX_RECORD_BYTES = 64 * 1024


def request_id(value: str | None = None) -> str:
    return value if value and _RID.fullmatch(value) else uuid.uuid4().hex


def safe_path(path: str) -> str:
    """查询串/片段和邮件链接路径中的凭据不进入日志。"""
    path = path.split("?", 1)[0].split("#", 1)[0]
    return re.sub(r"(/(?:api/v1/mail-actions|m/action)/)[^/]+", r"\1[redacted]", path)[:1024]


def sanitize_text(value: str) -> str:
    def url(match: re.Match[str]) -> str:
        from urllib.parse import urlsplit, urlunsplit

        try:
            parsed = urlsplit(match.group())
            if parsed.scheme.lower() not in ("http", "https"):
                return f"{parsed.scheme}://[redacted]"
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], safe_path(parsed.path), "", ""))
        except ValueError:
            return "[redacted-url]"

    text = _URL.sub(url, value[:_MAX_TEXT * 2])
    text = re.sub(r"(?i)Bearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted-jwt]", text)
    text = re.sub(r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted-key]", text)
    text = re.sub(r"(?s)\[parameters:.*?(?:\]\s*(?=\n|$)|$)", "[parameters: redacted]", text)
    text = _ASSIGNMENT.sub(r"\1[redacted]", text)
    text = re.sub(r"(/(?:api/v1/mail-actions|m/action)/)[^\s/?#\"']+", r"\1[redacted]", text)
    text = re.sub(r"(/[A-Za-z0-9_./:%{}-]+)\?[^\s\"']+", r"\1?[redacted]", text)
    return text[:_MAX_TEXT] + ("...[truncated]" if len(text) > _MAX_TEXT else "")


def sanitize_log(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k)[:128]: "[redacted]" if _SECRET.search(str(k).replace("-", "_"))
                else sanitize_log(v, _depth=_depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_log(v, _depth=_depth + 1) for v in list(value)[:100]]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return sanitize_text(value if isinstance(value, str) else str(value))


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def bind_log_context(**fields: Any) -> None:
    """补充当前独立请求的已验证身份；同步依赖线程共享该请求字典。"""
    current = _context.get()
    if current is not None:
        current.update(fields)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "api") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "level": record.levelname, "logger": record.name, "service": self.service,
                "pid": record.process, "thread": record.threadName, "msg": record.getMessage()}
        reserved = set(data)
        data.update({k: v for k, v in (_context.get() or {}).items() if k not in reserved})
        data.update({k: v for k, v in record.__dict__.items()
                     if k not in _STANDARD and k not in reserved and not k.startswith("_")})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack"] = record.stack_info
        clean = sanitize_log(data)
        encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) <= _MAX_RECORD_BYTES:
            return encoded
        # 异常和关联字段优先；超大嵌套 extra 不能让日志文件和磁盘 I/O 无界增长。
        priority = ("ts", "level", "logger", "service", "pid", "request_id", "task", "run_id",
                    "tenant_id", "project_ref", "status", "msg", "exc")
        trimmed: dict[str, Any] = {}
        omitted = 0
        for key in [*(k for k in priority if k in clean), *(k for k in clean if k not in priority)]:
            value = clean[key]
            size = len(json.dumps({**trimmed, key: value}, ensure_ascii=False).encode("utf-8"))
            if size > _MAX_RECORD_BYTES - 64 and isinstance(value, str):
                remaining = max(0, _MAX_RECORD_BYTES - len(json.dumps(trimmed, ensure_ascii=False).encode("utf-8")) - 256)
                value = value.encode("utf-8")[:remaining // 4].decode("utf-8", errors="ignore") + "...[truncated]"
                size = len(json.dumps({**trimmed, key: value}, ensure_ascii=False).encode("utf-8"))
            if size <= _MAX_RECORD_BYTES - 64:
                trimmed[key] = value
            else:
                omitted += 1
        trimmed["truncated_fields"] = omitted
        return json.dumps(trimmed, ensure_ascii=False, allow_nan=False)


def _write_failure() -> None:
    try:
        sys.stderr.write('{"level":"ERROR","logger":"logging","msg":"log.write_failed"}\n')
    except OSError:
        pass


class SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        _write_failure()


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            os.close(fd)
            raise ValueError("日志文件必须是当前用户独立的常规文件")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def handleError(self, record: logging.LogRecord) -> None:
        # logging 默认错误处理会输出未经 formatter 脱敏的 record.args。
        _write_failure()


def setup_logging(level: str | None = None, *, service: str | None = None) -> None:
    from .config import get_settings

    settings = get_settings()
    service = service or os.environ.get("INSPECTOR_LOG_SERVICE") or "api"
    service = re.sub(r"[^a-zA-Z0-9_-]", "_", service)[:40] or "app"
    formatter = JsonFormatter(service)
    handlers: list[logging.Handler] = [SafeStreamHandler(sys.stderr)]
    if settings.log_dir:
        folder = Path(settings.log_dir)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("日志目录必须是当前用户的私有目录（0700）")
        target = folder / f"{service}.jsonl"
        if target.is_symlink():
            raise ValueError("日志文件不能是符号链接")
        file_handler = PrivateRotatingFileHandler(target, maxBytes=settings.log_max_bytes,
                                                 backupCount=settings.log_backup_count, encoding="utf-8")
        os.chmod(target, 0o600)
        handlers.append(file_handler)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_inspector", False)]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._inspector = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level or settings.log_level)
    logging.captureWarnings(True)
    for name in ("httpx", "httpcore", "sqlalchemy.engine", "apscheduler.executors.default"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


class RequestIdMiddleware:
    """纯 ASGI 中间件覆盖异常、流式响应和断连，不缓存请求/响应体。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        rid = request_id(headers.get(b"x-request-id", b"").decode("latin-1"))
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        started = time.perf_counter()
        status, sent, first_byte_ms = 500, 0, None
        error: BaseException | None = None

        async def traced_send(message: Message) -> None:
            nonlocal status, sent, first_byte_ms
            if message["type"] == "http.response.start":
                status = message["status"]
                first_byte_ms = round((time.perf_counter() - started) * 1000, 1)
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message["headers"].append((b"x-request-id", rid.encode("ascii")))
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        with log_context(request_id=rid, method=scope["method"], path=safe_path(scope.get("path", "/"))):
            client = scope.get("client")
            if client:
                bind_log_context(client_ip=client[0])
            try:
                await self.app(scope, receive, traced_send)
            except asyncio.CancelledError as exc:
                error, status = exc, 499
                raise
            except Exception as exc:
                error = exc
                state["request_error_logged"] = True
                raise
            finally:
                from . import metrics
                from .config import get_settings

                duration = time.perf_counter() - started
                path = getattr(scope.get("route"), "path", None) or safe_path(scope.get("path", "/"))
                metrics.observe(scope["method"], path, status, duration)
                slow = duration * 1000 >= get_settings().log_slow_request_ms
                stream = path == "/api/v1/stream/alarms"
                quiet = path in ("/metrics", "/healthz", "/readyz") or stream
                severity = logging.ERROR if error and status != 499 or status >= 500 else (
                    logging.WARNING if status >= 400 or slow and not stream else logging.DEBUG if quiet else logging.INFO)
                logging.getLogger("access").log(
                    severity, "request.failed" if error and status != 499 else "request.completed",
                    extra={"path": safe_path(path), "status": status, "cost_ms": round(duration * 1000, 1),
                           "first_byte_ms": first_byte_ms, "response_bytes": sent, "slow": slow and not stream},
                    exc_info=(type(error), error, error.__traceback__) if error and status != 499 else None)
