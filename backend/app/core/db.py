from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, TypeDecorator, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """统一按 UTC 存取时间：入库前转 UTC，出库补 tzinfo（SQLite 不保存时区）。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs: dict = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        return create_engine(url, **kwargs)
    # 开发 / 演示：API、调度、通知多个进程共用一个 SQLite 文件 → WAL + 忙等待，避免 “database is locked”（生产用 PostgreSQL）
    kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if ":memory:" not in url and url not in ("sqlite://", "sqlite:///"):
        @event.listens_for(eng, "connect")
        def _sqlite_pragma(dbapi_conn, _record) -> None:  # pragma: no cover - 仅本地 SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return eng


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
