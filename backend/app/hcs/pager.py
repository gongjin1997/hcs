import hashlib
import json
from collections.abc import Callable, Iterator

from .errors import HcsError


def response_items(data: dict, key: str) -> list[dict]:
    """缺失或畸形列表不是空资源清单；调用方必须保留已有快照。"""
    if isinstance(data, dict) and (data.get("error") or data.get("errors")
            or data.get("error_code") not in (None, "", 0, "0")
            or "message" in data and data.get("code") not in (None, "", 0, "0", 200, "200")):
        # 不拼接上游错误文本，避免将内部信息/凭据写入同步记录。
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "上游列表包含错误信息，未使用失败清单")
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", f"上游列表字段 {key} 缺失或格式不正确")
    return items


def response_total(data: dict, key: str, *, required: bool = False, allow_string: bool = True) -> int | None:
    """按接口契约核对总数；只对明确声明总数必需的调用方启用 required。"""
    total = data.get(key)
    if total is None:
        if required:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", f"上游分页缺少总数字段 {key}")
        return None
    if allow_string and isinstance(total, str) and total.isdecimal():
        total = int(total)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "上游分页总数格式不正确")
    return total


def page_all(fetch: Callable[[dict], dict], items_key: str, *, limit: int = 100, mode: str = "offset",
             start: int = 0, total_key: str | None = "total", max_pages: int = 200,
             offset_param: str = "offset", limit_param: str = "limit", max_items: int = 20000) -> Iterator[dict]:
    """通用分页遍历。

    mode="offset"：offset 为记录偏移（DCS、GaussDB 等），按实际返回条数推进，兼容服务端缩页；
    mode="page"：offset 为页码（部分接口从 1 开始），每页 offset += 1。
    有 total 时以总数为准；无 total 时以空页 / 短页结束。页数耗尽报错，不能返回不完整快照。
    """
    if limit <= 0 or max_pages <= 0 or max_items <= 0 or mode not in ("offset", "page"):
        raise ValueError("分页大小、页数和模式不正确")
    cursor, seen = start, 0
    pages: set[bytes] = set()
    for _ in range(min(max_pages, 200)):
        data = fetch({offset_param: cursor, limit_param: limit})
        items = response_items(data, items_key)
        total = response_total(data, total_key) if total_key is not None else None
        consumed = seen + len(items) + (start if mode == "offset" else 0)
        if items and total is not None and consumed > total:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游分页总数小于已返回记录数，未使用矛盾清单")
        if not items:
            if total is not None and seen + (start if mode == "offset" else 0) < total:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表提前结束，未取得完整清单")
            return
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).digest()
        if fingerprint in pages or seen + len(items) > max_items:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表重复或超过条数预算，未使用不完整清单")
        pages.add(fingerprint)
        yield from items
        seen += len(items)
        if total is not None and seen + (start if mode == "offset" else 0) >= total:
            return
        if total is None and len(items) < limit:
            return
        cursor += len(items) if mode == "offset" else 1
    raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表超过分页上限，未取得完整清单")
