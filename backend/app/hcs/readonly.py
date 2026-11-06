"""额外允许的 GaussDB 查询型 POST；固定路径，不开放动作或任意 SQL。"""
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ces_access: ContextVar[object | None] = ContextVar("ces_explicit_access", default=None)


@contextmanager
def ces_access(client: object, enabled: bool) -> Iterator[None]:
    """只在已核对租户选择的调用链内授权 CES，不修改共享客户端状态。"""
    token = _ces_access.set(client if enabled is True else None)
    try:
        yield
    finally:
        _ces_access.reset(token)


def ces_authorized(client: object) -> bool:
    return _ces_access.get() is client

_GAUSS_QUERY = re.compile(
    r"/gaussdb/v3/\{project_id\}/instances/[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}/"
    r"(?:slow-sql-execute-node|slow-sql-list|slow-sql-detail|real-time-session)"
)


def gaussdb_query_post(path: str) -> bool:
    return path == "/gaussdb/v3/{project_id}/inspections/records" or _GAUSS_QUERY.fullmatch(path) is not None
