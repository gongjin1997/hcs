"""按租户和类型聚合本地监控覆盖；不探测云、不改开关、不写库。"""
from collections import defaultdict
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import Scope
from app.core.response import BizError, ErrorCode
from app.metrics.dcs_samples import parse_observed_at
from app.models import HcsAccount, HcsProject, InspectionTask, Resource, SysSetting
from app.models.base import utcnow
from app.schemas.collection_policy import CollectionPolicy
from app.schemas.monitoring import (
    CoverageBlocker,
    CoverageState,
    MonitoringCoverageOut,
    MonitoringCoverageRow,
    MonitoringMetric,
    MonitoringResourceType,
)
from app.services import collect_switch, collection_policy
from app.services.inspection_health import ALARM_HEALTH, inspection_state
from app.services.resource_guides import get_resource_guide

RESOURCE_PERMISSIONS: dict[MonitoringResourceType, str] = {
    "ecs_server": "ledger:view", "dcs_instance": "report:view", "gaussdb_instance": "ledger:view"}
STATE_LABELS: dict[CoverageState, str] = {
    "tenant_disabled": "租户已停用", "collection_disabled": "采集已关闭", "no_projects": "未配置项目",
    "projects_disabled": "项目已停用", "inspection_disabled": "巡检已关闭", "configuration_invalid": "配置无效",
    "no_resources": "无资源", "no_eligible_resources": "无可巡检资源", "not_inspected": "未巡检",
    "stale": "巡检已过期", "partial": "部分覆盖", "covered": "巡检已覆盖",
}
BLOCKER_LABELS: dict[CoverageBlocker, str] = {
    "tenant_disabled": "租户已停用",
    "collection_disabled": "该资源类型的采集开关已关闭",
    "no_projects": "未配置项目",
    "projects_disabled": "全部项目已停用",
    "ledger_task_disabled": "台账同步任务未启用",
    "inspection_task_disabled": "巡检任务未启用",
    "metrics_task_disabled": "指标采集任务未启用",
    "ledger_phase_disabled": "台账同步阶段已关闭",
    "inspection_phase_disabled": "巡检阶段已关闭",
    "metrics_phase_disabled": "指标采集阶段已关闭",
    "invalid_policy": "采集策略配置无效",
    "invalid_schedule": "任务调度配置无效",
    "no_eligible_resources": "资源未纳管，或不在已启用项目与当前选择范围内",
}


def _schedule_valid(interval: int, cron: str | None) -> bool:
    if not cron:
        return interval > 0
    try:
        CronTrigger.from_crontab(cron, timezone="UTC")
    except ValueError:
        return False
    return True


def coverage(db: Session, scope: Scope, *, tenant_id: int | None = None,
             now: datetime | None = None) -> MonitoringCoverageOut:
    """六类批量读取；空租户不访问环境变量兜底目标，权限按资源类型独立过滤。"""
    if tenant_id is not None and not scope.allows(tenant_id):
        raise BizError(ErrorCode.FORBIDDEN, "无权访问该租户的数据", 403, {"reason": "tenant_scope"})
    now = now or utcnow()
    types = [code for code, permission in RESOURCE_PERMISSIONS.items() if scope.user.can(permission)]
    if not types:
        return MonitoringCoverageOut(evaluated_at=now, rows=[])
    tenant_query = scope.apply(select(HcsAccount.id, HcsAccount.name, HcsAccount.enabled), HcsAccount.id)
    if tenant_id is not None:
        tenant_query = tenant_query.where(HcsAccount.id == tenant_id)
    tenants = list(db.execute(tenant_query.order_by(HcsAccount.id)))
    if not tenants:
        return MonitoringCoverageOut(evaluated_at=now, rows=[])
    tids = [tenant.id for tenant in tenants]
    projects: dict[int, dict[int, bool]] = defaultdict(dict)
    for project in db.execute(select(HcsProject.id, HcsProject.account_id, HcsProject.enabled)
                              .where(HcsProject.account_id.in_(tids))):
        projects[project.account_id][project.id] = project.enabled
    switches = collect_switch.load(db)
    guides = {code: guide for code in types if (guide := get_resource_guide(code)) is not None}
    task_codes = {"ledger_sync", *(code for guide in guides.values() for code in guide.collection_tasks)}
    tasks = {task.code: task for task in db.execute(select(
        InspectionTask.code, InspectionTask.enabled, InspectionTask.interval_sec, InspectionTask.cron)
        .where(InspectionTask.code.in_(task_codes)))}
    # effective() 的历史兼容分支只覆写周期和映射；这里仅使用阶段 enabled 和 selection，
    # 共用其已有策略模型/默认值，避免每租户调用 effective() 或引入另一份配置。
    policy_rows: dict[str, dict] = dict(db.execute(select(SysSetting.key, SysSetting.value).where(
        SysSetting.key.in_([collection_policy.policy_key(tid) for tid in tids]))).all()) if "ecs_server" in types else {}
    resources: dict[tuple[int, str], list[Resource]] = defaultdict(list)
    # 只取巡检元信息；不加载资源指标、普通属性或租户认证字段。
    for item in db.execute(select(Resource.id, Resource.tenant_id, Resource.project_ref, Resource.resource_type,
            Resource.monitored, Resource.scope_excluded, Resource.critical, Resource.health,
            Resource.attrs["inspection_monitoring"].label("inspection_monitoring"))
            .where(Resource.tenant_id.in_(tids), Resource.resource_type.in_(types), Resource.lifecycle == "ACTIVE")):
        resources[(item.tenant_id, item.resource_type)].append(Resource(
            id=item.id, tenant_id=item.tenant_id, project_ref=item.project_ref, resource_type=item.resource_type,
            monitored=item.monitored, scope_excluded=item.scope_excluded, critical=item.critical, health=item.health,
            lifecycle="ACTIVE", attrs={"inspection_monitoring": item.inspection_monitoring}))
    rows = []
    for tenant in tenants:
        tenant_projects = projects[tenant.id]
        enabled_projects = sum(tenant_projects.values())
        for code, guide in guides.items():
            master = switches.enabled(tenant.id, code)
            blockers: list[CoverageBlocker] = []
            if not tenant.enabled:
                blockers.append("tenant_disabled")
            if not master:
                blockers.append("collection_disabled")
            if not tenant_projects:
                blockers.append("no_projects")
            elif not enabled_projects:
                blockers.append("projects_disabled")
            policy = CollectionPolicy.model_validate({})
            explicit_policy = code == "ecs_server" and collection_policy.policy_key(tenant.id) in policy_rows
            if explicit_policy:
                raw = policy_rows[collection_policy.policy_key(tenant.id)]
                try:
                    policy = CollectionPolicy.model_validate({**raw, "aom": None})
                except (ValidationError, TypeError):
                    blockers.append("invalid_policy")
            inspection_code = next((task for task in guide.collection_tasks
                                    if collection_policy.TASK_PHASES.get(task, "inspection") == "inspection"), "")
            metrics_code = next((task for task in guide.collection_tasks
                                 if collection_policy.TASK_PHASES.get(task) == "metrics"), inspection_code)

            def task_ready(task_code: str, *, independent_schedule: bool = False) -> bool:
                task = tasks.get(task_code)
                return bool(task is not None and task.enabled and (
                    independent_schedule or _schedule_valid(task.interval_sec, task.cron)))

            ledger_task = tasks.get("ledger_sync")
            inspection_task = tasks.get(inspection_code)
            ledger_ready = task_ready("ledger_sync")
            inspection_ready = task_ready(inspection_code, independent_schedule=explicit_policy)
            metrics_ready = task_ready(metrics_code, independent_schedule=explicit_policy)
            for task_code, reason in (("ledger_sync", "ledger_task_disabled"),
                                      (inspection_code, "inspection_task_disabled"),
                                      (metrics_code, "metrics_task_disabled")):
                task = tasks.get(task_code)
                if task is None or not task.enabled:
                    if reason not in blockers:
                        blockers.append(reason)  # type: ignore[arg-type]
            if ((ledger_task is not None and ledger_task.enabled and not ledger_ready)
                    or inspection_task is not None and inspection_task.enabled and not inspection_ready
                    or (task := tasks.get(metrics_code)) is not None and task.enabled and not metrics_ready):
                blockers.append("invalid_schedule")
            if code == "ecs_server":
                for phase, reason in (("inventory", "ledger_phase_disabled"),
                                      ("inspection", "inspection_phase_disabled"), ("metrics", "metrics_phase_disabled")):
                    if not getattr(policy, phase).enabled:
                        blockers.append(reason)  # type: ignore[arg-type]
            target_enabled = bool(tenant.enabled and master and enabled_projects and "invalid_policy" not in blockers)
            ledger_enabled = bool(target_enabled and ledger_ready and (code != "ecs_server" or policy.inventory.enabled))
            inspection_enabled = bool(target_enabled and inspection_ready
                                      and (code != "ecs_server" or policy.inspection.enabled))
            metrics_enabled = bool(target_enabled and metrics_ready and (code != "ecs_server" or policy.metrics.enabled))
            # 采集范围之外的资源是有意不巡检的：不计入覆盖率分母，也不算“未知”，单独给出数量
            items = [r for r in resources[(tenant.id, code)] if not r.scope_excluded]
            out_of_scope = len(resources[(tenant.id, code)]) - len(items)
            eligible = inspected = unknown = abnormal = partial = stale = eligible_stale = 0
            last_inspected = None
            for resource in items:
                evidence = inspection_state(resource, now)
                if evidence in ("FRESH", "PARTIAL", "STALE"):
                    at = parse_observed_at(resource.attrs["inspection_monitoring"].get("evaluated_at"))
                    if at is not None and (last_inspected is None or at > last_inspected):
                        last_inspected = at
                included = bool(inspection_enabled and resource.project_ref is not None
                                and tenant_projects.get(resource.project_ref, False)
                                and resource.monitored and (code != "ecs_server"
                                or collection_policy.resource_included(resource, policy)))
                eligible += included
                fresh = included and evidence == "FRESH"
                inspected += fresh
                known_abnormal = resource.health in ALARM_HEALTH
                abnormal += known_abnormal
                unknown += not fresh and not known_abnormal
                partial += included and evidence == "PARTIAL"
                stale += evidence == "STALE"
                eligible_stale += included and evidence == "STALE"
            state: CoverageState
            if not tenant.enabled:
                state = "tenant_disabled"
            elif not master:
                state = "collection_disabled"
            elif not tenant_projects:
                state = "no_projects"
            elif not enabled_projects:
                state = "projects_disabled"
            elif "invalid_policy" in blockers or "invalid_schedule" in blockers:
                state = "configuration_invalid"
            elif not inspection_enabled:
                state = "inspection_disabled"
            elif not ledger_enabled:
                state = "collection_disabled"
            elif not items:
                state = "no_resources"
            elif not eligible:
                state = "no_eligible_resources"
                blockers.append("no_eligible_resources")
            elif not inspected and not partial:
                state = "stale" if eligible_stale else "not_inspected"
            elif inspected != len(items) or enabled_projects != len(tenant_projects):
                state = "partial"
            else:
                # covered 仅描述巡检证据；指标源和实际样本由各资源面板另行判断。
                state = "covered"
            rows.append(MonitoringCoverageRow(
                tenant_id=tenant.id, tenant_name=tenant.name, resource_type=code, resource_name=guide.name,
                resources=len(items), out_of_scope_resources=out_of_scope, eligible_resources=eligible,
                inspected_resources=inspected,
                unknown_resources=unknown, abnormal_resources=abnormal, partial_resources=partial,
                stale_resources=stale, last_inspected_at=last_inspected, projects=len(tenant_projects),
                enabled_projects=enabled_projects, collection_enabled=master, ledger_enabled=ledger_enabled,
                inspection_enabled=inspection_enabled, metrics_enabled=metrics_enabled, tenant_enabled=tenant.enabled,
                ledger_task_enabled=bool(ledger_task and ledger_task.enabled),
                inspection_task_enabled=bool(inspection_task and inspection_task.enabled),
                metrics=[MonitoringMetric.from_guide(field) for field in guide.metrics], state=state,
                state_label=STATE_LABELS[state], blockers=blockers,
                blocker_labels=[BLOCKER_LABELS[blocker] for blocker in blockers], source_label=guide.summary))
    return MonitoringCoverageOut(evaluated_at=now, rows=rows)
