"""GaussDB 原生磁盘、慢 SQL、会话及已完成巡检报告，只提供查询。

所有资源先做租户/项目范围校验再使用短时缓存。POST 仅用于文档明示的查询接口；
不执行 SQL，不结束会话，不扩容磁盘，不用 AOM/CES 或当前快照补历史指标。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import Scope
from app.core.response import BizError, ErrorCode, not_found
from app.hcs import HcsClient, HcsError
from app.hcs.factory import client_for
from app.hcs.gaussdb_native import identifier, instance_path, volume_usage
from app.hcs.query_cache import QueryCache
from app.hcs.ratelimit import request_budget
from app.metrics.dcs_samples import finite_number
from app.models import HcsAccount, HcsProject, Resource
from app.models.base import utcnow
from app.schemas.gaussdb import (
    GaussComponent,
    GaussDisk,
    GaussInstance,
    GaussInstancesOut,
    GaussNativeRows,
    GaussOverviewOut,
    GaussProjectOption,
)
from app.services import settings_service
from app.services.gaussdb_reports import normalize_metric_series
from app.services.inspection_health import current_health

_cache = QueryCache()
MAX_ROWS = 5000
MAX_COMPONENT_PAGES = 20
INSTANCE_RECORD_FIELDS = {"record_id", "instance_id", "instance_name", "inspection_policy", "inspection_scope",
                          "task_name", "start_time", "end_time", "status", "progress", "total_count", "normal_count",
                          "abnormal_count", "failed_count"}
SLOW_FIELDS = {"sql_id", "user_name", "node_id", "node_name", "sql_text", "query_plan", "calls", "avg_exec_time",
               "avg_cpu_time", "avg_io_time", "avg_returned_rows", "avg_fetched_rows", "buffer_hit_ratio", "sql_hit_ratio"}
SLOW_DETAIL_FIELDS = {"sql_id", "user_name", "client_ip", "client_port", "node_id", "node_name", "sql_text", "query_plan",
                      "start_time", "finish_time", "returned_rows", "fetched_rows", "fetched_pages", "hit_pages", "total_time",
                      "cpu_time", "plan_time", "io_time", "lock_count", "lock_time",
                      "buffer_hit_ratio", "sql_hit_ratio"}
SESSION_FIELDS = {"session_id", "pid", "unique_sql_id", "database_name", "client_ip", "user_name", "wait",
                  "block_session", "wait_event", "state", "query_runtime", "query", "back_end_start", "transaction_start",
                  "query_start", "application_name", "exec_time", "trans_num", "rollback_num", "sql_num"}
CHECK_FIELDS = {"name", "description", "inspection_category", "status", "inspection_result", "possible_causes",
                "suggestions", "item", "indicators"}


@dataclass(frozen=True)
class Target:
    resource: Resource
    tenant: HcsAccount | None
    project: HcsProject | None

    @property
    def unavailable(self) -> str | None:
        if not self.tenant or not self.project or self.project.account_id != self.resource.tenant_id:
            return "实例未关联有效租户与项目，请先核对台账来源"
        if not self.tenant.enabled or not self.project.enabled:
            return "租户或项目已停用"
        return None


@dataclass(frozen=True)
class SlowQuery:
    start_at: datetime
    end_at: datetime
    node_ids: tuple[str, ...]
    threshold: int = 1
    sql_id: str | None = None

    @classmethod
    def create(cls, start_at: datetime, end_at: datetime, node_ids: Sequence[str],
               threshold: int = 1, sql_id: str | None = None) -> SlowQuery:
        start, end = window(start_at, end_at)
        if not 10**12 <= int(start.timestamp()*1000) < int(end.timestamp()*1000) < 10**13:
            raise BizError(ErrorCode.BAD_REQUEST, "慢 SQL 起止时间须为递增的 13 位 UTC 毫秒时间戳")
        if not node_ids or len(node_ids) > 20 or not 1 <= threshold <= 99:
            raise BizError(ErrorCode.BAD_REQUEST, "请选择 1–20 个节点；慢 SQL 阈值为 1–99 秒")
        for value in (*node_ids, *([sql_id] if sql_id else [])):
            valid_id(value)
        return cls(start, end, tuple(dict.fromkeys(node_ids)), threshold, sql_id)


def valid_id(value: str) -> str:
    try:
        return identifier(value)
    except ValueError as exc:
        raise BizError(ErrorCode.BAD_REQUEST, "资源或节点标识格式不正确") from exc


def window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or end.tzinfo is None:
        raise BizError(ErrorCode.BAD_REQUEST, "起止时间须包含时区")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start or end - start > timedelta(days=31):
        raise BizError(ErrorCode.BAD_REQUEST, "时间范围须大于 0 且不超过 31 天")
    return start, end


def _resources(scope: Scope):
    return scope.apply(select(Resource).where(Resource.resource_type == "gaussdb_instance",
                                              Resource.lifecycle == "ACTIVE"), Resource.tenant_id)


def _local_target(db: Session, scope: Scope, resource_ref: int) -> Target:
    row = db.scalar(_resources(scope).where(Resource.id == resource_ref))
    if row is None:
        raise not_found("GaussDB 实例")
    tenant = db.get(HcsAccount, row.tenant_id) if row.tenant_id is not None else None
    project = db.get(HcsProject, row.project_ref) if row.project_ref is not None else None
    return Target(row, tenant, project)


def _target(db: Session, scope: Scope, resource_ref: int) -> Target:
    target = _local_target(db, scope, resource_ref)
    valid_id(target.resource.resource_id)
    return target


def instance_out(target: Target) -> GaussInstance:
    row, tenant, project = target.resource, target.tenant, target.project
    attrs = row.attrs or {}
    if project and project.account_id != row.tenant_id:
        project = None
    return GaussInstance(id=row.id, resource_id=row.resource_id, name=row.name, tenant_id=row.tenant_id,
                         tenant_name=tenant.name if tenant else "未分配租户", project_ref=row.project_ref,
                         project_name=(project.project_name or project.project_id) if project else None,
                         region=row.region, status_raw=row.status_raw, health=current_health(row),
                         vcpus=row.vcpus, memory_size=finite_number(attrs.get("memory_size")),
                         memory_unit="GB" if attrs.get("memory_unit_verified") is True else None,
                         storage_size=finite_number(attrs.get("volume_size")),
                         storage_unit="GB" if attrs.get("volume_unit_verified") is True else None,
                         snapshot_at=row.last_seen)


def list_instances(db: Session, scope: Scope, *, keyword: str | None = None, tenant_id: int | None = None,
                   project_ref: int | None = None, page: int = 1, page_size: int = 50) -> GaussInstancesOut:
    stmt = _resources(scope)
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        stmt = stmt.where(Resource.tenant_id == tenant_id)
    if project_ref is not None:
        stmt = stmt.where(Resource.project_ref == project_ref)
    if keyword and keyword.strip():
        stmt = stmt.where(or_(Resource.name.ilike(f"%{keyword.strip()}%"),
                              Resource.resource_id.ilike(f"%{keyword.strip()}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Resource.name, Resource.id).offset((page-1)*page_size).limit(page_size)).all()
    tenants = {t.id: t for t in db.scalars(select(HcsAccount).where(HcsAccount.id.in_(
        {r.tenant_id for r in rows if r.tenant_id is not None})))}
    projects = {p.id: p for p in db.scalars(select(HcsProject).where(HcsProject.id.in_(
        {r.project_ref for r in rows if r.project_ref is not None})))}
    return GaussInstancesOut(items=[instance_out(Target(r, tenants.get(r.tenant_id), projects.get(r.project_ref)))
                                    for r in rows], total=total, page=page, page_size=page_size)


def get_instance(db: Session, scope: Scope, resource_ref: int) -> GaussInstance:
    return instance_out(_local_target(db, scope, resource_ref))


def list_projects(db: Session, scope: Scope) -> list[GaussProjectOption]:
    stmt = select(HcsProject, HcsAccount).select_from(Resource).join(
        HcsProject, (Resource.project_ref == HcsProject.id) & (Resource.tenant_id == HcsProject.account_id)
    ).join(HcsAccount, HcsAccount.id == Resource.tenant_id).where(
        Resource.resource_type == "gaussdb_instance", Resource.lifecycle == "ACTIVE")
    # 仅 DISTINCT 项目主键；不能对含 JSON 凭据/配置的租户整行执行 PostgreSQL DISTINCT。
    visible = scope.apply(stmt.with_only_columns(HcsProject.id), Resource.tenant_id).distinct()
    rows = db.execute(select(HcsProject, HcsAccount).join(HcsAccount, HcsAccount.id == HcsProject.account_id).where(
        HcsProject.id.in_(visible))).all()
    options = [GaussProjectOption(project_ref=project.id, project_name=project.project_name or project.project_id,
        tenant_id=tenant.id, tenant_name=tenant.name, region=project.region_id) for project, tenant in rows]
    return sorted(options, key=lambda option: (
        option.tenant_name, option.project_name, option.region or "", option.project_ref))


@contextmanager
def native_client(db: Session, target: Target) -> Iterator[HcsClient]:
    assert target.tenant is not None and target.project is not None
    try:
        client = client_for(target.tenant, target.project)
    except (ValueError, TypeError) as exc:
        raise HcsError(0, "LOCAL.GAUSSDB_CONFIG", "GaussDB 接入配置不完整") from exc
    try:
        client.configure_qps(settings_service.get_collect(db).hcs_qps)
        with request_budget(attempts=80, seconds=45):
            yield client
    finally:
        client.metrics_snapshot()
        client.http.close()


def _identity(target: Target, operation: str, options: object) -> str:
    tenant, project, row = target.tenant, target.project, target.resource
    assert tenant is not None and project is not None
    settings = get_settings()
    values = [row.id, row.resource_id, row.tenant_id, row.project_ref, tenant.enabled, project.enabled,
              tenant.auth_type, tenant.iam_endpoint, tenant.domain_name, tenant.domain_id, tenant.username,
              tenant.access_key, tenant.secret_cipher, tenant.endpoints, project.project_id, project.region_id,
              project.external_domain, settings.hcs_endpoints, settings.hcs_scheme, settings.hcs_region,
              settings.hcs_external_domain, settings.hcs_mock_url if settings.env != "prod" else None,
              operation, options]
    return "gaussdb-native:v3:" + hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _message(exc: HcsError) -> str:
    if exc.status in (401, 403):
        return "GaussDB 认证或接口授权不足，请核对项目权限"
    if exc.status in (404, 405, 501):
        return "当前 GaussDB 版本或端点未提供此原生接口"
    if exc.code == "LOCAL.GAUSSDB_CONFIG":
        return "GaussDB 接入配置不完整，请核对租户接入"
    if exc.code == "LOCAL.GAUSSDB_RESPONSE":
        return "GaussDB 原生接口返回的数据结构不完整"
    return "GaussDB 原生接口查询失败，请查看接口调用记录"


def _error(exc: HcsError) -> GaussNativeRows:
    return GaussNativeRows(status="UNAVAILABLE" if exc.status in (401, 403, 404, 405, 501) else "ERROR",
                           queried_at=utcnow(), warning=_message(exc))


def _dict(body: object) -> dict:
    if not isinstance(body, dict):
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "响应须为对象")
    return body


def _rows(body: object, key: str) -> tuple[list[dict], int | None]:
    data = _dict(body)
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "原生记录列表结构不完整")
    count = data.get("total_count", data.get("total"))
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    return rows, count


def _fields(rows: Sequence[dict], allowed: set[str]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key in allowed} for row in rows[:MAX_ROWS]]


def _cached_rows(db: Session, target: Target, operation: str, options: object,
                 load: Callable[[HcsClient], GaussNativeRows]) -> GaussNativeRows:
    if target.unavailable:
        return GaussNativeRows(status="DISABLED", queried_at=utcnow(), warning=target.unavailable)
    def fetch() -> dict:
        try:
            with native_client(db, target) as client:
                value = load(client)
        except HcsError as exc:
            value = _error(exc)
        except (ValueError, TypeError):
            value = _error(HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "原生响应不完整"))
        return {"failed": value.status in ("ERROR", "UNAVAILABLE", "PARTIAL"), "data": value.model_dump(mode="json")}
    try:
        raw, _ = _cache.get(_identity(target, operation, options), fetch, ttl=30)
    except HcsError as exc:
        return _error(exc)
    return GaussNativeRows.model_validate(raw["data"])


def _components(client: HcsClient, iid: str) -> list[GaussComponent]:
    result: list[GaussComponent] = []
    seen: set[tuple[str, str]] = set()
    seen_nodes: set[str] = set()
    offset = 0
    for _ in range(MAX_COMPONENT_PAGES):
        body = client.get("gaussdb", instance_path(iid, "components"), params={"offset": offset, "limit": 100})
        rows, total = _rows(body, "nodes")
        if not rows:
            if total is not None and offset < total:
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件分页提前结束")
            return result
        if total is not None and offset + len(rows) > total:
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件总数与记录不一致")
        for row in rows:
            nested = row.get("components")
            if not isinstance(row.get("id"), str) or not isinstance(nested, list):
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "节点组件结构不完整")
            if row["id"] in seen_nodes:
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件节点分页重复")
            seen_nodes.add(row["id"])
            for item in nested:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("type"), str):
                    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件标识或类型不完整")
                pair = (row["id"], item["id"])
                if pair in seen:
                    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件列表重复")
                seen.add(pair)
                result.append(GaussComponent(node_id=row["id"], node_name=row.get("name"), component_id=item["id"],
                                              component_type=item["type"], role=item.get("role"), status=item.get("status")))
        offset += len(rows)
        if (total is not None and offset >= total) or (total is None and len(rows) < 100):
            return result
    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件记录过多，请核对接口分页")


def overview(db: Session, scope: Scope, resource_ref: int) -> GaussOverviewOut:
    target = _target(db, scope, resource_ref)
    now = utcnow()
    if target.unavailable:
        return GaussOverviewOut(instance=instance_out(target), queried_at=now,
                                disk=GaussDisk(status="DISABLED", warning=target.unavailable),
                                components_status="DISABLED", components_warning=target.unavailable)
    def fetch() -> dict:
        disk = GaussDisk(status="ERROR")
        components: list[GaussComponent] = []
        component_warning = None
        component_status: Literal["AVAILABLE", "NO_DATA", "ERROR", "UNAVAILABLE"] = "AVAILABLE"
        try:
            with native_client(db, target) as client:
                try:
                    used, total, usage = volume_usage(client, target.resource.resource_id)
                    received_at = utcnow()
                    disk = GaussDisk(status="AVAILABLE" if usage is not None else "PARTIAL", used_gb=used, total_gb=total,
                                     usage_percent=usage, observed_at=received_at, collected_at=received_at,
                                     time_basis="RESPONSE_RECEIVED",
                                     warning="容量为 0，无法计算使用率" if usage is None else None)
                except HcsError as exc:
                    disk = GaussDisk(status="UNAVAILABLE" if exc.status in (401, 403, 404, 405, 501) else "ERROR",
                                     warning=_message(exc))
                try:
                    components = _components(client, target.resource.resource_id)
                    component_status = "AVAILABLE" if components else "NO_DATA"
                except HcsError as exc:
                    component_status, component_warning = "ERROR", _message(exc)
                except (ValueError, TypeError):
                    component_status, component_warning = "ERROR", "GaussDB 组件响应结构不完整"
        except HcsError as exc:
            disk = GaussDisk(status="UNAVAILABLE", warning=_message(exc))
            component_status, component_warning = "UNAVAILABLE", _message(exc)
        return {"failed": disk.status != "AVAILABLE" or component_status == "ERROR",
                "disk": disk.model_dump(mode="json"), "components": [c.model_dump() for c in components],
                "components_status": component_status, "components_warning": component_warning,
                "queried_at": utcnow().isoformat()}
    try:
        raw, _ = _cache.get(_identity(target, "overview", None), fetch, ttl=30)
    except HcsError as exc:
        return GaussOverviewOut(instance=instance_out(target), queried_at=utcnow(),
            disk=GaussDisk(status="ERROR", warning=_message(exc)), components_status="ERROR", components_warning=_message(exc))
    return GaussOverviewOut(instance=instance_out(target), **{key: value for key, value in raw.items() if key != "failed"})


def _query_component(kind: object, role: object, status: object = None) -> bool:
    # CN 无主备关系；DN 仅主备。role 缺失时只采信表 4-80 明确的 Primary/Standby，Normal 不证明角色。
    return kind == "CN" or (kind == "DN" and (
        role in ("master", "slave") or (role in (None, "") and status in ("Primary", "Standby"))))


def _slow_nodes(client: HcsClient, iid: str) -> list[dict]:
    rows, _ = _rows(client.post("gaussdb", instance_path(iid, "slow-sql-execute-node"), json={"action": "slow"}), "nodes")
    if any(row.get("instance_id") != iid or not isinstance(row.get("node_id"), str) for row in rows):
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "慢 SQL 节点不属于当前实例")
    return [row for row in rows if _query_component(row.get("component_type"), row.get("role"))]


def slow_nodes(db: Session, scope: Scope, resource_ref: int) -> GaussNativeRows:
    target = _target(db, scope, resource_ref)
    def load(client: HcsClient) -> GaussNativeRows:
        rows = _slow_nodes(client, target.resource.resource_id)
        return GaussNativeRows(status="AVAILABLE" if rows else "NO_DATA", queried_at=utcnow(),
                               items=_fields(rows, {"node_id", "node_name", "role", "component_type", "instance_id"}),
                               total=len(rows))
    return _cached_rows(db, target, "slow_nodes", None, load)


def slow_sql(db: Session, scope: Scope, resource_ref: int, query: SlowQuery, *, detail: bool = False) -> GaussNativeRows:
    target = _target(db, scope, resource_ref)
    if detail and not query.sql_id:
        raise BizError(ErrorCode.BAD_REQUEST, "查询慢 SQL 详情须选择 SQL ID")
    def load(client: HcsClient) -> GaussNativeRows:
        allowed = {row["node_id"] for row in _slow_nodes(client, target.resource.resource_id)}
        if not set(query.node_ids) <= allowed:
            raise BizError(ErrorCode.BAD_REQUEST, "所选节点不属于当前实例的 CN 或 DN 主备组件")
        body: dict[str, Any] = {"instance_id": target.resource.resource_id, "node_ids": list(query.node_ids),
                "start_time": int(query.start_at.timestamp()*1000), "end_time": int(query.end_at.timestamp()*1000)}
        if query.sql_id:
            body["sql_id"] = query.sql_id
        if not detail:
            body["threshold"] = query.threshold
        suffix = "slow-sql-detail" if detail else "slow-sql-list"
        rows, count = _rows(client.post("gaussdb", instance_path(target.resource.resource_id, suffix), json=body),
                            "slow_sql_details" if detail else "slow_sql_infos")
        if any(row.get("node_id") not in query.node_ids or (query.sql_id and row.get("sql_id") != query.sql_id)
               or ("instance_id" in row and row["instance_id"] != target.resource.resource_id)
               for row in rows):
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "慢 SQL 响应与查询节点不匹配")
        truncated = len(rows) > MAX_ROWS or (count is not None and count > len(rows))
        return GaussNativeRows(status="PARTIAL" if truncated else "AVAILABLE" if rows else "NO_DATA",
                               queried_at=utcnow(), items=_fields(rows, SLOW_DETAIL_FIELDS if detail else SLOW_FIELDS),
                               total=count, truncated=truncated, start_at=query.start_at, end_at=query.end_at,
                               warning="原生接口未返回全部记录，请缩小时间或节点范围" if truncated else None,
                               units=({key: "微秒" for key in ("total_time", "cpu_time", "plan_time", "io_time", "lock_time")}
                                      if detail else {"avg_exec_time": "微秒", "avg_cpu_time": "微秒", "avg_io_time": "微秒"}))
    options = {key: value for key, value in query.__dict__.items() if not detail or key != "threshold"}
    return _cached_rows(db, target, "slow_detail" if detail else "slow_sql", options, load)


def sessions(db: Session, scope: Scope, resource_ref: int, node_id: str, component_id: str, *, limit: int = 100,
             filters: dict[str, str] | None = None) -> GaussNativeRows:
    target = _target(db, scope, resource_ref)
    valid_id(node_id)
    valid_id(component_id)
    if not 1 <= limit <= 200:
        raise BizError(ErrorCode.BAD_REQUEST, "会话查询上限为 1–200 条")
    filters = filters or {}
    allowed_filters = {"database_name", "client_ip", "user_name", "wait", "session_id", "state", "wait_event",
                       "exec_time", "query_runtime", "unique_sql_id"}
    if not set(filters) <= allowed_filters or any(not isinstance(value, str) or len(value) > 255 for value in filters.values()):
        raise BizError(ErrorCode.BAD_REQUEST, "会话筛选参数不正确")
    if "wait" in filters and filters["wait"] not in ("t", "f"):
        raise BizError(ErrorCode.BAD_REQUEST, "等待状态仅支持 t 或 f")
    def load(client: HcsClient) -> GaussNativeRows:
        pairs = {(item.node_id, item.component_id) for item in _components(client, target.resource.resource_id)
                 if _query_component(item.component_type, item.role, item.status)}
        if (node_id, component_id) not in pairs:
            raise BizError(ErrorCode.BAD_REQUEST, "节点和 CN 或 DN 主备组件不匹配")
        body: dict[str, Any] = {"node_id": node_id, "component_id": component_id, "limit": limit,
                                "is_statistic_open": "false"}
        if filters:
            body["query_info"] = filters
        rows, _ = _rows(client.post("gaussdb", instance_path(target.resource.resource_id, "real-time-session"), json=body),
                        "sessions")
        if any(("node_id" in row and row["node_id"] != node_id)
               or ("component_id" in row and row["component_id"] != component_id)
               or ("instance_id" in row and row["instance_id"] != target.resource.resource_id) for row in rows):
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "会话响应与当前实例节点组件不匹配")
        truncated = len(rows) >= limit
        return GaussNativeRows(status="PARTIAL" if truncated else "AVAILABLE" if rows else "NO_DATA",
                               queried_at=utcnow(), items=_fields(rows[:limit], SESSION_FIELDS), total=None,
                               truncated=truncated, units={"exec_time": "秒"},
                               warning=f"仅返回前 {limit} 条会话，请缩小筛选范围" if truncated else None)
    return _cached_rows(db, target, "sessions", [node_id, component_id, limit, filters], load)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value/1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _record_pages(client: HcsClient, target: Target, start_at: datetime | None = None,
                  end_at: datetime | None = None) -> Iterator[tuple[list[dict], bool]]:
    offset = 0
    seen: set[str] = set()
    for _ in range(20):
        body: dict[str, Any] = {"instance_name": target.resource.name, "status": "completed", "limit": 100,
                                "offset": offset, "sort": "start_time", "order": "desc"}
        if start_at is not None and end_at is not None:
            body["inspection_start_time_range"] = {"start_time": int(start_at.timestamp()*1000),
                                                    "end_time": int(end_at.timestamp()*1000)}
        rows, total = _rows(client.post("gaussdb", "/gaussdb/v3/{project_id}/inspections/records", json=body), "records")
        if total is not None and offset + len(rows) > total:
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "巡检记录总数与分页记录不一致")
        for row in rows:
            rid = row.get("record_id")
            if not isinstance(rid, str) or not rid or rid in seen:
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "巡检记录标识缺失或分页重复")
            seen.add(rid)
        offset += len(rows)
        complete = (offset >= total) if total is not None else len(rows) < 100
        yield rows, complete
        if complete or not rows:
            return


def inspections(db: Session, scope: Scope, resource_ref: int, *, start_at: datetime | None = None,
                end_at: datetime | None = None, limit: int = 50, offset: int = 0) -> GaussNativeRows:
    target = _target(db, scope, resource_ref)
    if (start_at is None) != (end_at is None) or not 1 <= limit <= 100 or not 0 <= offset <= 1000:
        raise BizError(ErrorCode.BAD_REQUEST, "巡检时间须成对提供，条数 1–100，偏移 0–1000")
    if start_at is not None and end_at is not None:
        start_at, end_at = window(start_at, end_at)
    def load(client: HcsClient) -> GaussNativeRows:
        selected: list[dict] = []
        complete = False
        for rows, page_complete in _record_pages(client, target, start_at, end_at):
            complete = page_complete
            for row in rows:
                if row.get("instance_id") != target.resource.resource_id:
                    continue
                record_at = _timestamp(row.get("start_time"))
                if start_at is not None and end_at is not None:
                    if record_at is None or not start_at <= record_at < end_at:
                        continue
                if row.get("status") != "completed":
                    continue
                selected.append(row)
        result = _fields(selected[offset:offset+limit], INSTANCE_RECORD_FIELDS)
        return GaussNativeRows(status="PARTIAL" if not complete else "AVAILABLE" if result else "NO_DATA",
                               queried_at=utcnow(), items=result, total=len(selected) if complete else None,
                               truncated=not complete, start_at=start_at, end_at=end_at,
                               warning="巡检记录较多，请缩小时间范围" if not complete else None,
                               units={"start_time": "UTC 毫秒", "end_time": "UTC 毫秒"})
    return _cached_rows(db, target, "inspections", [start_at, end_at, limit, offset, target.resource.name], load)


def inspection_detail(db: Session, scope: Scope, resource_ref: int, record_id: str) -> GaussNativeRows:
    target = _target(db, scope, resource_ref)
    valid_id(record_id)
    def load(client: HcsClient) -> GaussNativeRows:
        body = _dict(client.get("gaussdb", "/gaussdb/v3/{project_id}/inspections/records/" + identifier(record_id)))
        if body.get("instance_id") != target.resource.resource_id:
            raise not_found("当前实例的巡检记录")
        if body.get("status") is not None and body["status"] != "completed":
            raise not_found("已完成的巡检记录")
        # 详情契约没有必填 status，须再从 completed 记录列表验证，不假设任意 record_id 已完成。
        found = False
        for records, _ in _record_pages(client, target):
            found = any(row.get("record_id") == record_id and row.get("instance_id") == target.resource.resource_id
                        and row.get("status") == "completed" for row in records)
            if found:
                break
        if not found:
            raise not_found("当前实例已完成的巡检记录")
        rows, total = _rows(body, "inspection_results")
        truncated = len(rows) > MAX_ROWS or (total is not None and total > len(rows))
        normalization_warnings: list[str] = []
        metric_series = normalize_metric_series(_fields(rows, CHECK_FIELDS), warnings=normalization_warnings)
        return GaussNativeRows(status="PARTIAL" if truncated else "AVAILABLE" if rows else "NO_DATA", queried_at=utcnow(),
                               items=_fields(rows, CHECK_FIELDS), total=total if total is not None else len(rows),
                               record_id=record_id,
                               truncated=truncated,
                               start_at=_timestamp(body.get("start_time")), end_at=_timestamp(body.get("end_time")),
                               metric_series=metric_series,
                               warning="报告指标按原生巡检时间展示，不代表当前使用率" + (
                                   "；原生检查项未全部返回或数量超过本地上限" if truncated else "") + (
                                   "；" + "；".join(normalization_warnings) if normalization_warnings else ""))
    return _cached_rows(db, target, "inspection_detail", [record_id, target.resource.name], load)
