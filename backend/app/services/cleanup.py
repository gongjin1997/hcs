"""数据清理（任务代码 data_cleanup，默认每天 03:30）：按“系统设置 → 数据保留”删除过期数据。

- 分批删除（每批 2000 行），避免长事务锁表；每张表的删除行数记入运行记录（巡检任务页可查看）
- 活动告警（告警中 / 已认领 / 已抑制）、在用资源、未结束的容器事件不会被删除
- 审计日志保留期不能小于 90 天（建议 180 天以上）；需要长期留存的请定期导出归档
- 本地 DCS 内存样本固定保留 90 天；当前 retention 没有 metric_snapshot 配置键，不新增共享设置项
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AuditLog,
    DcsMemorySample,
    DcsMetricSample,
    EcsMetricSample,
    EvalState,
    GaussDBStorageSample,
    InspectionRun,
    Notification,
    PodEvent,
    PodState,
    Report,
    Resource,
    ResourceChange,
    Silence,
)
from app.models.base import utcnow

from . import settings_service

CLEANUP_TASK = "data_cleanup"
BATCH = 2000
DCS_MEMORY_RETENTION_DAYS = 90
ECS_METRIC_RETENTION_DAYS = 90
GAUSSDB_STORAGE_RETENTION_DAYS = 90
log = logging.getLogger(__name__)


def _purge(db: Session, model, id_col, *conds) -> int:
    total = 0
    while True:
        ids = list(db.scalars(select(id_col).where(*conds).limit(BATCH)))
        if not ids:
            return total
        db.execute(delete(model).where(id_col.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < BATCH:
            return total


def run_cleanup(db: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    r = settings_service.get_retention(db)
    ago = lambda days: now - timedelta(days=days)  # noqa: E731
    out: dict[str, int] = {}
    out["inspection_run"] = _purge(db, InspectionRun, InspectionRun.id, InspectionRun.started_at < ago(r.inspection_run_days),
                                   InspectionRun.status != "RUNNING")
    out["notification"] = _purge(db, Notification, Notification.id, Notification.created_at < ago(r.notification_days),
                                 Notification.status.in_(("SENT", "FAILED", "MERGED")))
    # 已恢复 / 已关闭的告警：先删处理时间线和通知，再删告警本身
    old_alarm = (AlarmEvent.status.in_(("RESOLVED", "CLOSED")), AlarmEvent.last_seen < ago(r.alarm_closed_days))
    alarm_ids = select(AlarmEvent.id).where(*old_alarm)
    out["alarm_event_log"] = _purge(db, AlarmEventLog, AlarmEventLog.id, AlarmEventLog.alarm_id.in_(alarm_ids))
    _purge(db, Notification, Notification.id, Notification.alarm_id.in_(alarm_ids))
    out["alarm_event"] = _purge(db, AlarmEvent, AlarmEvent.id, *old_alarm)
    out["audit_log"] = _purge(db, AuditLog, AuditLog.id, AuditLog.at < ago(r.audit_log_days))
    out["resource_change"] = _purge(db, ResourceChange, ResourceChange.id,
                                    ResourceChange.detected_at < ago(r.resource_change_days))
    released = (Resource.lifecycle == "DELETED", Resource.deleted_at < ago(r.released_resource_days))
    out["dcs_memory_sample"] = _purge(db, DcsMemorySample, DcsMemorySample.id,
                                     DcsMemorySample.observed_at < ago(DCS_MEMORY_RETENTION_DAYS))
    # 先清理待删除资源的样本；同时清除 SQLite 未启用外键时留下的孤儿，不能依赖运行时外键开关。
    out["dcs_memory_sample"] += _purge(db, DcsMemorySample, DcsMemorySample.id, or_(
        DcsMemorySample.resource_ref.in_(select(Resource.id).where(*released)),
        DcsMemorySample.resource_ref.not_in(select(Resource.id))))
    out["dcs_metric_sample"] = _purge(db, DcsMetricSample, DcsMetricSample.id,
                                     DcsMetricSample.observed_at < ago(DCS_MEMORY_RETENTION_DAYS))
    out["dcs_metric_sample"] += _purge(db, DcsMetricSample, DcsMetricSample.id, or_(
        DcsMetricSample.resource_ref.in_(select(Resource.id).where(*released)),
        DcsMetricSample.resource_ref.not_in(select(Resource.id))))
    out["ecs_metric_sample"] = _purge(db, EcsMetricSample, EcsMetricSample.id,
                                     EcsMetricSample.observed_at < ago(ECS_METRIC_RETENTION_DAYS))
    out["ecs_metric_sample"] += _purge(db, EcsMetricSample, EcsMetricSample.id, or_(
        EcsMetricSample.resource_ref.in_(select(Resource.id).where(*released)),
        EcsMetricSample.resource_ref.not_in(select(Resource.id))))
    out["gaussdb_storage_sample"] = _purge(db, GaussDBStorageSample, GaussDBStorageSample.id,
        GaussDBStorageSample.observed_at < ago(GAUSSDB_STORAGE_RETENTION_DAYS))
    out["gaussdb_storage_sample"] += _purge(db, GaussDBStorageSample, GaussDBStorageSample.id, or_(
        GaussDBStorageSample.resource_ref.in_(select(Resource.id).where(*released)),
        GaussDBStorageSample.resource_ref.not_in(select(Resource.id))))
    _purge(db, ResourceChange, ResourceChange.id, ResourceChange.resource_ref.in_(select(Resource.id).where(*released)))
    out["resource"] = _purge(db, Resource, Resource.id, *released)
    out["report"] = _purge(db, Report, Report.id, Report.created_at < ago(r.report_days))
    out["pod_event"] = _purge(db, PodEvent, PodEvent.id, PodEvent.ongoing.is_(False),
                              PodEvent.last_time < ago(r.pod_event_days))
    out["pod_state"] = _purge(db, PodState, PodState.key, PodState.deleted.is_(True),
                              PodState.last_seen < ago(r.pod_event_days))
    out["alarm_silence"] = _purge(db, Silence, Silence.id, Silence.ends_at < ago(r.alarm_closed_days))
    # 规则评估状态：30 天没有再评估的指纹（资源已释放 / 规则已删除）
    out["eval_state"] = _purge(db, EvalState, EvalState.fingerprint, EvalState.updated_at < ago(30))
    log.info("data cleanup: %s", ", ".join(f"{k}={v}" for k, v in out.items() if v) or "nothing to delete")
    return out
