"""统一响应信封与业务错误码：{"code": 0, "message": "ok", "data": ...}"""
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorCode:
    OK = 0
    BAD_REQUEST = 40001
    UNAUTHORIZED = 40101
    FORBIDDEN = 40301
    NOT_FOUND = 40401
    CONFLICT = 40901
    INTERNAL = 50001
    HCS_UPSTREAM = 50201


class BizError(Exception):
    def __init__(self, code: int, message: str, http_status: int = 400, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.http_status, self.data = code, message, http_status, data


class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


def ok(data: Any = None, message: str = "ok") -> dict:
    return {"code": ErrorCode.OK, "message": message, "data": data}


def not_found(what: str) -> BizError:
    return BizError(ErrorCode.NOT_FOUND, f"{what}不存在", 404)
