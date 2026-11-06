"""ECS API 参考 §5.10.1：GET /v1/{project_id}/cloudservers/limits。

PDF 路径参数 tenant_id 指 HCS 项目 ID，不是应用租户外键。只读取实际 limits，
不回退已准备废弃的 os-quota-sets、不读 defaults、不以台账计数推算用量。
读取缓存无云端副作用；refresh_quota 仅 flush，调用方负责审计和事务提交。
"""

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import Scope
from app.core.response import BizError, ErrorCode, not_found
from app.hcs import HcsClient, HcsError
from app.hcs.factory import client_for
from app.hcs.ratelimit import request_budget
from app.models import CollectSwitch, HcsAccount, HcsProject, SysSetting
from app.models.base import utcnow
from app.schemas.ecs_quotas import EcsQuotaItem, EcsQuotaOut, EcsQuotasOut
from app.services import collect_switch, settings_service
from app.services.tenancy import Target

NATIVE_LIMITS_PATH = "/v1/{project_id}/cloudservers/limits"


@dataclass(frozen=True)
class _Quota:
    key: str
    label: str
    unit: Literal["count", "vCPU", "MB", "byte"]
    limit: str
    used: str | None = None
    note: str | None = None


# 原生 §5.10.1 表 5-472；仅这 6 种用量有上游字段。
_VPC_NOTE = "ECS 返回值仅供参考，实际安全组配额以 VPC 为准（PDF §5.10.1）。"
QUOTAS = (
    _Quota("instances", "云服务器", "count", "maxTotalInstances", "totalInstancesUsed"),
    _Quota("cores", "CPU 核数", "vCPU", "maxTotalCores", "totalCoresUsed"),
    _Quota("ram", "内存", "MB", "maxTotalRAMSize", "totalRAMUsed"),
    _Quota("server_groups", "云服务器组", "count", "maxServerGroups", "totalServerGroupsUsed"),
    _Quota("server_group_members", "每组云服务器成员", "count", "maxServerGroupMembers"),
    _Quota("key_pairs", "SSH 密钥对", "count", "maxTotalKeypairs"),
    _Quota("floating_ips", "浮动 IP", "count", "maxTotalFloatingIps", "totalFloatingIpsUsed"),
    _Quota("security_groups", "安全组", "count", "maxSecurityGroups", "totalSecurityGroupsUsed", _VPC_NOTE),
    _Quota("security_group_rules", "每个安全组规则", "count", "maxSecurityGroupRules", note=_VPC_NOTE),
    _Quota("server_metadata", "云服务器元数据", "count", "maxServerMeta"),
    _Quota("image_metadata", "镜像元数据", "count", "maxImageMeta"),
    _Quota("injected_files", "注入文件", "count", "maxPersonality"),
    _Quota("injected_file_content_bytes", "注入文件内容大小", "byte", "maxPersonalitySize"),
)


class _Cache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    identity: str
    absolute: dict[str, StrictInt | None] = Field(default_factory=dict)
    fetched_at: AwareDatetime | None = None
    successful_attempt_at: AwareDatetime | None = None
    last_attempt_at: AwareDatetime
    last_error: Literal["upstream_error", "invalid_response", "configuration_error"] | None = None


class _InvalidLimitsResponse(Exception):
    pass


def cache_key(tenant_id: int, project_ref: int) -> str:
    return f"ecs_quota:{tenant_id}:{project_ref}"


def _identity(tenant: HcsAccount, project: HcsProject) -> str:
    # 同一外键被改接其他项目、区域、账号域或服务端点时，旧值不能冒充新目标的配额。
    settings = get_settings()
    values = [tenant.id, project.id, project.project_id, project.region_id, project.external_domain,
              tenant.domain_id, tenant.domain_name, tenant.iam_endpoint, (tenant.endpoints or {}).get("ecs"),
              settings.hcs_endpoints.get("ecs"), settings.hcs_scheme,
              settings.hcs_mock_url if settings.env != "prod" else None]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


def _integer(value: object, *, limit: bool) -> int | None:
    # 原生定义为 Integer；兼容某些网关返回整数字符串。拒绝 bool、浮点数、非整数和非法负数。
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        try:
            number = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number >= 0 or (limit and number == -1) else None


def normalize_absolute(absolute: dict) -> list[EcsQuotaItem]:
    """固定输出原生支持的条目；无限制只接受限额 -1，用量 -1 是未知。"""
    items = []
    for spec in QUOTAS:
        limit = _integer(absolute.get(spec.limit), limit=True)
        used = _integer(absolute.get(spec.used), limit=False) if spec.used else None
        items.append(EcsQuotaItem(
            key=spec.key, label=spec.label, unit=spec.unit,
            limit=limit if limit is not None and limit >= 0 else None, used=used,
            limit_status="unlimited" if limit == -1 else ("known" if limit is not None else "unknown"),
            usage_status="not_reported" if spec.used is None else ("known" if used is not None else "unknown"),
            remaining=limit - used if limit is not None and limit >= 0 and used is not None else None,
            limit_field=spec.limit, usage_field=spec.used, note=spec.note,
        ))
    return items


def _read_cache(row: SysSetting | None, tenant: HcsAccount, project: HcsProject) -> _Cache | None:
    if row is None:
        return None
    try:
        cache = _Cache.model_validate(row.value)
    except ValidationError:
        return None
    return cache if cache.identity == _identity(tenant, project) else None


def _out(tenant: HcsAccount, project: HcsProject, cache: _Cache | None, *, collection_enabled: bool = True) -> EcsQuotaOut:
    items = normalize_absolute(cache.absolute if cache and cache.fetched_at else {})
    status: Literal["not_collected", "available", "partial", "refresh_failed"] = "not_collected"
    if cache and cache.last_error:
        status = "refresh_failed"
    elif cache and cache.fetched_at:
        status = "partial" if any(i.limit_status == "unknown" or i.usage_status == "unknown" for i in items) else "available"
    return EcsQuotaOut(
        tenant_id=tenant.id, tenant_name=tenant.name, project_ref=project.id, project_id=project.project_id,
        project_name=project.project_name or project.project_id, region=project.region_id,
        enabled=tenant.enabled and project.enabled and collection_enabled, status=status,
        fetched_at=cache.fetched_at if cache else None, source_observed_at=None,
        last_attempt_at=cache.last_attempt_at if cache else None,
        last_error=cache.last_error if cache else None,
        is_stale=bool(cache and cache.fetched_at and cache.last_error), quotas=items,
    )


def _projects():
    return (select(HcsAccount, HcsProject).join(HcsProject, HcsProject.account_id == HcsAccount.id)
            .execution_options(populate_existing=True))


def _ecs_switches(db: Session, tenant_ids: Sequence[int]) -> collect_switch.Switches:
    # 直接读列，避免长生命周期 Session 的 ORM identity map 保留已经关闭前的主开关。
    rows = db.execute(select(CollectSwitch.tenant_id, CollectSwitch.enabled).where(
        CollectSwitch.resource_type == "ecs_server", CollectSwitch.tenant_id.in_([0, *tenant_ids])))
    return collect_switch.Switches({(tid, "ecs_server"): enabled for tid, enabled in rows})


def list_quotas(db: Session, scope: Scope, *, tenant_id: int | None = None, project_ref: int | None = None,
                page: int = 1, page_size: int = 100) -> EcsQuotasOut:
    query = scope.apply(_projects(), HcsAccount.id)
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        if db.get(HcsAccount, tenant_id) is None:
            raise not_found("租户")
        query = query.where(HcsAccount.id == tenant_id)
    if project_ref is not None:
        query = query.where(HcsProject.id == project_ref)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(HcsAccount.id, HcsProject.id).offset((page - 1) * page_size).limit(page_size)).all()
    keys = [cache_key(tenant.id, project.id) for tenant, project in rows]
    cached = {row.key: row for row in db.scalars(select(SysSetting).where(SysSetting.key.in_(keys))
                                               .execution_options(populate_existing=True))} if keys else {}
    switches = _ecs_switches(db, [t.id for t, _ in rows])
    return EcsQuotasOut(
        items=[_out(t, p, _read_cache(cached.get(cache_key(t.id, p.id)), t, p),
                    collection_enabled=switches.enabled(t.id, "ecs_server")) for t, p in rows],
        total=total, page=page, page_size=page_size,
    )


def _target(tenant: HcsAccount, project: HcsProject) -> Target:
    return Target(tenant_id=tenant.id, code=tenant.code, name=tenant.name, short_name=tenant.short_name,
                  project_ref=project.id, project_id=project.project_id,
                  project_name=project.project_name or project.project_id, region=project.region_id,
                  tenant=tenant, project=project)


def resolve_target(db: Session, scope: Scope, project_ref: int, tenant_id: int | None = None) -> Target:
    query = scope.apply(_projects().where(HcsProject.id == project_ref), HcsAccount.id)
    if tenant_id is not None:
        scope.ensure(tenant_id, "项目")
        query = query.where(HcsAccount.id == tenant_id)
    row = db.execute(query).first()
    if row is None:
        raise not_found("项目")
    return _target(*row)


@contextmanager
def quota_client(db: Session, tenant: HcsAccount, project: HcsProject) -> Iterator[HcsClient]:
    client = client_for(tenant, project)
    try:
        client.configure_qps(settings_service.get_collect(db).hcs_qps)
        yield client
    finally:
        try:
            client.metrics_snapshot()
        finally:
            client.http.close()


def _fetch(client: HcsClient, tenant: HcsAccount, project: HcsProject) -> dict[str, int | None]:
    # 调度器传入的客户端也必须属于这个租户 × 项目，不允许环境变量/其他租户兜底。
    if (client.tenant_id, client.project_ref, client.project_id) != (tenant.id, project.id, project.project_id):
        raise BizError(ErrorCode.BAD_REQUEST, "ECS 配额客户端与租户/项目不匹配")
    path = NATIVE_LIMITS_PATH.replace("{project_id}", quote(project.project_id, safe=""))
    body = client.get("ecs", path)
    if not isinstance(body, dict) or not isinstance(body.get("absolute"), dict):
        raise _InvalidLimitsResponse
    absolute = body["absolute"]
    # 只保存文档列出的数字字段；上游附带的机密、错误体及任意扩展不进入缓存或返回值。
    return {field: _integer(absolute.get(field), limit=field == spec.limit)
            for spec in QUOTAS for field in (spec.limit, spec.used) if field is not None}


def _store_cache(db: Session, tenant: HcsAccount, project: HcsProject, candidate: _Cache) -> _Cache:
    """先原子创建占位行，再锁行合并；锁仅在云请求完成后持有，直到调用方提交事务。

    PostgreSQL ON CONFLICT 避免并发首次插入的唯一键错误，FOR UPDATE 串行化同项目更新。
    SQLite 的 INSERT 获取写锁，也使用 ON CONFLICT，兼容本地开发/测试。
    最近发起的刷新决定 last_error，最近发起且成功的刷新决定数值，两者分别排序。
    """
    key = cache_key(tenant.id, project.id)
    dialect = db.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    db.execute(insert(SysSetting).values(key=key, value=candidate.model_dump(mode="json"))
               .on_conflict_do_nothing(index_elements=["key"]))
    setting = db.scalar(select(SysSetting).where(SysSetting.key == key).with_for_update()
                        .execution_options(populate_existing=True))
    assert setting is not None
    previous = _read_cache(setting, tenant, project)
    if previous is not None:
        if candidate.last_error is not None or (previous.fetched_at is not None and (
                previous.successful_attempt_at or previous.fetched_at) > candidate.last_attempt_at):
            # 失败使用提交时的最新成功值，而非请求开始时读到的旧快照。
            # 老缓存未记录成功请求起始时间时，以 fetched_at 保守判断，不能回退数值。
            candidate.absolute = previous.absolute
            candidate.fetched_at = previous.fetched_at
            candidate.successful_attempt_at = previous.successful_attempt_at
        if previous.last_attempt_at > candidate.last_attempt_at:
            candidate.last_attempt_at = previous.last_attempt_at
            candidate.last_error = previous.last_error
    setting.value = candidate.model_dump(mode="json")
    db.flush()
    return candidate


def refresh_quota(db: Session, target: Target, client: HcsClient | None = None) -> EcsQuotaOut:
    """单项目只读云查询并 flush 缓存；失败保留上次成功值并返回 refresh_failed。

    不 commit、不审计、不修改/关闭注入的客户端。API 负责权限和审计，调度器负责运行记录和事务。
    target 必须是数据库中存在的明确租户/项目，且租户、项目、ECS 主采集开关都启用。
    手动刷新不受自动采集阶段频率/开关限制。并发时返回合并后的最新项目缓存状态。
    绝不调用 legacy_target/build_client。
    """
    if target.tenant_id is None or target.project_ref is None:
        raise not_found("租户项目")
    row = db.execute(_projects().where(HcsAccount.id == target.tenant_id, HcsProject.id == target.project_ref)).first()
    if row is None:
        raise not_found("租户项目")
    tenant, project = row
    if (target.project_id, target.region) != (project.project_id, project.region_id):
        raise BizError(ErrorCode.CONFLICT, "租户项目配置已变更，请重新选择项目", 409)
    if not tenant.enabled or not project.enabled:
        raise BizError(ErrorCode.CONFLICT, "租户或项目已停用，不能刷新 ECS 配额", 409)
    if not _ecs_switches(db, [tenant.id]).enabled(tenant.id, "ecs_server"):
        raise BizError(ErrorCode.CONFLICT, "该租户 ECS 主采集开关已关闭，不能刷新 ECS 配额", 409)
    cache = _Cache(identity=_identity(tenant, project), last_attempt_at=utcnow())
    try:
        with request_budget():
            if client is not None:
                absolute = _fetch(client, tenant, project)
            else:
                with quota_client(db, tenant, project) as owned_client:
                    absolute = _fetch(owned_client, tenant, project)
        cache.absolute = absolute
        cache.fetched_at = utcnow()
        cache.successful_attempt_at = cache.last_attempt_at
    except _InvalidLimitsResponse:
        cache.last_error = "invalid_response"
    except (HcsError, httpx.HTTPError):
        cache.last_error = "upstream_error"
    except (KeyError, ValueError):
        cache.last_error = "configuration_error"
    cache = _store_cache(db, tenant, project, cache)
    return _out(tenant, project, cache)
