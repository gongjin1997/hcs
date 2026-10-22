import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.core.deps import scope_of, user_from_token
from app.core.events import broadcaster
from app.core.response import BizError, ErrorCode

router = APIRouter(tags=["实时推送"])


@router.get("/stream/alarms", summary="告警实时推送（SSE，按用户租户范围过滤）")
async def alarm_stream(request: Request, token: str = Query(..., description="EventSource 无法设置 Header，Token 走 Query"),
                       tenant: str | None = Query(None, description="顶部租户切换（可选）")):
    user = user_from_token(token)
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
