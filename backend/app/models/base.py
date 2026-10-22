from datetime import UTC, datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import UtcDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)
