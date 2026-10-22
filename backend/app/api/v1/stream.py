"""告警实时推送（SSE）。

EventSource 不能设置请求头：前端先用登录令牌换取 60 秒有效、只能用一次的票据（POST /stream/ticket），
再用票据建立连接（GET /stream/alarms?ticket=...），登录令牌不会出现在 URL 和 Nginx 访问日志里。
连接断开后前端重新换票据再连（见 frontend/src/composables/useAlarmStream.ts）。
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUser, require_perm, scope_of, user_from_ticket
from app.core.events import broadcaster
from app.core.response import BizError, ErrorCode, ok
from app.core.security import SSE_TTL, create_sse_ticket

router = APIRouter(tags=["实时推送"])


@router.post("/stream/ticket", summary="换取 SSE 一次性票据（60 秒内有效，只能使用一次）")
def ticket(cu: CurrentUser = Depends(require_perm("alarm:view"))):
    return ok({"ticket": create_sse_ticket(cu.username, cu.id), "expires_in": SSE_TTL})


@router.get("/stream/alarms", summary="告警实时推送（SSE，按用户租户范围过滤）")
async def alarm_stream(request: Request, ticket: str = Query(..., description="POST /stream/ticket 换取的一次性票据"),
                       tenant: str | None = Query(None, description="顶部租户切换（可选）")):
    user = await asyncio.to_thread(user_from_ticket, ticket)
    if not user.can("alarm:view"):
        raise BizError(ErrorCode.FORBIDDEN, "缺少权限：查看告警", 403)
    scope = scope_of(user, tenant)
    queue = broadcaster.subscribe()

    def visible(ev: dict) -> bool:
        return scope.tenant_ids is None or (ev.get("alarm") or {}).get("tenant_id") in scope.tenant_ids

    async def gen():
        try:
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                    if visible(ev):
                        yield f"event: alarm\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"        # 心跳，防止 Nginx / 负载均衡空闲断开
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
