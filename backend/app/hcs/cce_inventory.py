"""HCS 8.5.1 CCE 管理面只读字段；容量不是使用率，检查时间不由本地时间替代。"""
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

from app.core.logging import sanitize_text
from app.hcs import HcsClient, HcsError
from app.hcs.cce_api import management_items

CLUSTERS_PATH = "/api/v3/projects/{project_id}/clusters"
NODE_STATUS_TTL = 300


def text(value: object, limit: int = 512) -> str | None:
    return sanitize_text(value)[:limit] if isinstance(value, str) and value.strip() else None


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except ValueError:
        return None


def freshness(value: object, now: datetime, ttl: int = NODE_STATUS_TTL) -> str:
    at = timestamp(value)
    if at is None or at > now:
        return "unknown"
    return "fresh" if (now - at).total_seconds() <= ttl else "stale"


def count(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        value = int(value)
    return value if type(value) is int and 0 <= value <= 10**12 else None


def objects(data: dict, kind: str) -> list[dict]:
    rows = management_items(data)
    seen: set[str] = set()
    for row in rows:
        md = row.get("metadata")
        if (not isinstance(md, dict) or not isinstance(md.get("uid"), str) or not md["uid"].strip()
                or len(md["uid"]) > 255 or md["uid"] in seen):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", f"CCE {kind}标识缺失或重复，未使用不完整清单")
        seen.add(md["uid"])
        for section in ("spec", "status"):
            if row.get(section) is not None and not isinstance(row[section], dict):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", f"CCE {kind}字段结构不正确")
    return rows


def clusters(client: HcsClient, *, detail: bool = True) -> list[dict]:
    return objects(client.get("cce", CLUSTERS_PATH, **({"params": {"detail": "true"}} if detail else {})), "集群")


def nodes(client: HcsClient, cluster_id: str) -> list[dict]:
    return objects(client.get("cce", f"{CLUSTERS_PATH}/{quote(cluster_id, safe='')}/nodes"), "节点")


def nodepools(client: HcsClient, cluster_id: str) -> list[dict]:
    return objects(client.get("cce", f"{CLUSTERS_PATH}/{quote(cluster_id, safe='')}/nodepools",
                              params={"showDefaultNodePool": "true"}), "节点池")


def cluster_info(item: dict) -> dict:
    md, spec, status = item["metadata"], item.get("spec") or {}, item.get("status") or {}
    annotation = md.get("annotations")
    annotation = annotation if isinstance(annotation, dict) else {}
    endpoints = []
    for endpoint in status.get("endpoints") or []:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("url"), str):
            continue
        try:
            parsed = urlsplit(endpoint["url"])
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                continue
            endpoints.append({"type": text(endpoint.get("type")), "scheme": parsed.scheme,
                              "host": parsed.hostname, "port": parsed.port,
                              "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))})
        except ValueError:
            continue
    addons = annotation.get("installedAddonInstances")
    if isinstance(addons, str) and len(addons) <= 65536:
        try:
            addons = json.loads(addons)
        except ValueError:
            addons = None
    return {"cluster_id": md["uid"], "name": text(md.get("name"), 255) or md["uid"],
            "version": text(spec.get("version")), "flavor": text(spec.get("flavor")),
            "cluster_type": text(spec.get("type")), "phase": text(status.get("phase")),
            "reason": text(status.get("reason")), "message": text(status.get("message"), 1024),
            "job_id": text(status.get("jobID")), "endpoints": endpoints,
            "capacity": {"total_nodes": count(annotation.get("totalNodesNumber")),
                         "active_nodes": count(annotation.get("activeNodesNumber")),
                         "total_cpu_raw": text(str(annotation["totalNodesCPU"]))
                         if isinstance(annotation.get("totalNodesCPU"), str | int | float)
                         and not isinstance(annotation.get("totalNodesCPU"), bool) else None,
                         "total_memory_raw": text(str(annotation["totalNodesMemory"]))
                         if isinstance(annotation.get("totalNodesMemory"), str | int | float)
                         and not isinstance(annotation.get("totalNodesMemory"), bool) else None,
                         "memory_unit": "unverified"},
            "addons": [{"name": text(a.get("addonTemplateName")), "version": text(a.get("version")),
                        "status": text(a.get("status"))} for a in addons if isinstance(a, dict)]
            if isinstance(addons, list) else None}


def node_info(item: dict, now: datetime) -> dict:
    md, spec, status = item["metadata"], item.get("spec") or {}, item.get("status") or {}
    phase, at = text(status.get("phase")), timestamp(status.get("lastProbeTime"))
    state = freshness(status.get("lastProbeTime"), now)
    health = ("healthy" if phase == "Active" else "abnormal" if phase in ("Abnormal", "Error") else "transition"
              if phase in ("Build", "Installing", "Upgrading", "Deleting") else "unknown") if state == "fresh" else "unknown"
    return {"id": md["uid"], "name": text(md.get("name"), 255) or md["uid"], "phase": phase,
            "last_probe_at": at.isoformat() if at else None, "freshness": state, "health": health,
            "server_id": text(status.get("serverId"), 255), "private_ip": text(status.get("privateIP")),
            "flavor": text(spec.get("flavor")), "node_pool_id": text(spec.get("nodePoolID"))}


def pool_info(item: dict) -> dict:
    md, spec, status = item["metadata"], item.get("spec") or {}, item.get("status") or {}
    conditions = status.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []
    return {"id": md["uid"], "name": text(md.get("name"), 255) or md["uid"],
            "type": text(spec.get("type")), "current_nodes": count(status.get("currentNode")),
            "creating_nodes": count(status.get("creatingNode")), "deleting_nodes": count(status.get("deletingNode")),
            "synced_nodes": count(status.get("configurationSyncedNodeCount")),
            "initial_nodes": count(spec.get("initialNodeCount")),
            "conditions": [{"type": text(c.get("type")),
                            "status": c.get("status") if c.get("status") in ("True", "False") else "Unknown",
                            "reason": text(c.get("reason")), "message": text(c.get("message")),
                            "last_probe_at": text(c.get("lastProbeTime"))}
                           for c in conditions if isinstance(c, dict)]}
