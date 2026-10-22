from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, TypeDecorator, create_engine
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
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
