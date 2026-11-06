"""CCE 管理面 v3 全量列表；与 Kubernetes 原生 continue 分页分开。"""
from .errors import HcsError
from .pager import response_items


def management_items(data: dict) -> list[dict]:
    metadata = data.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict) or metadata.get("continue") not in (None, "")):
        raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "CCE 控制面返回未核实的分页清单，未使用不完整列表")
    return response_items(data, "items")


def available_cluster(items: list[dict]) -> dict | None:
    for item in items:
        metadata, status = item.get("metadata"), item.get("status")
        if (isinstance(metadata, dict) and isinstance(metadata.get("uid"), str) and metadata["uid"].strip()
                and isinstance(status, dict) and status.get("phase") == "Available"):
            return item
    return None
