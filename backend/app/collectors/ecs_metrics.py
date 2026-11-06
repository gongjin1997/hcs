"""ECS 指标与配额独立任务；指标采样只查询本地选定实例，不重拉机器清单。"""
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from types import SimpleNamespace

from app.collectors.base import Observation, RunContext
from app.metrics.ecs_diagnostics import INCOMPLETE, diagnostic_message
from app.metrics.ecs_readings import collect_readings, ecs_source
from app.models import HcsAccount, HcsProject, Resource, ResourceChange
from app.models.base import utcnow
from app.services import collect_switch, collection_policy
from app.services.ecs_monitoring import (
    METRICS,
    Reading,
    config_signature,
    number,
    parse_at,
    policy_dict,
    save_readings,
    timestamp_semantics,
)


def ip_index(resources: list[Resource]) -> dict[str, str]:
    """同一项目有重复私网地址时不猜归属；其他项目的 IP 从不进入本次索引。"""
    candidates: dict[str, set[tuple[int | None, int, str]]] = defaultdict(set)
    for resource in resources:
        extra = (resource.attrs or {}).get("fixed_ips")
        for raw in [resource.ip, *(extra if isinstance(extra, list) else [])]:
            if not isinstance(raw, str):
                continue
            try:
                address = str(ip_address(raw.strip()))
            except ValueError:
                continue
            candidates[address].add((resource.project_ref, resource.id, resource.resource_id))
    return {address: next(iter(ids))[2] for address, ids in candidates.items() if len(ids) == 1}


class EcsMetricsCollector:
    code, service, name = "ecs_metrics", "ecs", "ECS 使用指标采样"

    def collect(self, ctx: RunContext) -> list[Observation]:
        if ctx.db is None:
            ctx.errors.append("ECS 指标采样需要本地资源台账")
            return []
        policy = ctx.params.get("_collection_policy") or policy_dict(ctx.db, ctx.tenant_id)
        if not collect_switch.load(ctx.db).enabled(ctx.tenant_id, "ecs_server") or not policy["metrics"]["enabled"]:
            ctx.stats["metrics_skipped"] = "disabled"
            return []
        resources = collection_policy.select_resources(ctx.db, ctx.tenant_id, ctx.project_ref, policy)
        now = utcnow()
        if not resources:
            ctx.stats.update(metrics_source=ecs_source(ctx.tenant, policy), servers=0, with_metrics=0,
                             metrics_diagnostic={"status": "no_selected_resources"})
            return []
        # 同租户共用 Prometheus 地址；跨项目重复私网 IP 也不猜归属。
        from sqlalchemy import select

        all_resources = list(ctx.db.scalars(select(Resource).where(Resource.tenant_id == ctx.tenant_id,
            Resource.resource_type == "ecs_server", Resource.lifecycle == "ACTIVE")))
        index = ip_index(all_resources)
        selected_ids = {r.resource_id for r in resources}
        index = {address: sid for address, sid in index.items() if sid in selected_ids}
        expected_signature = config_signature(ctx.tenant, policy, ecs_source(ctx.tenant, policy, client=ctx.client))
        result = collect_readings(ctx, [r.resource_id for r in resources], index,
            metrics=policy.get("metric_keys", list(METRICS)), config=policy)
        now = utcnow()
        current_policy = policy_dict(ctx.db, ctx.tenant_id)
        current_tenant = ctx.tenant
        if ctx.tenant_id is not None:
            state = ctx.db.execute(select(HcsAccount.id, HcsAccount.enabled, HcsAccount.metrics_source,
                HcsAccount.endpoints, HcsAccount.prometheus_url, HcsAccount.secret_cipher, HcsAccount.access_key,
                HcsAccount.domain_name, HcsAccount.capabilities, HcsAccount.manageone_config).where(
                    HcsAccount.id == ctx.tenant_id)).mappings().first()
            current_tenant = SimpleNamespace(**state) if state is not None else None
        current_source = ecs_source(current_tenant, current_policy, client=ctx.client)
        project_enabled = ctx.project_ref is None or ctx.db.scalar(select(HcsProject.enabled).where(
            HcsProject.id == ctx.project_ref, HcsProject.account_id == ctx.tenant_id)) is True
        if (not collection_policy.stage_enabled(ctx.db, ctx.tenant_id, "metrics", current_policy)
                or ctx.tenant_id is not None and (current_tenant is None or not current_tenant.enabled)
                or not project_enabled
                or config_signature(current_tenant, current_policy, current_source) != expected_signature):
            ctx.stats["metrics_skipped"] = "configuration_changed"
            ctx.errors.append("ECS 采样期间接入配置或开关已变更，本次结果未入库")
            return []
        if result.source != current_source or result.source not in ("NONE", "PROMETHEUS", "CES", "MANAGEONE"):
            ctx.stats["metrics_skipped"] = "source_mismatch"
            ctx.errors.append("ECS 返回指标来源与当前配置不一致，本次结果未入库")
            return []
        allowed = {r.id for r in collection_policy.select_resources(ctx.db, ctx.tenant_id, ctx.project_ref, current_policy)}
        refs = [r.id for r in resources if r.id in allowed]
        resources = list(ctx.db.scalars(select(Resource).where(Resource.id.in_(refs), Resource.tenant_id == ctx.tenant_id,
            Resource.project_ref == ctx.project_ref).order_by(Resource.resource_id)
            .with_for_update().execution_options(populate_existing=True)))
        signature = config_signature(ctx.tenant, policy, result.source)
        accepted, rejected = save_readings(ctx.db, resources, result.readings, now,
            diagnostic=result.diagnostic, signature=signature)
        if rejected:
            ctx.errors.append("ECS 部分采样无效或与同一时刻记录冲突，未写入不确定值")
        diagnostic_metrics = result.diagnostic.get("metrics") or {}
        required = [key for key in policy["metric_keys"] if key in METRICS] or policy["metric_keys"]
        has_fresh = any((diagnostic_metrics.get(key) or {}).get("fresh_points", 0) > 0 for key in required)
        if (result.diagnostic.get("status") in INCOMPLETE
                and (result.diagnostic.get("status") != "partial" or not has_fresh)):
            # A completed request is not proof that this task obtained usage data.
            # Partial coverage alone (e.g. stopped VMs) remains a diagnostic;
            # real request errors already set ctx.errors independently.
            ctx.errors.insert(0, "ECS 指标采样未完成：" + diagnostic_message(result.diagnostic))
        observations = []
        for resource in resources:
            latest: dict[str, Reading] = {}
            for reading in accepted.get(resource.id, []):
                previous = latest.get(reading.metric)
                if previous is None or reading.observed_at > previous.observed_at:
                    latest[reading.metric] = reading
            snapshot = {key: {"value": point.value, "observed_at": point.observed_at.isoformat(),
                "collected_at": now.isoformat(), "source": point.source, "period_sec": point.period_sec,
                "dimensions": point.dimensions, "unit": point.unit,
                "timestamp_semantics": timestamp_semantics(point.source, key, result.diagnostic)}
                for key, point in latest.items()}
            previous_meta = (resource.attrs or {}).get("ecs_monitoring")
            if (isinstance(previous_meta, dict) and previous_meta.get("source") == result.source
                    and previous_meta.get("config_signature") == signature and isinstance(previous_meta.get("latest"), dict)):
                for key, old in previous_meta["latest"].items():
                    if (key not in policy["metric_keys"] or key not in METRICS or not isinstance(old, dict)
                            or old.get("source") != result.source or result.source == "NONE"):
                        continue
                    previous_at = parse_at(old.get("observed_at"))
                    current_at = parse_at(snapshot.get(key, {}).get("observed_at"))
                    if (previous_at is not None and number(old.get("value")) is not None
                            and previous_at <= now and (current_at is None or previous_at > current_at)):
                        snapshot[key] = dict(old)
            attrs: dict = {"metrics_source": result.source, "ecs_monitoring": {"source": result.source,
                "config_signature": signature, "checked_at": now.isoformat(), "diagnostic": result.diagnostic,
                "latest": snapshot}}
            metrics: dict = {key: None for key in METRICS}
            metrics["disk_mount"] = None
            for key, item in snapshot.items():
                at, value = parse_at(item.get("observed_at")), number(item.get("value"))
                if (at is not None and value is not None and now - timedelta(seconds=policy["stale_after_sec"])
                        <= at <= now + timedelta(seconds=30)):
                    metrics[key] = value
                    if key == "disk_util":
                        raw_dims = item.get("dimensions")
                        dims = raw_dims if isinstance(raw_dims, dict) else {}
                        metrics["disk_mount"] = dims.get("mountpoint", dims.get("mount_point", "-"))
            # 开机时间不是百分比，不写入百分比历史表；保留实际观测证据及一次性重启记录。
            boots = [p for p in result.readings.get(resource.resource_id, []) if p.metric == "boot_time"
                     and p.source == current_source and current_source in ("PROMETHEUS", "CES")
                     and now - timedelta(seconds=policy["stale_after_sec"]) <= p.observed_at <= now + timedelta(seconds=30)]
            if boots:
                boot = max(boots, key=lambda p: p.observed_at)
                old_boot = (resource.attrs or {}).get("boot_ts")
                old_at = (resource.attrs or {}).get("boot_observed_at")
                previous_at = parse_at(old_at)
                if (isinstance(old_boot, (int, float)) and not isinstance(old_boot, bool) and boot.value > old_boot + 60
                        and previous_at is not None and boot.value >= previous_at.timestamp() - 60):
                    ctx.db.add(ResourceChange(tenant_id=resource.tenant_id, resource_ref=resource.id,
                        resource_type=resource.resource_type, resource_id=resource.resource_id, name=resource.name,
                        change_type="REBOOT", source="INSPECT", operator="system", detected_at=boot.observed_at,
                        changes=[{"field": "boot_time", "label": "开机时间",
                                  "old": datetime.fromtimestamp(old_boot, UTC).isoformat(),
                                  "new": datetime.fromtimestamp(boot.value, UTC).isoformat()}]))
                attrs.update(boot_ts=int(boot.value), boot_time=datetime.fromtimestamp(boot.value, UTC).isoformat(),
                             boot_observed_at=boot.observed_at.isoformat())
            observations.append(Observation("ecs_server", resource.resource_id, resource.name, "metric_sample", {},
                                            attrs=attrs, metrics=metrics))
        ctx.stats.update(metrics_source=result.source, servers=len(resources),
                         with_metrics=len(accepted), sample_points=sum(len(v) for v in accepted.values()),
                         rejected_samples=rejected, metrics_diagnostic=result.diagnostic)
        return observations


class EcsQuotaCollector:
    code, service, name = "ecs_quota", "ecs", "ECS 项目配额采集"

    def collect(self, ctx: RunContext) -> list[Observation]:
        from app.services.ecs_quotas import refresh_quota
        from app.services.tenancy import Target

        if ctx.db is None or ctx.tenant is None or ctx.project_ref is None:
            ctx.errors.append("ECS 配额采集需要明确的租户与项目")
            return []
        project = ctx.db.get(HcsProject, ctx.project_ref)
        if project is None or project.account_id != ctx.tenant_id:
            ctx.errors.append("ECS 配额项目不属于当前租户")
            return []
        target = Target(tenant_id=ctx.tenant_id, code=ctx.account, name=ctx.tenant_name, short_name=ctx.tenant_name,
            project_ref=ctx.project_ref, project_id=ctx.project_id, project_name=project.project_name or project.project_id,
            region=ctx.region or project.region_id, tenant=ctx.tenant, project=project)
        result = refresh_quota(ctx.db, target, ctx.client)
        ctx.stats.update(quota_status=result.status, quota_items=len(result.quotas),
                         quota_fetched_at=result.fetched_at.isoformat() if result.fetched_at else None,
                         project_ref=ctx.project_ref)
        if result.last_error:
            ctx.errors.append("ECS 配额查询失败，已保留上次成功快照")
        return []
