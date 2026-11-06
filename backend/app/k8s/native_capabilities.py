"""Optional, read-only native enrichment. No settings, database, AOM or shared cache.

Public entry points accept a request-scoped KubeApi and require ``enabled=True``.
Results are dictionaries with fixed ``status``/``reason`` codes, never upstream
messages. ``items`` retain independent valid evidence on partial failures.

Ownership returns one row per supplied Pod, with ``pod``, ``direct_owner`` and
``top_owner`` identities (kind/api_version/namespace/name/uid). An intermediate
ReplicaSet/Job is matched by namespace, kind, name AND UID before its controlling
reference is used. A top reference proves ownership, not the owner's liveness.

Metrics discovery negotiates only implemented schemas (v1/v1beta1) advertised by
the server. Lists use KubeApi._list's bounded, consistent-snapshot pagination.
``complete`` refers to transport completion, NOT coverage of all live objects.
Absent metrics do not prove zero usage. CPU is Decimal cores; memory is Decimal
working-set bytes (not percentages). Invalid/stale temporal evidence makes both
values None; invalid individual quantities stay None without losing valid peers.
Results are JSON ready (finite numeric values, UTC ISO timestamps and seconds).
Arithmetic is Decimal internally; parse_quantity itself returns Decimal or None.
Callers own tenant settings, scheduling, persistence and census comparisons.
"""

import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from app.hcs import HcsError

from .kube import SERVICE, KubeApi

Status = Literal["ok", "disabled", "unsupported", "forbidden", "error", "malformed", "partial"]
_log = logging.getLogger(__name__)
_GROUP = "metrics.k8s.io"
_VERSIONS = ("v1", "v1beta1")
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z", re.ASCII)
_NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_API_VERSION = re.compile(r"(?:[a-z0-9.-]+/)?v[0-9]+(?:(?:alpha|beta)[0-9]+)?\Z", re.ASCII)
_QUANTITY = re.compile(r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
                       r"(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]|[eE][+-]?[0-9]+)?\Z", re.ASCII)
_DECIMAL_POWERS = {"n": -9, "u": -6, "m": -3, "": 0, "k": 3, "M": 6,
                   "G": 9, "T": 12, "P": 15, "E": 18}
_BINARY_POWERS = {f"{s}i": 10 * i for i, s in enumerate("KMGTPE", 1)}
_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)", re.ASCII)
_DURATION_SCALE = {"ns": Decimal("1e-9"), "us": Decimal("1e-6"), "µs": Decimal("1e-6"),
                   "μs": Decimal("1e-6"), "ms": Decimal("1e-3"), "s": Decimal(1),
                   "m": Decimal(60), "h": Decimal(3600)}
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z", re.ASCII)
_OWNER_GROUPS = {"ReplicaSet": "apps", "Deployment": "apps", "Job": "batch", "CronJob": "batch"}
_OWNER_LISTS = {"ReplicaSet": "/apis/apps/v1/replicasets", "Job": "/apis/batch/v1/jobs"}


def _result(status: Status, reason: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **fields}


def _report(feature: str, result: dict[str, Any]) -> dict[str, Any]:
    # Never log objects, names, quantity strings, exception text or discovery data.
    _log.info("cce_native_capability", extra={"feature": feature, "status": result["status"],
                                            "reason": result["reason"]})
    return _json_ready(result)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HcsError):
        if exc.code == "LOCAL.INCOMPLETE_PAGE":
            return _result("partial", "incomplete_list")
        if exc.code == "LOCAL.INVALID_RESPONSE":
            return _result("malformed", "invalid_response")
        if exc.status == 403:
            return _result("forbidden", "rbac_forbidden")
        if exc.status in (404, 405, 501):
            return _result("unsupported", "api_unsupported")
        if exc.status == 401:
            return _result("error", "authentication_failed")
    return _result("error", "request_failed")


def _get(kube: KubeApi, path: str) -> dict:
    return kube.client.get(SERVICE, path, host_vars={"cluster_id": kube.cluster_id})


def _list(kube: KubeApi, path: str) -> dict[str, Any]:
    try:
        items = kube._list(path)
    except Exception as exc:
        return {**_failure(exc), "items": [], "complete": False}
    return _result("ok", "list_complete", items=items, complete=True)


def _name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and all(value.split("."))


def _namespace(value: object) -> bool:
    return isinstance(value, str) and _NAMESPACE.fullmatch(value) is not None


def _uid(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and not any(c.isspace() or ord(c) < 32 for c in value)


def _identity(obj: object, *, kind: str | None = None, require_uid: bool = True) -> dict[str, Any] | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("metadata"), dict):
        return None
    meta = obj["metadata"]
    if not _name(meta.get("name")) or not _namespace(meta.get("namespace")):
        return None
    if require_uid and not _uid(meta.get("uid")):
        return None
    if kind is not None and obj.get("kind", kind) != kind:
        return None
    return {"kind": kind or obj.get("kind"), "api_version": obj.get("apiVersion"),
            "namespace": meta["namespace"], "name": meta["name"], "uid": meta.get("uid")}


def _controller(obj: dict, namespace: str) -> tuple[dict[str, Any] | None, str | None]:
    references = obj.get("metadata", {}).get("ownerReferences", [])
    if not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references):
        return None, "owner_references_invalid"
    if any("controller" in ref and type(ref["controller"]) is not bool for ref in references):
        return None, "owner_references_invalid"
    controllers = [ref for ref in references if ref.get("controller") is True]
    if not controllers:
        return None, None
    if len(controllers) != 1:
        return None, "multiple_controllers"
    ref = controllers[0]
    kind, version = ref.get("kind"), ref.get("apiVersion")
    if (not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", kind)
            or not _name(ref.get("name")) or not _uid(ref.get("uid"))
            or not isinstance(version, str) or not _API_VERSION.fullmatch(version)
            or "namespace" in ref and ref["namespace"] != namespace):
        return None, "owner_reference_invalid"
    if kind in _OWNER_GROUPS and version.split("/")[0] != _OWNER_GROUPS[kind]:
        return None, "owner_api_group_invalid"
    if ref["uid"] == obj["metadata"].get("uid"):
        return None, "owner_cycle"
    return {"kind": kind, "api_version": version, "namespace": namespace,
            "name": ref["name"], "uid": ref["uid"]}, None


def resolve_pod_owners(kube: KubeApi, pods: list[dict], *, enabled: bool = False) -> dict[str, Any]:
    """Return per-Pod direct/top ownership plus independent ReplicaSet/Job sources.

    Calls only the necessary all-namespace ReplicaSet/Job GET lists, once per kind
    per invocation; no cross-cluster/tenant cache. Never mutates supplied Pods.
    An unavailable intermediate leaves direct_owner intact and top_owner None.
    """
    rows, needed = [], set()
    for pod in pods:
        identity = _identity(pod, kind="Pod")
        row = _result("ok", "pod_unowned", pod=identity, direct_owner=None, top_owner=identity)
        if identity is None:
            row.update(status="malformed", reason="pod_identity_invalid", top_owner=None)
        else:
            owner, reason = _controller(pod, identity["namespace"])
            row.update(direct_owner=owner, top_owner=owner or identity)
            if reason:
                row.update(status="malformed", reason=reason, top_owner=None)
            elif owner:
                row["reason"] = "direct_owner"
                if owner["kind"] in _OWNER_LISTS:
                    row["top_owner"] = None
                    needed.add(owner["kind"])
        rows.append(row)
    if enabled is not True:
        return _report("owners", _result("disabled", "disabled", items=rows, sources={}))
    sources, indexes = {}, {}
    for kind in sorted(needed):
        source = _list(kube, _OWNER_LISTS[kind])
        index: dict[tuple[str, str], list[dict]] = {}
        invalid = 0
        for obj in source.pop("items"):
            identity = _identity(obj, kind=kind)
            if identity is None or not isinstance(obj.get("apiVersion"), str) \
                    or not _API_VERSION.fullmatch(obj["apiVersion"]) \
                    or obj["apiVersion"].split("/")[0] != _OWNER_GROUPS[kind]:
                invalid += 1
                continue
            index.setdefault((identity["namespace"], identity["name"]), []).append(obj)
        if invalid and source["status"] == "ok":
            source.update(status="partial", reason="owner_list_invalid_items")
        sources[kind], indexes[kind] = source, index
    for row in rows:
        owner = row["direct_owner"]
        if row["status"] != "ok" or not owner or owner["kind"] not in sources:
            continue
        source = sources[owner["kind"]]
        if not source["complete"]:
            row.update(status=source["status"], reason=source["reason"])
            continue
        candidates = indexes[owner["kind"]].get((owner["namespace"], owner["name"]), [])
        if len(candidates) != 1:
            row.update(status="partial", reason="owner_not_found" if not candidates else "owner_ambiguous")
            continue
        obj = candidates[0]
        if obj["metadata"]["uid"] != owner["uid"]:
            row.update(status="partial", reason="owner_uid_mismatch")
            continue
        parent, reason = _controller(obj, owner["namespace"])
        if reason:
            row.update(status="malformed", reason=reason)
            continue
        expected = "Deployment" if owner["kind"] == "ReplicaSet" else "CronJob"
        if parent and parent["kind"] != expected:
            row.update(status="partial", reason="owner_chain_unsupported")
        elif parent and parent["uid"] in (owner["uid"], row["pod"]["uid"]):
            row.update(status="malformed", reason="owner_cycle")
        else:
            row.update(reason="owner_chain_verified" if parent else "owner_standalone", top_owner=parent or owner)
    issues = any(row["status"] != "ok" for row in rows) or any(s["status"] != "ok" for s in sources.values())
    return _report("owners", _result("partial" if issues else "ok", "owner_enrichment_partial" if issues
                                     else "owners_resolved", items=rows, sources=sources))


def parse_quantity(value: object) -> Decimal | None:
    """Parse bounded nonnegative Kubernetes usage quantities exactly, without floats.

    Supports DecimalSI (n/u/m/k/M/G/T/P/E), BinarySI and decimal exponents.
    This observation parser deliberately rejects quantities above signed int64
    magnitude or below nano precision instead of Kubernetes' rounding/clamping.
    Rejecting out-of-budget/negative/non-string input preserves unknown evidence.
    Fractional bytes are retained; only explicit valid zero quantities yield zero.
    """
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = _QUANTITY.fullmatch(value)
    if match is None:
        return None
    number, suffix = match.group(1), match.group(2) or ""
    with localcontext() as context:
        context.prec = 256
        try:
            if suffix in _BINARY_POWERS:
                result = Decimal(number) * (Decimal(2) ** _BINARY_POWERS[suffix])
            else:
                power = _DECIMAL_POWERS.get(suffix)
                if power is None:
                    if len(suffix) > 5:
                        return None
                    power = int(suffix[1:])
                if abs(power) > 63:
                    return None
                result = Decimal(number) * (Decimal(10) ** power)
            if result > 2 ** 63 - 1 or result != result.quantize(Decimal("1e-9")):
                return None
            return result
        except (InvalidOperation, ValueError, OverflowError):
            return None


def _discovery_resources(data: object, version: str) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("kind") != "APIResourceList" \
            or data.get("groupVersion") != f"{_GROUP}/{version}" or not isinstance(data.get("resources"), list):
        return None
    found = {}
    for resource in data["resources"]:
        if not isinstance(resource, dict) or not isinstance(resource.get("name"), str) \
                or not isinstance(resource.get("verbs"), list) \
                or any(not isinstance(verb, str) for verb in resource["verbs"]):
            return None
        name = resource["name"]
        if name not in ("pods", "nodes"):
            continue
        if name in found or resource.get("kind") != {"pods": "PodMetrics", "nodes": "NodeMetrics"}[name] \
                or type(resource.get("namespaced")) is not bool or resource["namespaced"] != (name == "pods"):
            return None
        found[name] = _result("ok", "list_supported") if "list" in resource["verbs"] else \
            _result("unsupported", "list_unsupported")
    return {name: found.get(name, _result("unsupported", "resource_unsupported")) for name in ("pods", "nodes")}


def discover_resource_metrics(kube: KubeApi, *, enabled: bool = False) -> dict[str, Any]:
    """GET group/version discovery; no permission reviews (POST) or guessed paths.

    Returns version plus pods/nodes capability results. Discovery is not proof of
    RBAC: actual lists may independently return forbidden/error/partial.
    """
    if enabled is not True:
        return _report("metrics_discovery", _result("disabled", "disabled", version=None, resources={}))
    try:
        group = _get(kube, f"/apis/{_GROUP}")
        if not isinstance(group, dict) or group.get("kind") != "APIGroup" or group.get("name") != _GROUP \
                or not isinstance(group.get("versions"), list) or not 1 <= len(group["versions"]) <= 20:
            return _report("metrics_discovery", _result("malformed", "group_discovery_invalid",
                                                        version=None, resources={}))
        versions = []
        for item in group["versions"]:
            if not isinstance(item, dict) or not isinstance(item.get("version"), str) \
                    or len(item["version"]) > 32 or not re.fullmatch(r"v[0-9]+(?:(?:alpha|beta)[0-9]+)?", item["version"]) \
                    or item.get("groupVersion") != f"{_GROUP}/{item['version']}" or item["version"] in versions:
                return _report("metrics_discovery", _result("malformed", "group_discovery_invalid",
                                                            version=None, resources={}))
            versions.append(item["version"])
        preferred = group.get("preferredVersion")
        if preferred is not None and (not isinstance(preferred, dict) or preferred.get("version") not in versions
                or preferred.get("groupVersion") != f"{_GROUP}/{preferred.get('version')}"):
            return _report("metrics_discovery", _result("malformed", "preferred_version_invalid",
                                                        version=None, resources={}))
        supported = [version for version in _VERSIONS if version in versions]
        if not supported:
            return _report("metrics_discovery", _result("unsupported", "metrics_version_unsupported",
                                                        version=None, resources={}))
        version = preferred["version"] if preferred and preferred["version"] in supported else supported[0]
        resources = _discovery_resources(_get(kube, f"/apis/{_GROUP}/{version}"), version)
        if resources is None:
            return _report("metrics_discovery", _result("malformed", "resource_discovery_invalid",
                                                        version=version, resources={}))
        available = sum(result["status"] == "ok" for result in resources.values())
        status: Status = "ok" if available == 2 else "partial" if available else "unsupported"
        return _report("metrics_discovery", _result(status, "metrics_discovered" if available else "resources_unsupported",
                                                    version=version, resources=resources))
    except Exception as exc:
        return _report("metrics_discovery", {**_failure(exc), "version": None, "resources": {}})


def _duration(value: object) -> Decimal | None:
    if not isinstance(value, str) or not 0 < len(value) <= 96:
        return None
    value = value.removeprefix("+")
    end, total = 0, Decimal(0)
    with localcontext() as context:
        context.prec = 256
        for part in _DURATION_PART.finditer(value):
            if part.start() != end:
                return None
            total += Decimal(part.group(1)) * _DURATION_SCALE[part.group(2)]
            end = part.end()
    return total if end == len(value) and total > 0 and total <= Decimal(2 ** 63 - 1) / Decimal(10 ** 9) \
        and total == total.quantize(Decimal("1e-9")) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86400 + value.seconds) + Decimal(value.microseconds) / Decimal(10 ** 6)


def _timing(obj: dict, now: datetime, max_age: timedelta, future_tolerance: timedelta,
            max_window: timedelta) -> dict[str, Any]:
    timestamp, window = _timestamp(obj.get("timestamp")), _duration(obj.get("window"))
    result = {"timestamp": timestamp.isoformat() if timestamp else None, "window_seconds": window,
              "fresh": False, "age_seconds": _seconds(now - timestamp) if timestamp else None}
    if timestamp is None:
        reason = "timestamp_invalid"
    elif window is None:
        reason = "window_invalid"
    elif window > _seconds(max_window):
        reason = "window_excessive"
    elif timestamp - now > future_tolerance:
        reason = "timestamp_future"
    elif now - timestamp > max_age:
        reason = "sample_stale"
    else:
        return {**_result("ok", "sample_fresh"), **result, "fresh": True}
    return {**_result("partial", reason), **result}


def _usage(value: object) -> dict[str, Any]:
    usage = value if isinstance(value, dict) else {}
    cpu, memory = parse_quantity(usage.get("cpu")), parse_quantity(usage.get("memory"))
    return _result("ok" if cpu is not None and memory is not None else "partial", "usage_valid"
                   if cpu is not None and memory is not None else "quantity_invalid", cpu_cores=cpu, memory_bytes=memory)


def _metric_identity(obj: dict, kind: str, namespace: str | None) -> dict[str, Any] | None:
    meta = obj.get("metadata")
    if not isinstance(meta, dict) or not _name(meta.get("name")):
        return None
    ns = meta.get("namespace")
    if kind == "pods" and (not _namespace(ns) or namespace is not None and ns != namespace) \
            or kind == "nodes" and ns not in (None, "") or meta.get("uid") is not None and not _uid(meta["uid"]):
        return None
    return {"name": meta["name"], "namespace": ns if kind == "pods" else None, "uid": meta.get("uid")}


def _normalize_metrics(items: list[dict], kind: str, version: str, namespace: str | None,
                       now: datetime, max_age: timedelta, future_tolerance: timedelta,
                       max_window: timedelta) -> list[dict[str, Any]]:
    identities = [_metric_identity(obj, kind, namespace) for obj in items]
    keys = Counter((identity["namespace"], identity["name"]) for identity in identities if identity)
    rows = []
    for obj, identity in zip(items, identities, strict=True):
        row = _result("malformed", "metric_identity_invalid", identity=identity, cpu_cores=None, memory_bytes=None,
                      timestamp=None, window_seconds=None, age_seconds=None, fresh=False, containers=[])
        expected_kind = "PodMetrics" if kind == "pods" else "NodeMetrics"
        if identity is None:
            rows.append(row)
            continue
        if obj.get("kind", expected_kind) != expected_kind \
                or obj.get("apiVersion", f"{_GROUP}/{version}") != f"{_GROUP}/{version}":
            row["reason"] = "metric_type_invalid"
            rows.append(row)
            continue
        if keys[identity["namespace"], identity["name"]] != 1:
            row["reason"] = "metric_identity_duplicate"
            rows.append(row)
            continue
        timing = _timing(obj, now, max_age, future_tolerance, max_window)
        row.update(timing)
        if not timing["fresh"]:
            rows.append(row)
            continue
        if kind == "nodes":
            row.update(_usage(obj.get("usage")))
        else:
            containers = obj.get("containers")
            if not isinstance(containers, list) or not containers \
                    or any(not isinstance(c, dict) or not _name(c.get("name")) for c in containers):
                row.update(status="malformed", reason="containers_invalid")
            elif len({c["name"] for c in containers}) != len(containers):
                row.update(status="malformed", reason="containers_duplicate")
            else:
                row["containers"] = [{"name": c["name"], **_usage(c.get("usage"))} for c in containers]
                for field in ("cpu_cores", "memory_bytes"):
                    values = [c[field] for c in row["containers"]]
                    # Explicit nonempty completeness guard; never sum a subset or [] into zero.
                    with localcontext() as context:
                        context.prec = 256
                        row[field] = sum(values, Decimal(0)) if all(v is not None for v in values) else None
                partial = any(c["status"] != "ok" for c in row["containers"])
                row.update(status="partial" if partial else "ok", reason="quantity_invalid" if partial else "usage_valid")
        rows.append(row)
    return rows


def collect_resource_metrics(kube: KubeApi, *, enabled: bool = False, namespace: str | None = None,
                             now: datetime | None = None, max_age: timedelta = timedelta(minutes=5),
                             future_tolerance: timedelta = timedelta(seconds=20),
                             max_window: timedelta = timedelta(minutes=5)) -> dict[str, Any]:
    """Discover then independently list Pod/Node metrics; opt-in defaults to off.

    Returns ``discovery``, ``pods`` and ``nodes`` results, each status/reason coded.
    A namespace restricts only Pod metrics; Nodes are always cluster scoped.
    No failure of one optional source aborts the other or raises task errors.
    Invalid caller time limits raise ValueError before any request (programming
    errors, not upstream errors); now must be timezone aware.
    """
    if enabled is not True:
        disabled = _result("disabled", "disabled", items=[], complete=False)
        return _report("metrics", _result("disabled", "disabled", discovery=None, pods=dict(disabled), nodes=dict(disabled)))
    if namespace is not None and not _namespace(namespace):
        invalid = _result("malformed", "namespace_invalid", items=[], complete=False)
        return _report("metrics", _result("malformed", "namespace_invalid", discovery=None,
                                          pods=dict(invalid), nodes=dict(invalid)))
    now = now if now is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None \
            or any(not isinstance(limit, timedelta) for limit in (max_age, future_tolerance, max_window)) \
            or max_age <= timedelta(0) or future_tolerance < timedelta(0) or max_window <= timedelta(0):
        raise ValueError("native_metrics_time_policy_invalid")
    now = now.astimezone(UTC)
    discovery = discover_resource_metrics(kube, enabled=True)
    sources = {}
    for kind in ("pods", "nodes"):
        capability = discovery["resources"].get(kind, discovery)
        if capability["status"] != "ok":
            sources[kind] = _result(capability["status"], capability["reason"], items=[], complete=False)
            continue
        scope = f"namespaces/{namespace}/" if namespace and kind == "pods" else ""
        source = _list(kube, f"/apis/{_GROUP}/{discovery['version']}/{scope}{kind}")
        if source["complete"]:
            source["items"] = _normalize_metrics(source["items"], kind, discovery["version"], namespace,
                                                  now, max_age, future_tolerance, max_window)
            if source["items"] and all(row["status"] == "malformed" for row in source["items"]):
                source.update(status="malformed", reason="metrics_samples_malformed")
            elif any(row["status"] != "ok" for row in source["items"]):
                source.update(status="partial", reason="metrics_samples_partial")
            else:
                source["reason"] = "metrics_samples_available" if source["items"] else "metrics_list_empty"
        sources[kind] = source
    statuses = [source["status"] for source in sources.values()]
    status: Status = "ok" if statuses == ["ok", "ok"] else statuses[0] if len(set(statuses)) == 1 else "partial"
    return _report("metrics", _result(status, "metrics_collected" if status == "ok" else "metrics_unavailable"
                                      if status != "partial" else "metrics_partial", discovery=discovery, **sources))
