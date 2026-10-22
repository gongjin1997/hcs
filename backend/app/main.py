import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, models  # noqa: F401  注册全部 ORM 模型
from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import Base, SessionLocal, engine
from app.core.events import broadcaster, redis_forwarder
from app.core.logging import RequestIdMiddleware, setup_logging
from app.core.response import BizError, ErrorCode

log = logging.getLogger(__name__)


def _err(status: int, code: int, message: str, data=None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, "data": jsonable_encoder(data)})


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.env != "prod":
            Base.metadata.create_all(engine)     # 开发便捷；生产环境使用 alembic upgrade head
        if settings.demo_seed and settings.env == "prod":
            log.warning("INSPECTOR_DEMO_SEED is ignored in prod; use python -m app.workers.bootstrap")
        elif settings.demo_seed:
            from app.services.seed import seed_demo
            with SessionLocal() as db:
                if seed_demo(db):
                    log.info("demo data seeded")
        broadcaster.bind_loop(asyncio.get_running_loop())
        forwarder = asyncio.create_task(redis_forwarder()) if settings.redis_url else None
        yield
        if forwarder:
            forwarder.cancel()

    app = FastAPI(title="HCS 巡检平台 API", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(BizError)
    async def biz_error(_: Request, e: BizError):
        return _err(e.http_status, e.code, e.message, e.data)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        return _err(400, ErrorCode.BAD_REQUEST, "参数校验失败", e.errors())

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, e: StarletteHTTPException):
        code = {401: ErrorCode.UNAUTHORIZED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND}.get(
            e.status_code, ErrorCode.BAD_REQUEST if e.status_code < 500 else ErrorCode.INTERNAL)
        return _err(e.status_code, code, str(e.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, e: Exception):
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return _err(500, ErrorCode.INTERNAL, "服务器内部错误", {"request_id": getattr(request.state, "request_id", None)})

    app.include_router(api_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app


app = create_app()
