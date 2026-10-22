"""v0.4 演示数据（seed_demo 最后调用）：平台告警通知路由、系统设置、采集开关、静默 / 维护窗口、
平台自身告警（任务连续失败）、数据清理运行记录、台账 Excel 导入记录。

时间都相对“现在”计算（每周维护窗口按平台时区取“后天”的星期，保证演示时处于“等待下次窗口”状态）。
"""
import logging
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.ledger.importer import LABEL
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AuditLog,
    CollectSwitch,
    HcsAccount,
    InspectionRun,
    Notification,
    NotifyRoute,
    Resource,
    ResourceChange,
    Silence,
    SysSetting,
    User,
)
from app.models.base import utcnow

from . import collect_switch, platform_watch, settings_service, silence_service
from .cleanup import CLEANUP_TASK
from .pipeline import run_cleanup_task
from .seed_history import IPS, message_id

log = logging.getLogger(__name__)
WEEKDAY = "一二三四五六日"
# 租户B 没有开通 DataArts / MRS / GaussDB / DWS：关闭采集后这些巡检对租户B 不再执行
OFF_B = ["dgc_realtime_job", "dgc_batch_job", "mrs_cluster", "mrs_job", "gaussdb_instance", "dws_cluster"]
MRS_TIMEOUT = ("[504] APIGW.0201: Backend timeout（MRS 作业接口 /v2/{project_id}/clusters/{cluster_id}/job-executions，"
               "已重试 3 次）")
OFF_B_TASKS = ["dgc_realtime_status", "dgc_batch_instance", "mrs_cluster_status", "mrs_job_status", "gaussdb_status"]
BIZ_REMARK = {"电商交易": "订单、支付、库存", "电商门户": "PC / H5 门户", "数据平台": "实时 + 离线数仓",
              "容器平台": "CCE 集群与公共组件", "运维监控": "Prometheus / 日志", "测试环境": "非生产，告警降级"}


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _local(day: datetime, hh: int, mm: int = 0) -> datetime:
    """day 所在的本地日期的 hh:mm（返回带时区的时间）。"""
    d = day.astimezone(_tz()).date()
    return datetime.combine(d, time(hh, mm), _tz())


def _audit(db: Session, users: dict[str, User], at: datetime, username: str, action: str, *,
           target_type: str | None = None, target_id: object = None, target_name: str | None = None,
           tenant_id: int | None = None, detail: dict | None = None) -> None:
    u = users.get(username)
    db.add(AuditLog(at=at, user_id=u.id if u else None, operator=u.display_name if u else username, username=username,
                    action=action, target_type=target_type, target_id=None if target_id is None else str(target_id),
                    target_name=target_name, tenant_id=tenant_id, result="SUCCESS", detail=detail or {},
                    ip=IPS.get(username, "10.20.8.66")))


def _alarm(db: Session, tenant_id: int, name: str, rule_id: str) -> AlarmEvent | None:
    return db.scalar(select(AlarmEvent).where(AlarmEvent.tenant_id == tenant_id, AlarmEvent.resource_name == name,
                                              AlarmEvent.rule_id == rule_id, AlarmEvent.status == "FIRING"))


def _resource(db: Session, tenant_id: int, rtype: str, name: str) -> Resource | None:
    return db.scalar(select(Resource).where(Resource.tenant_id == tenant_id, Resource.resource_type == rtype,
                                            Resource.name == name, Resource.lifecycle == "ACTIVE"))


# ---------------------------------------------------------------- 通知路由：平台自身告警
def _route(db: Session) -> None:
    db.add(NotifyRoute(name="平台自身告警 → 运维负责人", priority=1, match={"services": [platform_watch.PLATFORM]},
                       group_code="leaders", escalation=[], continue_match=False, enabled=True))
    db.flush()


# ---------------------------------------------------------------- 系统设置
def _settings(db: Session, users: dict[str, User], now: datetime) -> None:
    biz = sorted({x for x in db.scalars(select(Resource.business).where(Resource.business.is_not(None))) if x})
    owners = sorted({x for x in db.scalars(select(Resource.owner).where(Resource.owner.is_not(None))) if x})
    report = settings_service.get_report(db)
    items: list[tuple[str, dict, int]] = [
        ("report", {"cron": report.cron, "enabled": report.enabled,
                    "mail_to": ["ops-report@example.com", "zhaolei@example.com"]}, 14),
        ("platform_alarm", {"mail_to": ["zhaolei@example.com"]}, 14),
        ("ledger_dict", {"business": [{"name": n, "remark": BIZ_REMARK.get(n)} for n in biz],
                         "owners": [{"name": n} for n in owners], "enforce": False}, 11),
    ]
    for key, data, days in items:
        _, diff = settings_service.save(db, key, data, operator="赵磊")
        at = now - timedelta(days=days, hours=2, minutes=17)
        row = db.get(SysSetting, key)
        if row is not None:
            row.updated_at = at
        _audit(db, users, at, "zhaolei", "setting.update", target_type="setting", target_id=key,
               target_name=settings_service.LABELS[key], detail={"changes": diff})
    db.flush()


# ---------------------------------------------------------------- 采集开关
def _switches(db: Session, b: HcsAccount, users: dict[str, User], now: datetime) -> None:
    at = now - timedelta(days=9, hours=5, minutes=12)
    res = collect_switch.update(db, [{"resource_type": t, "tenant_id": b.id, "enabled": False} for t in OFF_B], "赵磊")
    for row in db.scalars(select(CollectSwitch).where(CollectSwitch.tenant_id == b.id)):
        row.updated_at = at
    _audit(db, users, at, "zhaolei", "collect.update", target_type="collect_switch", target_name=b.short_name or b.name,
           tenant_id=b.id, detail={"changed": [{"type": c["name"], "tenant": b.short_name or b.name, "old": "跟随全局",
                                                "new": "关闭"} for c in res["changed"]],
                                   "closed_alarms": res["closed_alarms"],
                                   "reason": "租户B 未开通 DataArts / MRS / GaussDB / DWS，避免空跑和无效调用"})
    # 关闭之后租户B 不会再有这些任务的运行记录（演示历史是按“全部开启”生成的，这里删掉关闭之后的部分）
    db.execute(delete(InspectionRun).where(InspectionRun.tenant_id == b.id, InspectionRun.task_code.in_(OFF_B_TASKS),
                                           InspectionRun.started_at >= at))
    db.flush()


# ---------------------------------------------------------------- 静默 / 维护窗口
def _silence(db: Session, users: dict[str, User], *, name: str, kind: str, matchers: dict, starts: datetime,
             ends: datetime, reason: str, username: str, created: datetime, recurrence: dict | None = None,
             tenant_id: int | None = None, audit_action: str = "silence.create", audit_target: dict | None = None,
             names: dict[int, str]) -> Silence:
    u = users[username]
    s = Silence(name=name, kind=kind, matchers=silence_service.normalize_matchers(matchers),
                recurrence=silence_service.normalize_recurrence(recurrence), starts_at=starts, ends_at=ends,
                reason=reason, created_by=u.display_name, created_at=created, updated_at=created)
    db.add(s)
    db.flush()
    now = utcnow()
    # 创建时已生效的立即抑制；提前创建的在开始时刻抑制（通知进程每分钟扫描一次）
    active = silence_service.is_active(s, now)
    res = silence_service.sweep(db, max(starts, created), only=s) if active else {"suppressed": 0}
    detail = {"kind": kind, "matchers": silence_service.describe(s.matchers, names),
              "window": f"{starts.isoformat()} ~ {ends.isoformat()}", "recurrence": s.recurrence,
              "suppressed": res["suppressed"], "silence_id": s.id}
    tgt = audit_target or {"target_type": "silence", "target_id": s.id, "target_name": name}
    _audit(db, users, created, username, audit_action, tenant_id=tenant_id, detail=detail, **tgt)
    return s


def _silences(db: Session, a: HcsAccount, b: HcsAccount, users: dict[str, User], now: datetime) -> None:
    names = {t.id: t.short_name or t.name for t in db.scalars(select(HcsAccount))}
    # 1) 告警详情里“静默 4 小时”（只针对这一条告警）
    al = _alarm(db, a.id, "ecs-report-01", "R-ECS-006")
    if al is not None:
        at = now - timedelta(minutes=40)
        _silence(db, users, name=f"静默告警：{al.resource_name} · ECS 磁盘使用率过高", kind="SILENCE",
                 matchers={"fingerprints": [al.fingerprint]}, starts=at, ends=at + timedelta(hours=4),
                 reason="报表导出目录正在清理，预计 2 小时内完成，期间不再重复通知", username="oncall", created=at,
                 tenant_id=a.id, audit_action="alarm.silence", names=names,
                 audit_target={"target_type": "alarm", "target_id": al.id, "target_name": al.title})
    # 2) 维护窗口：租户B ecs-batch-01 内核升级（指定资源）
    r = _resource(db, b.id, "ecs_server", "ecs-batch-01")
    if r is not None:
        at = now - timedelta(minutes=25)
        _silence(db, users, name="ecs-batch-01 内核升级与重启", kind="MAINTENANCE",
                 matchers={"tenant_ids": [b.id], "resource_refs": [r.id]}, starts=at, ends=at + timedelta(hours=2),
                 reason="变更单 CHG-20261022-017：内核补丁升级，期间可能重启、内存升高", username="wangfang",
                 created=at - timedelta(hours=3), tenant_id=b.id, names=names)
    # 3) 每周重复的变更窗口：租户A P3 / P4 告警在窗口内不通知（P1 / P2 照常通知）
    day = (now.astimezone(_tz()) + timedelta(days=2)).isoweekday()
    created = now - timedelta(days=24, hours=6)
    s = _silence(db, users, name=f"租户A 例行变更窗口（每周{WEEKDAY[day - 1]} 22:00–次日 02:00）", kind="MAINTENANCE",
                 matchers={"tenant_ids": [a.id], "severities": ["P3", "P4"]}, starts=created,
                 ends=created + timedelta(days=180), recurrence={"weekdays": [day], "start": "22:00", "end": "02:00"},
                 reason="每周固定发布窗口：P3 / P4 告警在窗口内只记录不通知，窗口结束后仍未恢复的补发通知",
                 username="zhaolei", created=created, tenant_id=a.id, names=names)
    s.suppressed_total = 9
    # 4) 未开始：租户B 门户全链路压测（后天 20:00 ~ 24:00）
    start = _local(now + timedelta(days=2), 20)
    _silence(db, users, name="租户B 门户全链路压测", kind="MAINTENANCE",
             matchers={"tenant_ids": [b.id], "services": ["ecs", "cce"], "severities": ["P3", "P4"]},
             starts=start, ends=start + timedelta(hours=4),
             reason="双 11 前压测，ECS CPU / 内存与容器重启类低级别告警预期会触发", username="wangfang",
             created=now - timedelta(hours=20), tenant_id=b.id, names=names)
    # 5) 已结束：GaussDB 小版本升级（5 天前 23:00 ~ 次日 02:00）
    start = _local(now - timedelta(days=5), 23)
    s = _silence(db, users, name="GaussDB 小版本升级（gs-core-01 / gs-crm-01）", kind="MAINTENANCE",
                 matchers={"tenant_ids": [a.id], "services": ["gaussdb"]}, starts=start, ends=start + timedelta(hours=3),
                 reason="变更单 CHG-20261017-006：主备切换期间实例状态、复制延迟告警预期会触发", username="lina",
                 created=start - timedelta(days=1, hours=4), tenant_id=a.id, names=names)
    s.suppressed_total = 4
    # 6) 已取消：DCS 扩容改期
    start = _local(now - timedelta(days=3), 22)
    r = _resource(db, a.id, "dcs_instance", "redis-prod-01")
    s = _silence(db, users, name="redis-prod-01 扩容（16G → 32G）", kind="MAINTENANCE",
                 matchers={"tenant_ids": [a.id], "resource_refs": [r.id] if r else [], "services": ["dcs"]},
                 starts=start, ends=start + timedelta(hours=2), reason="变更单 CHG-20261019-002：DCS 规格变更",
                 username="lina", created=start - timedelta(days=1), tenant_id=a.id, names=names)
    s.cancelled_at, s.cancelled_by = start - timedelta(hours=3, minutes=50), "李娜"
    _audit(db, users, s.cancelled_at, "lina", "silence.cancel", target_type="silence", target_id=s.id,
           target_name=s.name, tenant_id=a.id, detail={"released": 0, "reason": "业务方要求改期到下周"})
    db.flush()


# ---------------------------------------------------------------- 平台自身告警：MRS 作业巡检连续失败
def _platform_alarm(db: Session, a: HcsAccount, now: datetime) -> None:
    # 最近 3 次（每 5 分钟一次）都失败；之前的成功运行挪到失败之前
    for r in db.scalars(select(InspectionRun).where(InspectionRun.task_code == "mrs_job_status",
                                                    InspectionRun.tenant_id == a.id,
                                                    InspectionRun.started_at >= now - timedelta(minutes=16))):
        cost = (r.finished_at - r.started_at) if r.finished_at else timedelta(seconds=3)
        r.started_at = now - timedelta(minutes=19, seconds=12)
        r.finished_at = r.started_at + cost
    last = None
    for i, ago in enumerate((14, 9, 4)):
        start = now - timedelta(minutes=ago)
        last = InspectionRun(task_code="mrs_job_status", tenant_id=a.id, trigger_type="SCHEDULE", started_at=start,
                             finished_at=start + timedelta(seconds=31 + i), status="FAILED", api_calls=4, stats={},
                             error_summary=MRS_TIMEOUT)
        db.add(last)
    db.commit()
    assert last is not None
    platform_watch.on_run(db, last)
    alarm = db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001",
                                               AlarmEvent.status.in_(("FIRING", "SUPPRESSED"))))
    if alarm is None:
        return
    at = last.finished_at or now
    alarm.first_seen = alarm.last_seen = at
    for lg in db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id)):
        lg.at = at
    for n in db.scalars(select(Notification).where(Notification.alarm_id == alarm.id)):
        n.created_at = at + timedelta(seconds=3)
        n.status, n.sent_at, n.message_id = "SENT", at + timedelta(seconds=8), message_id()
    db.commit()


# ---------------------------------------------------------------- 数据清理：昨天 03:30 + 之前 13 天的运行记录
def _cleanup(db: Session, now: datetime) -> None:
    run = run_cleanup_task(db, trigger="SCHEDULE")
    rng = random.Random(20261022)
    yesterday = _local(now - timedelta(days=1), 3, 30)
    cost = (run.finished_at - run.started_at) if run.finished_at else timedelta(seconds=2)
    run.started_at, run.finished_at = yesterday, yesterday + cost + timedelta(seconds=4)

    def stats() -> dict[str, int]:
        out = {k: 0 for k in run.stats or {}}
        out.update({"pod_event": rng.randint(18, 64), "pod_state": rng.randint(0, 3), "eval_state": rng.randint(0, 6)})
        return out

    run.stats = stats()
    run.resource_total = sum(run.stats.values())
    for d in range(2, 15):
        start = _local(now - timedelta(days=d), 3, 30) + timedelta(seconds=rng.randint(0, 3))
        st = stats()
        db.add(InspectionRun(task_code=CLEANUP_TASK, tenant_id=None, trigger_type="SCHEDULE", started_at=start,
                             finished_at=start + timedelta(seconds=rng.randint(3, 9)), status="SUCCESS",
                             stats=st, resource_total=sum(st.values()), api_calls=0))
    db.commit()


# ---------------------------------------------------------------- 台账 Excel 导入（批量补齐负责人 / 业务系统）
def _import(db: Session, a: HcsAccount, users: dict[str, User], now: datetime) -> None:
    at = now - timedelta(days=10, hours=1, minutes=48)
    rows = list(db.scalars(select(Resource).where(Resource.tenant_id == a.id, Resource.lifecycle == "ACTIVE",
                                                  Resource.resource_type.in_(("ecs_server", "rds_instance")),
                                                  Resource.owner.is_not(None)).order_by(Resource.name).limit(6)))
    for i, r in enumerate(rows):
        changes = [{"field": "owner", "label": LABEL["owner"], "old": None if i % 2 else "陈静", "new": r.owner}]
        if i < 3 and r.business:
            changes.append({"field": "business", "label": LABEL["business"], "old": None, "new": r.business})
        db.add(ResourceChange(tenant_id=r.tenant_id, resource_ref=r.id, resource_type=r.resource_type,
                              resource_id=r.resource_id, name=r.name, change_type="UPDATED", changes=changes,
                              source="IMPORT", operator="赵磊", detected_at=at))
    _audit(db, users, at, "zhaolei", "ledger.import", target_type="resource", target_name="台账管理字段-租户A.xlsx",
           tenant_id=a.id, detail={"file": "台账管理字段-租户A.xlsx", "rows": 42, "updated": len(rows),
                                   "unchanged": 42 - len(rows) - 2, "errors": 2, "skip_errors": True})
    db.flush()


def seed_v04(db: Session, tenants: dict[str, HcsAccount], users: dict[str, User]) -> None:
    now = utcnow()
    a, b = tenants["tenant-a"], tenants["tenant-b"]
    _route(db)
    _settings(db, users, now)
    _switches(db, b, users, now)
    _import(db, a, users, now)
    db.commit()
    _silences(db, a, b, users, now)
    db.commit()
    _platform_alarm(db, a, now)
    _cleanup(db, utcnow())
    log.info("v0.4 demo data seeded: switches, silences, platform alarm, cleanup")
