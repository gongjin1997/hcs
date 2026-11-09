"""ManageOne 8.5.1 read-only ECS usage queries; never falls back to HCS IAM."""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx

from app.core.crypto import decrypt
from app.hcs import signer
from app.hcs.diagnostics import network_error, record_attempt, request_id
from app.hcs.errors import HcsError
from app.hcs.ratelimit import acquire_cloud, consume_budget, remaining_time, request_budget
from app.hcs.transport import send_bounded
from app.metrics.ecs_diagnostics import REASONS
from app.services.manageone_config import configured, get_config

if TYPE_CHECKING:
    from app.collectors.base import RunContext
    from app.metrics.ecs_readings import EcsReading

log = logging.getLogger(__name__)

RESOURCE_PATH = "/rest/tenant-resource/v1/tenant/resources/CLOUD_VM"
TOKEN_PATH = "/rest/plat/smapp/v1/sessions"
HISTORY_PATH = "/rest/performance/v1/data-svc/history-data/action/query"
_READ_ONLY = frozenset({("PUT", TOKEN_PATH), ("GET", RESOURCE_PATH), ("POST", HISTORY_PATH)})
OBJECT_TYPE_ID = 562958543355904
INDICATORS = {
    "cpu_util": 562958543421441,
    "mem_util": 562958543486979,
    "disk_util": 562958543618052,
}
RESOURCE_LIMIT = 1000
OBJECTS_PER_QUERY = 100
MAX_RESOURCE_PAGES = 200


@dataclass(frozen=True)
class _Credentials:
    """认证仅作用于显式配置的北向地址，不继承租户 IAM 认证。"""
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    roa_rand: str = field(default="", repr=False)

    def apply(self, request: httpx.Request) -> None:
        if self.access_key:
            headers = {key: value for key, value in request.headers.items()
                       if key in ("host", "content-type")}
            request.headers.update(signer.sign(request.method, str(request.url), headers, request.content,
                                               self.access_key, self.secret_key))
        else:
            request.headers["x-auth-token"] = self.token
            if request.method != "GET":
                request.headers["roarand"] = self.roa_rand


def _record(ctx: RunContext, *, calls: int = 0, errors: int = 0, wait_ms: float = 0) -> None:
    observer = getattr(ctx.client, "_record", None)
    if callable(observer):
        observer("manageone", calls=calls, errors=errors, wait_ms=wait_ms)


class _NoDefaultAuth:
    """Use the shared TLS transport while suppressing any HCS client auth."""

    def __init__(self, http: httpx.Client):
        self.http = http

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return self.http.send(request, auth=None, **kwargs)


def _request(ctx: RunContext, base: str, path: str, method: str, *, body: dict | None = None,
             headers: dict[str, str] | None = None, params: dict[str, int] | None = None,
             credentials: _Credentials | None = None) -> dict:
    method = method.upper()
    if (method, path) not in _READ_ONLY:
        raise HcsError(0, "LOCAL.READ_ONLY", "ManageOne adapter only allows its fixed authentication and query routes")
    http = getattr(ctx.client, "http", None)
    if not isinstance(http, httpx.Client):
        raise HcsError(0, "MANAGEONE.CLIENT", "ManageOne 查询客户端未配置")
    waiting = time.perf_counter()
    wait_ms = 0.0
    started = time.perf_counter()
    lease = None
    response: httpx.Response | None = None
    error: HcsError | None = None
    exception_type: str | None = None
    calls = 0
    try:
        consume_budget()
        lease = acquire_cloud("manageone", 1, 1, min(10, remaining_time(10)))
        wait_ms = (time.perf_counter() - waiting) * 1000
        url = f"{base.rstrip('/')}{path}"
        request = httpx.Request(method, url, params=params, json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8", **(headers or {})})
        if credentials is not None:
            credentials.apply(request)
        calls = 1
        # Do not inherit HcsClient's auth, cookies, default headers, or IAM token.
        response = send_bounded(cast(httpx.Client, _NoDefaultAuth(http)), request,
                                timeout=min(10, remaining_time(10)))
        if response.status_code < 200 or response.status_code >= 300:
            code = "MANAGEONE.AUTH" if path == TOKEN_PATH else "MANAGEONE.HTTP"
            error = HcsError(response.status_code, code, "ManageOne 返回非成功状态")
        else:
            try:
                value = response.json()
            except ValueError:
                error = HcsError(response.status_code, "MANAGEONE.INVALID_RESPONSE", "ManageOne 返回非 JSON 数据")
            else:
                if not isinstance(value, dict):
                    error = HcsError(response.status_code, "MANAGEONE.INVALID_RESPONSE", "ManageOne 返回数据格式无效")
                elif ("status_code" in value and (type(value["status_code"]) is not int
                                                   or value["status_code"] != 200)
                      or "error_code" in value and (type(value["error_code"]) is not int
                                                     or value["error_code"] != 0)):
                    error = HcsError(response.status_code, "MANAGEONE.API", "ManageOne 查询接口返回业务错误")
    except HcsError as exc:
        error = exc
    except httpx.HTTPError as exc:
        error, exception_type = network_error(exc), type(exc).__name__
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        error, exception_type = HcsError(0, "MANAGEONE.INVALID_RESPONSE", "ManageOne 查询失败"), type(exc).__name__
    finally:
        if lease is not None:
            lease.release()
        status = response.status_code if response is not None else 0
        _record(ctx, calls=calls, errors=int(error is not None), wait_ms=wait_ms)
        record_attempt(log, service="manageone", path=path, method=method, tenant_id=ctx.tenant_id,
                       project_ref=ctx.project_ref, status=status, attempt=1,
                       cost_ms=(time.perf_counter() - started) * 1000, wait_ms=wait_ms,
                       upstream_id=request_id(response) if response is not None else None,
                       error=error, exception_type=exception_type)
    if error is not None:
        raise error
    assert response is not None
    return value


def _config(ctx: RunContext) -> tuple[str, dict, str]:
    config = get_config(ctx.tenant)
    if not config["enabled"]:
        raise HcsError(0, "MANAGEONE.DISABLED", "ManageOne 性能接入未开启")
    if not configured(config):
        raise HcsError(0, "MANAGEONE.NOT_CONFIGURED", "ManageOne 北向配置不完整")
    try:
        secret = decrypt(config["sk_cipher"] if config["auth_type"] == "AKSK" else config["secret_cipher"])
    except Exception:
        raise HcsError(0, "MANAGEONE.NOT_CONFIGURED", "ManageOne 北向配置无效") from None
    if not secret:
        raise HcsError(0, "MANAGEONE.NOT_CONFIGURED", "ManageOne 北向凭据未配置")
    return config["endpoint"].rstrip("/"), config, secret


def _token(ctx: RunContext, base: str, username: str, password: str, organization: str) -> tuple[str, str]:
    body: dict[str, str] = {"grantType": "password", "userName": username, "value": password}
    if organization:
        body["organizationName"] = organization
    result = _request(ctx, base, TOKEN_PATH, "PUT", body=body)
    session, roa_rand = result.get("accessSession"), result.get("roaRand")
    if not isinstance(session, str) or not session or not isinstance(roa_rand, str) or not roa_rand:
        raise HcsError(0, "MANAGEONE.INVALID_TOKEN", "ManageOne 北向认证响应缺少会话凭据")
    return session, roa_rand


def _resource_page(ctx: RunContext, base: str, credentials: _Credentials, offset: int) -> dict:
    return _request(ctx, base, RESOURCE_PATH, "GET", params={"limit": RESOURCE_LIMIT, "offset": offset},
                    credentials=credentials)


def _map_resources(ctx: RunContext, base: str, credentials: _Credentials,
                   wanted: set[str]) -> tuple[dict[str, str], set[str], set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    unverified_source: set[str] = set()
    wrong_project: set[str] = set()
    offset = 0
    total: int | None = None
    for _ in range(MAX_RESOURCE_PAGES):
        result = _resource_page(ctx, base, credentials, offset)
        rows = result.get("objList")
        page_total = result.get("totalNum")
        if (not isinstance(rows, list) or isinstance(page_total, bool) or not isinstance(page_total, int)
                or page_total < 0 or len(rows) > RESOURCE_LIMIT):
            raise HcsError(0, "MANAGEONE.INVALID_RESOURCES", "ManageOne ECS 资源分页响应格式无效")
        if total is None:
            total = page_total
        elif total != page_total:
            raise HcsError(0, "MANAGEONE.INCONSISTENT_RESOURCES", "ManageOne ECS 资源分页期间总数变化")
        for row in rows:
            if not isinstance(row, dict):
                continue
            native_id = row.get("nativeId")
            if not isinstance(native_id, str) or native_id not in wanted:
                continue
            if row.get("projectId") != ctx.project_id:
                wrong_project.add(native_id)
                continue
            owner_id, owner_type = row.get("ownerId"), row.get("ownerType")
            if (isinstance(owner_id, bool) or not isinstance(owner_id, (str, int)) or not str(owner_id)
                    or owner_type is not None and not isinstance(owner_type, str)):
                unverified_source.add(native_id)
                continue
            res_id = row.get("resId")
            if isinstance(res_id, (int, str)) and not isinstance(res_id, bool) and str(res_id):
                found[native_id].add(str(res_id))
        if offset + len(rows) >= total:
            mapping = {native_id: next(iter(object_ids)) for native_id, object_ids in found.items()
                       if len(object_ids) == 1 and native_id not in unverified_source and native_id not in wrong_project}
            ambiguous = {native_id for native_id, object_ids in found.items() if len(object_ids) != 1}
            return mapping, unverified_source, ambiguous | wrong_project
        if not rows:
            raise HcsError(0, "MANAGEONE.INCOMPLETE_RESOURCES", "ManageOne ECS 资源分页返回不完整")
        offset += RESOURCE_LIMIT
    raise HcsError(0, "MANAGEONE.RESOURCE_BUDGET", "ManageOne ECS 资源分页超过预算")


def _sample_points(result: dict, object_id: str, indicator_id: int, start: datetime, end: datetime
                   ) -> tuple[list[tuple[datetime, float]], int, str | None]:
    data = result.get("data")
    if not isinstance(data, dict):
        return [], 0, "invalid_response"
    objects = data.get(object_id)
    if objects is None:
        return [], 0, "no_data"
    if not isinstance(objects, dict):
        return [], 0, "invalid_response"
    metrics = objects.get(str(indicator_id))
    if metrics is None:
        return [], 0, "no_data"
    if not isinstance(metrics, dict):
        return [], 0, "invalid_response"
    series = metrics.get("series")
    if not isinstance(series, list):
        return [], 0, "invalid_response" if series is not None else "no_data"
    if len(series) > 10000:
        return [], 0, "invalid_response"
    if not series:
        return [], 0, "no_data"
    points: dict[datetime, float] = {}
    rejected = 0
    for sample in series:
        if not isinstance(sample, dict) or len(sample) != 1:
            rejected += 1
            continue
        raw_at, raw_value = next(iter(sample.items()))
        if not isinstance(raw_at, str) or not raw_at.isascii() or not raw_at.isdigit() or len(raw_at) != 13:
            rejected += 1
            continue
        try:
            # 正式历史响应示例使用13位 epoch 毫秒时间戳。
            at = datetime.fromtimestamp(int(raw_at) / 1000, UTC)
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError, OSError):
            rejected += 1
            continue
        if (not math.isfinite(value) or not 0 <= value <= 100 or not start <= at <= end):
            rejected += 1
            continue
        points[at] = value
    return sorted(points.items()), rejected, None if points else "invalid_sample"


def collect_manageone(ctx: RunContext, ids: list[str], aliases: list[str], diagnostic: dict,
                      out: dict[str, list[EcsReading]]) -> None:
    """Read actual ManageOne samples only after source, identity and project checks."""
    for alias in aliases:
        if alias not in INDICATORS:
            diagnostic["metrics"].setdefault(alias, {}).update(
                status="mapping_missing", reason="manageone_metric_unsupported", unsupported_metric=True)
    wanted_aliases = [alias for alias in aliases if alias in INDICATORS]
    if not ids or not wanted_aliases:
        return
    if str(getattr(ctx.tenant, "metrics_source", "") or "").upper() != "MANAGEONE":
        for alias in wanted_aliases:
            diagnostic["metrics"].setdefault(alias, {}).update(status="no_source", reason="manageone_not_selected")
        return
    if (not isinstance(ctx.project_id, str) or not ctx.project_id
            or getattr(ctx.client, "project_id", ctx.project_id) != ctx.project_id
            or getattr(ctx.client, "tenant_id", ctx.tenant_id) != ctx.tenant_id
            or getattr(ctx.client, "project_ref", ctx.project_ref) != ctx.project_ref):
        for alias in wanted_aliases:
            diagnostic["metrics"].setdefault(alias, {}).update(status="error", reason="scope_mismatch",
                                                                 errors=1)
        return
    for alias in wanted_aliases:
        diagnostic["metrics"].setdefault(alias, {}).setdefault("timestamp_semantics", "source_sample")
    try:
        base, config, secret = _config(ctx)
        with request_budget(attempts=384, seconds=60):
            if config["auth_type"] == "AKSK":
                credentials = _Credentials(access_key=config["access_key"], secret_key=secret)
            else:
                token, roa_rand = _token(ctx, base, config["username"], secret, config["organization"] or "")
                credentials = _Credentials(token=token, roa_rand=roa_rand)
            resource_map, source_unverified, wrong_project = _map_resources(ctx, base, credentials, set(ids))
        missing = [sid for sid in ids if sid not in resource_map]
        if missing:
            for alias in wanted_aliases:
                diagnostic["metrics"][alias].update(status="no_series", reason="no_manageone_mapping")
            message = "ManageOne ECS 资源映射未覆盖全部选定实例，未猜测跨项目或缺失对象"
            if message not in ctx.errors:
                ctx.errors.append(message)
        if source_unverified:
            for alias in wanted_aliases:
                diagnostic["metrics"][alias].update(status="mapping_missing",
                                                       reason="manageone_source_unverified")
            message = "ManageOne ECS 资源来源元数据不完整，未使用未核实映射"
            if message not in ctx.errors:
                ctx.errors.append(message)
        if wrong_project:
            for alias in wanted_aliases:
                diagnostic["metrics"][alias].update(status="error", reason="scope_mismatch",
                                                       errors=diagnostic["metrics"][alias].get("errors", 0) + 1)
            message = "ManageOne ECS 资源映射返回其他项目或重复对象，未跨项目关联"
            if message not in ctx.errors:
                ctx.errors.append(message)
        if not resource_map:
            return
        now = datetime.now(UTC)
        start, end = now - timedelta(minutes=5), now
        metric_ids = [INDICATORS[alias] for alias in wanted_aliases]
        by_object = list(resource_map.items())
        batch_size = OBJECTS_PER_QUERY // len(metric_ids)
        for offset in range(0, len(by_object), batch_size):
            batch = by_object[offset:offset + OBJECTS_PER_QUERY // len(metric_ids)]
            body = {"obj_type_id": OBJECT_TYPE_ID, "indicator_ids": metric_ids,
                    "obj_ids": [res_id for _native_id, res_id in batch], "interval": "MINUTE",
                    "range": "LAST_5_MINUTE"}
            try:
                with request_budget(attempts=384, seconds=60):
                    result = _request(ctx, base, HISTORY_PATH, "POST", body=body,
                                      credentials=credentials)
            except HcsError:
                for alias in wanted_aliases:
                    entry = diagnostic["metrics"][alias]
                    entry.update(status="error", reason="query_failed", errors=entry.get("errors", 0) + 1)
                message = "ECS ManageOne 指标查询失败：" + REASONS["query_failed"]
                if message not in ctx.errors:
                    ctx.errors.append(message)
                continue
            for sid, object_id in batch:
                for alias in wanted_aliases:
                    entry = diagnostic["metrics"][alias]
                    entry["series_count"] += 1
                    points, rejected, point_reason = _sample_points(result, object_id, INDICATORS[alias], start, end)
                    entry["rejected_points"] = entry.get("rejected_points", 0) + rejected
                    if not points:
                        if point_reason == "invalid_response":
                            entry.update(status="error", reason="query_failed",
                                         errors=entry.get("errors", 0) + 1)
                        elif entry.get("reason") is None:
                            entry.update(status="no_data", reason="no_data")
                        continue
                    at, value = points[-1]
                    from app.metrics.ecs_readings import EcsReading

                    dimensions = {"semantics": "cloud_volume"} if alias == "disk_util" else {}
                    out.setdefault(sid, []).append(EcsReading(alias, value, at, "MANAGEONE", unit="%",
                        dimensions=dimensions,
                        period_sec=60))
    except HcsError as exc:
        reason = ("manageone_disabled" if exc.code == "MANAGEONE.DISABLED" else
                  "manageone_not_configured" if exc.code == "MANAGEONE.NOT_CONFIGURED" else "query_failed")
        for alias in wanted_aliases:
            entry = diagnostic["metrics"][alias]
            entry.update(status="error", reason=reason, errors=entry.get("errors", 0) + 1)
        safe_reason = REASONS.get(reason, REASONS["query_failed"])
        message = "ECS ManageOne 指标查询失败：" + safe_reason
        if message not in ctx.errors:
            ctx.errors.append(message)
