"""浏览器错误只写本机日志；不采集页面内容、登录凭据或浏览器存储。"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.deps import CurrentUser, Scope, get_current_user, get_scope
from app.core.logging import request_id, sanitize_text
from app.core.response import BizError, ErrorCode, ok

router = APIRouter(prefix="/diagnostics", tags=["故障定位"])
log = logging.getLogger("browser")
_lock = threading.Lock()
_windows: OrderedDict[int, tuple[float, int, set[str]]] = OrderedDict()
_ROUTE = re.compile(r"^/(?:dashboard|bigscreen|alarms|ledger|pod-events|reports|rules|silences|notify|"
                    r"system|dcs|guides|job-reports(?:/[a-z-]+)?|login|403|resources|m/action/:token)$")
_FRAME = re.compile(r"(?:^|[/\\\s(])([A-Za-z0-9_.-]+\.(?:js|ts|vue)):(\d{1,7}):(\d{1,7})")


class ClientError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["vue", "error", "unhandledrejection"]
    error_name: str = Field(default="Error", max_length=80)
    message: str = Field(default="", max_length=256)
    route: str = Field(default="/", max_length=160)
    stack: str = Field(default="", max_length=2000)
    request_id: str | None = Field(default=None, max_length=64)


def _admit(uid: int, fingerprint: str) -> bool:
    now = time.monotonic()
    with _lock:
        start, count, seen = _windows.get(uid, (now, 0, set()))
        if now - start >= 60:
            start, count, seen = now, 0, set()
        if count >= 10 or fingerprint in seen:
            return False
        seen.add(fingerprint)
        _windows[uid] = (start, count + 1, seen)
        _windows.move_to_end(uid)
        while len(_windows) > 1024:
            _windows.popitem(last=False)
        return True


@router.post("/client-errors", summary="记录浏览器错误（本机、限速、去重）")
async def client_error(request: Request, user: CurrentUser = Depends(get_current_user),
                       scope: Scope = Depends(get_scope)):
    # 不使用未限长的 request.json()/Pydantic 自动读取请求体。
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > 8192:
            raise BizError(ErrorCode.BAD_REQUEST, "错误报告过大", 413)
        content.extend(chunk)
    try:
        body = ClientError.model_validate(json.loads(content))
    except (ValueError, ValidationError) as exc:
        raise BizError(ErrorCode.BAD_REQUEST, "错误报告格式不正确") from exc
    route = body.route if _ROUTE.fullmatch(body.route) else "[other]"
    # 客户端可被修改。只接受技术类别、文件名/行号，不接受任意异常消息/URL。
    error_name = body.error_name if body.error_name in {
        "Error", "TypeError", "ReferenceError", "SyntaxError", "RangeError", "ChunkLoadError",
        "NetworkError", "UnhandledRejection", "VueError", "URIError", "EvalError"} else "Error"
    frames = [f"{name}:{line}:{column}" for name, line, column in _FRAME.findall(body.stack)[:12]]
    fingerprint = hashlib.sha256(json.dumps([body.kind, error_name, route, frames]).encode()).hexdigest()[:20]
    if _admit(user.id, fingerprint):
        original = body.request_id
        log.error("browser.error", extra={
            "event": "browser.error", "kind": body.kind, "error_name": error_name, "route": route,
            "frames": frames, "fingerprint": fingerprint,
            "client_request_id": original if original and request_id(original) == original else None,
            "message_category": sanitize_text(body.kind), "user_id": user.id, "tenant_id": scope.single,
        })
    return ok()
