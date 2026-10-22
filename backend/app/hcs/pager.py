from collections.abc import Callable, Iterator


def page_all(fetch: Callable[[dict], dict], items_key: str, *, limit: int = 100, mode: str = "offset",
             start: int = 0, total_key: str = "total", max_pages: int = 200,
             offset_param: str = "offset", limit_param: str = "limit") -> Iterator[dict]:
    """通用分页遍历。

    mode="offset"：offset 为记录偏移（DCS、GaussDB 等），每页 offset += limit；
    mode="page"：offset 为页码（部分接口从 1 开始），每页 offset += 1。
    结束条件：本页为空 / 不足一页 / 已达 total / 超过 max_pages（防御接口异常导致死循环）。
    """
    cursor, seen = start, 0
    for _ in range(max_pages):
        data = fetch({offset_param: cursor, limit_param: limit})
        items = data.get(items_key) or []
        yield from items
        seen += len(items)
        total = data.get(total_key)
        if not items or len(items) < limit or (isinstance(total, int) and seen >= total):
            return
        cursor += limit if mode == "offset" else 1
