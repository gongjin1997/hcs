"""演示数据：历史记录（台账变更、已恢复告警、容器事件、巡检运行、操作审计、报表）。

seed.py 先用 Mock HCS 跑出“当前”状态（活动告警、最新台账、Pod 状态），这里再往前补一段历史：
从上个月 1 日开始（月报有完整月份），让趋势图、日报 / 周报 / 月报和环比、Pod 时间线、操作审计都有数据。
历史告警用真实规则（app.rules.model.Rule）计算级别、标题、阈值文本，通知收件人走真实路由；随机数固定种子，结果可复现。
"""
import random
import re
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.core.config import get_settings
from app.k8s.catalog import EVENT_TYPES
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AlarmRule,
    AuditLog,
    Contact,
    ContactGroup,
    HcsAccount,
    HcsProject,
    InspectionRun,
    Notification,
    PodEvent,
    PodProbe,
    PodState,
    Report,
    Resource,
    ResourceChange,
    User,
)
from app.notify.escalation import escalate, escalation_plan
from app.notify.render import build_subject
from app.notify.routing import resolve_recipients
from app.reports.service import default_recipients, generate
from app.rules.expr import evaluate
from app.rules.model import Rule, render_title
from app.services.catalog import RESOURCE_TYPES


# ---------------------------------------------------------------- 时间工具
def history_start(now: datetime) -> datetime:
    """演示历史起点：上个月 1 日 00:00（本地时区）。"""
    tz = ZoneInfo(get_settings().timezone)
    first = now.astimezone(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first - timedelta(days=1)).replace(day=1).astimezone(UTC)


def from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(int(ts), UTC)


def iso_ts(ts: float) -> str:
    """与 ECS 巡检记录开机时间的格式一致。"""
    return from_ts(ts).isoformat(timespec="seconds")


def message_id() -> str:
    return f"<demo-{uuid.uuid4().hex[:20]}@hcs-inspector>"


def retime_created(db: Session, start: datetime, now: datetime) -> None:
    """首次同步把全部存量资源都记成了“新增”：云上创建早于历史起点的删掉这条记录，其余按创建时间回拨发现时间。"""
    rng = random.Random(11)
    res = {r.id: r for r in db.scalars(select(Resource))}
    for ch in db.scalars(select(ResourceChange).where(ResourceChange.change_type == "CREATED")).all():
        r = res.get(ch.resource_ref)
        created = r.created_time if r is not None else None
        if r is None or created is None or created < start:
            db.delete(ch)
            continue
        ch.detected_at = min(created + timedelta(minutes=rng.randint(6, 55)), now - timedelta(minutes=3))
        r.first_seen = ch.detected_at
    db.commit()


@dataclass
class Ctx:
    db: Session
    tenants: dict[str, HcsAccount]
    users: dict[str, User]
    start: datetime
    now: datetime
    rng: random.Random
    tz: ZoneInfo
    projects: dict[int, HcsProject]
    rules: dict[str, AlarmRule]
    alarms: list[AlarmEvent] = field(default_factory=list)
    owner_changes: dict[int, tuple[datetime, str]] = field(default_factory=dict)
    ack_notes: dict[int, str] = field(default_factory=dict)
    firing_titles: dict[int, str] = field(default_factory=dict)

    def at(self, days: float, hh: int, mm: int = 0) -> datetime:
        """days 天前（本地日期）的 hh:mm，换算成 UTC；不晚于当前时间前 5 分钟。"""
        d = (self.now.astimezone(self.tz) - timedelta(days=days)).date()
        t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=self.tz).astimezone(UTC)
        return min(t, self.now - timedelta(minutes=5))

    def local(self, d: date, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
        return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=self.tz).astimezone(UTC)

    def res(self, code: str, name: str, rtype: str | None = None) -> Resource | None:
        q = select(Resource).where(Resource.tenant_id == self.tenants[code].id, Resource.name == name)
        if rtype:
            q = q.where(Resource.resource_type == rtype)
        return self.db.scalars(q.limit(1)).first()

    def user(self, username: str) -> User | None:
        return self.users.get(username)

    def name(self, username: str, default: str = "system") -> str:
        u = self.users.get(username)
        return u.display_name if u is not None else default

    def project(self, tenant: HcsAccount) -> HcsProject | None:
        return next((p for p in self.projects.values() if p.account_id == tenant.id), None)


def _pick(rng: random.Random, v):
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, int | float) for x in v):
        lo, hi = v
        return rng.randint(lo, hi) if isinstance(lo, int) and isinstance(hi, int) else round(rng.uniform(lo, hi), 1)
    return v


def _scale_last(spec: str | None, factor: float) -> str | None:
    """把规格文本里最后一个数字按比例换算，例如 “Redis 5.0 | 32 GB” × 0.5 → “Redis 5.0 | 16 GB”。"""
    if not spec:
        return spec
    nums = list(re.finditer(r"\d+(?:\.\d+)?", spec))
    if not nums:
        return spec
    m = nums[-1]
    return f"{spec[:m.start()]}{float(m.group()) * factor:g}{spec[m.end():]}"


def _change(ctx: Ctx, r: Resource, kind: str, at: datetime, changes: list | None = None, source: str = "SYNC",
            operator: str = "system") -> None:
    ctx.db.add(ResourceChange(tenant_id=r.tenant_id, resource_ref=r.id, resource_type=r.resource_type,
                              resource_id=r.resource_id, name=r.name, change_type=kind, changes=changes or [],
                              source=source, operator=operator, detected_at=at))


def _audit(ctx: Ctx, at: datetime, username: str | None, action: str, *, target_type: str | None = None,
           target_id: object = None, target_name: str | None = None, tenant_id: int | None = None,
           result: str = "SUCCESS", detail: dict | None = None, operator: str | None = None) -> None:
    u = ctx.users.get(username or "")
    ctx.db.add(AuditLog(at=at, user_id=u.id if u else None, operator=operator or (u.display_name if u else username or "-"),
                        username=username, action=action, target_type=target_type,
                        target_id=None if target_id is None else str(target_id), target_name=target_name,
                        tenant_id=tenant_id, result=result, detail=detail or {}, ip=IPS.get(username or "", "10.20.8.66")))


IPS = {"admin": "10.20.8.10", "zhaolei": "10.20.8.15", "oncall": "10.20.8.31", "lina": "10.20.8.42",
       "wangfang": "10.30.2.17", "viewer": "10.20.9.88", "chenjing": "10.20.8.27"}


# ---------------------------------------------------------------- 1. 台账变更
# (租户, 类型, 名称, 几天前, 时, 分, 变更方式)
SPEC_CHANGES: list[tuple[str, str, str, int, int, int, str]] = [
    ("tenant-a", "ecs_server", "ecs-app-03", 12, 22, 40, "ecs"),          # 变更窗口内升配
    ("tenant-a", "evs_volume", "ecs-etl-02-data-01", 20, 21, 10, "storage"),
    ("tenant-a", "ecs_server", "ecs-etl-02", 20, 21, 10, "ecs_disk"),
    ("tenant-a", "dcs_instance", "redis-cache-02", 9, 23, 5, "memory"),
    ("tenant-a", "gaussdb_instance", "gs-report-01", 25, 22, 30, "storage"),
    ("tenant-a", "eip", "eip-elb-web", 33, 20, 0, "spec"),
    ("tenant-b", "ecs_server", "ecs-portal-02", 6, 23, 20, "ecs"),
]
# 已释放的资源：(租户, 类型, 名称, 创建于几天前, 释放于几天前, 台账字段, (业务系统, 负责人), 备注)
GONE: list[tuple[str, str, str, int, int, dict[str, Any], tuple[str, str], str]] = [
    ("tenant-a", "ecs_server", "ecs-tmp-build-01", 18, 16,
     {"spec": "4 vCPU | 8 GB", "vcpus": 4, "memory_gb": 8.0, "storage_gb": 100.0, "ip": "192.168.20.88",
      "status_raw": "ACTIVE"}, ("运维管理", "赵磊"), "临时构建机，版本发布后释放"),
    ("tenant-a", "evs_volume", "vol-migrate-tmp-02", 75, 9,
     {"spec": "SAS | 500 GB", "storage_gb": 500.0, "status_raw": "available"}, ("基础设施", "陈静"), "数据迁移临时盘"),
    ("tenant-a", "eip", "eip-campaign-01", 14, 7,
     {"spec": "50 Mbit/s", "ip": "100.93.10.57", "status_raw": "DOWN"}, ("电商门户", "周敏"), "国庆大促临时 EIP"),
    ("tenant-b", "ecs_server", "ecs-portal-old-01", 180, 11,
     {"spec": "4 vCPU | 8 GB", "vcpus": 4, "memory_gb": 8.0, "storage_gb": 140.0, "ip": "10.60.2.21",
      "status_raw": "SHUTOFF"}, ("门户网站", "王芳"), "旧版门户，迁移到 CCE 后下线"),
    ("tenant-b", "eip", "eip-web-campaign", 20, 13,
     {"spec": "30 Mbit/s", "ip": "100.94.22.18", "status_raw": "DOWN"}, ("门户网站", "王芳"), "活动临时 EIP"),
]
RUN_LABEL = {True: "应保持运行", False: "允许关机"}
# 页面上的人工维护：(租户, 类型, 名称, 几天前, 时, 分, 操作人, [(字段, 标签, 旧值, 新值)])
MANUAL: list[tuple[str, str, str, int, int, int, str, list[tuple[str, str, Any, Any]]]] = [
    ("tenant-a", "gaussdb_instance", "gs-core-01", 30, 16, 40, "zhaolei",
     [("remark", "备注", None, "核心交易库，变更走 CAB 审批"), ("critical", "核心资源", False, True)]),
    ("tenant-a", "ecs_server", "ecs-test-02", 26, 15, 20, "oncall",
     [("expected_state.running", "预期状态", RUN_LABEL[True], RUN_LABEL[False])]),
    ("tenant-a", "ecs_server", "ecs-dev-01", 26, 15, 22, "oncall",
     [("expected_state.running", "预期状态", RUN_LABEL[True], RUN_LABEL[False])]),
    ("tenant-b", "cce_cluster", "cce-web-01", 15, 22, 30, "wangfang", [("critical", "核心资源", False, True)]),
    ("tenant-b", "ecs_server", "ecs-stage-01", 12, 11, 5, "wangfang",
     [("expected_state.running", "预期状态", RUN_LABEL[True], RUN_LABEL[False])]),
    ("tenant-a", "ecs_server", "ecs-api-gw-01", 8, 14, 0, "oncall", [("remark", "备注", None, "API 网关主节点（双活）")]),
    ("tenant-a", "ecs_server", "ecs-report-01", 4, 10, 12, "zhaolei", [("owner", "负责人", "陈静", "李娜")]),
]


def _ledger_history(ctx: Ctx) -> None:
    db = ctx.db
    for code, rtype, name, days, hh, mm, how in SPEC_CHANGES:
        r = ctx.res(code, name, rtype)
        if r is None:
            continue
        at = ctx.at(days, hh, mm) + timedelta(minutes=ctx.rng.randint(8, 40))
        ch: list[dict] = []
        if how == "ecs" and r.vcpus and r.memory_gb:
            ov, om = max(1, r.vcpus // 2), r.memory_gb / 2
            ch = [{"field": "spec", "label": "规格", "old": f"{ov} vCPU | {om:g} GB", "new": r.spec},
                  {"field": "vcpus", "label": "vCPU", "old": ov, "new": r.vcpus},
                  {"field": "memory_gb", "label": "内存(GB)", "old": om, "new": r.memory_gb}]
        elif how == "storage" and r.storage_gb:
            ch = [{"field": "spec", "label": "规格", "old": _scale_last(r.spec, 0.5), "new": r.spec},
                  {"field": "storage_gb", "label": "存储(GB)", "old": r.storage_gb / 2, "new": r.storage_gb}]
        elif how == "ecs_disk" and r.storage_gb:
            ch = [{"field": "storage_gb", "label": "存储(GB)", "old": r.storage_gb - 500, "new": r.storage_gb}]
        elif how == "memory" and r.memory_gb:
            ch = [{"field": "spec", "label": "规格", "old": _scale_last(r.spec, 0.5), "new": r.spec},
                  {"field": "memory_gb", "label": "内存(GB)", "old": r.memory_gb / 2, "new": r.memory_gb}]
        elif how == "spec":
            ch = [{"field": "spec", "label": "规格", "old": _scale_last(r.spec, 0.5), "new": r.spec}]
        if ch:
            _change(ctx, r, "UPDATED", at, ch)
    for code, rtype, name, born, gone, fields, (business, owner), remark in GONE:
        tenant = ctx.tenants[code]
        proj = ctx.project(tenant)
        created = ctx.at(born, 10, 5)
        deleted = ctx.at(gone, 18, 30) + timedelta(minutes=ctx.rng.randint(5, 50))
        r = Resource(tenant_id=tenant.id, project_ref=proj.id if proj else None,
                     service=RESOURCE_TYPES[rtype]["service"], resource_type=rtype,
                     resource_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"hcs-demo/{code}/{rtype}/{name}")), name=name,
                     health="UNKNOWN", region=proj.region_id if proj else None, az="az1.dc1", created_time=created,
                     labels={}, attrs={}, metrics={}, expected_state={}, owner=owner, business=business,
                     env_type="PROD", remark=remark, lifecycle="DELETED", first_seen=max(ctx.start, created),
                     last_seen=deleted - timedelta(hours=1), deleted_at=deleted, **fields)
        db.add(r)
        db.flush()
        if created >= ctx.start:
            _change(ctx, r, "CREATED", created + timedelta(minutes=ctx.rng.randint(10, 50)))
        _change(ctx, r, "DELETED", deleted)
    for code, rtype, name, days, hh, mm, who, edits in MANUAL:
        r = ctx.res(code, name, rtype)
        if r is None:
            continue
        at = ctx.at(days, hh, mm)
        operator = ctx.name(who)
        _change(ctx, r, "UPDATED", at, [{"field": f, "label": lb, "old": old, "new": new} for f, lb, old, new in edits],
                source="MANUAL", operator=operator)
        for f, _lb, old, new in edits:
            if f == "owner":
                r.owner = new
                ctx.owner_changes[r.id] = (at, old)
            elif f == "expected_state.running":
                r.expected_state = {**(r.expected_state or {}), "running": False}
            elif f == "remark":
                r.remark = new
            elif f == "critical":
                r.critical = bool(new)
        _audit(ctx, at, who, "ledger.update", target_type="resource", target_id=r.id, target_name=r.name,
               tenant_id=r.tenant_id, detail={"changes": {lb: [old, new] for _f, lb, old, new in edits}})
    # 负责人交接之后产生的告警归新负责人
    for rid, (at, _old) in ctx.owner_changes.items():
        r = db.get(Resource, rid)
        for a in db.scalars(select(AlarmEvent).where(AlarmEvent.resource_ref == rid, AlarmEvent.first_seen >= at)):
            a.owner = r.owner if r else a.owner
    db.flush()


# ---------------------------------------------------------------- 2. 历史告警
# (规则, 资源名, 观测值（区间随机）, 权重, 子对象, 发生时段)
DAY, NIGHT, WORK = (8, 23), (1, 6), (9, 19)
POOLS: dict[str, list[tuple[str, str, dict[str, Any], float, str, tuple[int, int]]]] = {
    "tenant-a": [
        ("R-ECS-004", "ecs-etl-02", {"cpu_util": (86.0, 98.5)}, 5, "", NIGHT),
        ("R-ECS-004", "ecs-etl-03", {"cpu_util": (85.5, 96.0)}, 3, "", NIGHT),
        ("R-ECS-004", "ecs-app-02", {"cpu_util": (85.2, 92.0)}, 2, "", DAY),
        ("R-ECS-005", "ecs-etl-01", {"mem_util": (90.3, 97.0)}, 4, "", NIGHT),
        ("R-ECS-005", "ecs-jenkins-01", {"mem_util": (90.1, 93.5)}, 1, "", WORK),
        ("R-ECS-006", "ecs-jenkins-01", {"disk_util": (85.3, 97.0), "disk_mount": "/data"}, 3, "", WORK),
        ("R-ECS-006", "ecs-report-01", {"disk_util": (85.1, 92.0), "disk_mount": "/data"}, 3, "", NIGHT),
        ("R-ECS-006", "ecs-gitlab-01", {"disk_util": (85.4, 90.0), "disk_mount": "/"}, 1, "", WORK),
        ("R-ECS-002", "ecs-app-04", {"status": "SHUTOFF", "status_duration_min": (6, 20)}, 1, "", DAY),
        ("R-ECS-002", "ecs-test-01", {"status": "SHUTOFF", "status_duration_min": (6, 30)}, 1, "", WORK),
        ("R-ECS-007", "ecs-web-04", {"reboots_24h": (1, 2)}, 1, "", DAY),
        ("R-ECS-007", "ecs-api-gw-01", {"reboots_24h": 1}, 1, "", NIGHT),
        ("R-ECS-001", "ecs-etl-03", {"status": "ERROR"}, 0.4, "", NIGHT),
        ("R-DCS-005", "redis-prod-01", {"memory_usage": (80.5, 94.0)}, 4, "", DAY),
        ("R-DCS-005", "redis-cache-02", {"memory_usage": (80.2, 89.0)}, 3, "", DAY),
        ("R-DCS-005", "redis-session-01", {"memory_usage": (80.1, 86.0)}, 1, "", DAY),
        ("R-DCS-006", "redis-rank-01", {"cpu_usage": (70.5, 88.0)}, 2, "", DAY),
        ("R-DCS-007", "redis-session-01", {"connected_clients": (8100, 9300), "max_clients": 10000}, 2, "", DAY),
        ("R-DCS-003", "redis-lock-01", {"status": "RESTARTING", "status_duration_min": (61, 80)}, 0.4, "", NIGHT),
        ("R-GS-006", "gs-core-01", {"disk_usage": (75.2, 88.0)}, 3, "", DAY),
        ("R-GS-006", "gs-crm-01", {"disk_usage": (75.5, 92.0)}, 3, "", DAY),
        ("R-GS-006", "gs-report-01", {"disk_usage": (76.0, 86.0)}, 2, "", NIGHT),
        ("R-GS-007", "gs-report-01", {"cpu_usage": (80.2, 93.0)}, 3, "", NIGHT),
        ("R-GS-007", "gs-bill-01", {"cpu_usage": (80.1, 88.0)}, 1, "", DAY),
        ("R-GS-008", "gs-core-01", {"mem_usage": (85.1, 92.0)}, 1, "", DAY),
        ("R-GS-001", "gs-audit-01", {"status": "FAILED"}, 0.3, "", NIGHT),
        ("R-DGC-RT-001", "rt_payment_risk", {"status": "EXCEPTION"}, 2, "", DAY),
        ("R-DGC-RT-001", "rt_user_behavior", {"status": "EXCEPTION"}, 1, "", DAY),
        ("R-DGC-RT-002", "rt_inventory_cdc", {"status": "STOPPED", "expected_running": True}, 1, "", DAY),
        ("R-DGC-RT-005", "rt_user_behavior", {"job_status": "NORMAL", "node_status": "EXCEPTION"}, 1,
         "rt_user_behavior_agg", DAY),
        ("R-DGC-BAT-001", "dws_settle_daily", {"last_status": "fail"}, 3, "", NIGHT),
        ("R-DGC-BAT-001", "ods_crm_customer_sync", {"last_status": "fail"}, 2, "", NIGHT),
        ("R-DGC-BAT-001", "dwd_order_detail_daily", {"last_status": "fail"}, 1, "", NIGHT),
        ("R-DGC-BAT-003", "ads_report_gen", {"last_status": "running", "run_minutes": (100, 160), "p90_minutes": 45},
         2, "", NIGHT),
        ("R-MRS-JOB-001", "dws_settle_daily / mr_export", {"node_status": "fail"}, 2, "", DAY),
        ("R-MRS-JOB-001", "dwd_order_detail_daily / spark_etl", {"node_status": "fail"}, 1, "", NIGHT),
        ("R-MRS-CLU-001", "mrs-prod-02", {"cluster_state": "abnormal"}, 0.3, "", NIGHT),
        ("R-CCE-POD-001", "trade/order-service", {"restarts_10m": (3, 7)}, 2, "", DAY),
        ("R-CCE-POD-001", "member/member-api", {"restarts_10m": (3, 5)}, 1, "", DAY),
        ("R-CCE-POD-002", "trade/order-service", {"oom_1h": (1, 3)}, 2, "", DAY),
        ("R-CCE-POD-003", "trade/payment-gateway", {"crashloop_pods": 1, "status": "2/3 Ready"}, 1, "", DAY),
        ("R-CCE-POD-004", "flink/flink-taskmanager", {"backpressure_min": (5, 45)}, 4, "", DAY),
        ("R-CCE-POD-005", "trade/report-export", {"evicted_1h": 1}, 1, "", NIGHT),
    ],
    "tenant-b": [
        ("R-ECS-004", "ecs-batch-01", {"cpu_util": (85.3, 97.0)}, 3, "", NIGHT),
        ("R-ECS-004", "ecs-batch-02", {"cpu_util": (85.1, 93.0)}, 2, "", NIGHT),
        ("R-ECS-004", "ecs-search-01", {"cpu_util": (85.2, 90.0)}, 1, "", DAY),
        ("R-ECS-005", "ecs-batch-01", {"mem_util": (90.2, 96.0)}, 3, "", NIGHT),
        ("R-ECS-006", "ecs-prom-01", {"disk_util": (85.2, 94.0), "disk_mount": "/data"}, 2, "", WORK),
        ("R-ECS-006", "ecs-cms-01", {"disk_util": (85.1, 90.0), "disk_mount": "/"}, 1, "", WORK),
        ("R-ECS-007", "ecs-cms-02", {"reboots_24h": (1, 2)}, 2, "", DAY),
        ("R-ECS-003", "ecs-batch-02", {"transitional": True, "status": "MIGRATING", "status_text": "迁移中",
                                       "status_duration_min": (30, 45)}, 1, "", NIGHT),
        ("R-ECS-001", "ecs-search-01", {"status": "ERROR"}, 0.4, "", DAY),
        ("R-DCS-005", "redis-portal-01", {"memory_usage": (80.4, 92.0)}, 3, "", DAY),
        ("R-DCS-005", "redis-cms-01", {"memory_usage": (80.1, 86.0)}, 1, "", DAY),
        ("R-DCS-007", "redis-portal-01", {"connected_clients": (8050, 9000), "max_clients": 10000}, 1, "", DAY),
        ("R-CCE-POD-001", "web/portal-api", {"restarts_10m": (3, 6)}, 3, "", DAY),
        ("R-CCE-POD-002", "cms/cms-backend", {"oom_1h": (1, 2)}, 1, "", DAY),
        ("R-CCE-POD-004", "cms/cms-worker", {"backpressure_min": (5, 40)}, 3, "", DAY),
        ("R-CCE-POD-005", "batch/report-job", {"evicted_1h": 1}, 1, "", NIGHT),
    ],
}
# 一次“大促压测”的集中告警：(租户, 几天前, 时, 分, [规则, 资源, 观测值])
SPIKES: list[tuple[str, int, int, int, list[tuple[str, str, dict[str, Any]]]]] = [
    ("tenant-a", 9, 14, 10, [("R-DCS-007", "redis-session-01", {"connected_clients": 9420, "max_clients": 10000}),
                             ("R-ECS-004", "ecs-app-02", {"cpu_util": 96.4}),
                             ("R-ECS-004", "ecs-api-gw-02", {"cpu_util": 91.2}),
                             ("R-CCE-POD-001", "trade/order-service", {"restarts_10m": 6}),
                             ("R-DGC-RT-001", "rt_payment_risk", {"status": "EXCEPTION"}),
                             ("R-GS-007", "gs-core-01", {"cpu_usage": 91.5})]),
    ("tenant-b", 16, 20, 30, [("R-CCE-POD-001", "web/portal-api", {"restarts_10m": 5}),
                              ("R-DCS-005", "redis-portal-01", {"memory_usage": 95.6}),
                              ("R-ECS-004", "ecs-portal-01", {"cpu_util": 93.8})]),
]
# 告警持续时长（分钟）
DURATION = {"R-ECS-004": (15, 150), "R-ECS-005": (20, 240), "R-ECS-006": (60, 900), "R-ECS-002": (15, 120),
            "R-ECS-001": (40, 180), "R-ECS-003": (5, 30), "R-ECS-007": (1440, 1500), "R-DCS-005": (15, 200),
            "R-DCS-006": (10, 60), "R-DCS-007": (10, 90), "R-DCS-003": (10, 40), "R-GS-006": (120, 1800),
            "R-GS-007": (15, 120), "R-GS-008": (20, 180), "R-GS-001": (30, 90), "R-DGC-RT-001": (8, 60),
            "R-DGC-RT-002": (30, 240), "R-DGC-RT-005": (10, 45), "R-DGC-BAT-001": (40, 300),
            "R-DGC-BAT-003": (20, 120), "R-MRS-JOB-001": (60, 240), "R-MRS-CLU-001": (20, 60),
            "R-CCE-POD-001": (10, 30), "R-CCE-POD-002": (60, 80), "R-CCE-POD-003": (15, 60),
            "R-CCE-POD-005": (60, 70)}
INTERVAL_MIN = {"ecs": 2, "dcs": 1, "gaussdb": 2, "dataarts": 1, "mrs": 2, "cce": 1}
# 人工关闭的比例和备注（其余自动恢复）
CLOSE = {"R-DGC-BAT-001": (0.5, "已手动重跑成功，关闭告警"), "R-MRS-JOB-001": (0.4, "上游数据延迟导致，已补跑成功"),
         "R-ECS-007": (0.3, "计划内重启（变更窗口），关闭"), "R-ECS-002": (0.4, "计划内关机维护，关闭"),
         "R-GS-006": (0.15, "已清理历史分区，磁盘回落后关闭")}
ACK_RATE = {"P1": (0.95, 2, 12), "P2": (0.85, 3, 25), "P3": (0.45, 5, 60), "P4": (0.2, 10, 120)}
ACK_NOTES = {"ecs": ["已认领，排查中", "已登录主机排查占用进程", "确认是批处理高峰，观察中"],
             "dcs": ["已认领，排查大 key / 热 key", "已联系业务方确认缓存过期策略"],
             "gaussdb": ["已认领，DBA 处理中", "已提交扩容申请，先归档历史数据"],
             "dataarts": ["已联系作业负责人", "排查上游数据源", "已重跑作业，观察中"],
             "mrs": ["已认领，查看 YARN 日志"], "cce": ["已认领，查看 Pod 日志", "已回滚到上一版本", "调大 limit 后观察"]}


def _acker(ctx: Ctx, code: str, alarm: AlarmEvent) -> str:
    r = ctx.rng.random()
    if code == "tenant-b":
        return "wangfang" if r < 0.75 else "oncall"
    if alarm.service in ("dataarts", "mrs") or (alarm.owner == "李娜"):
        return "lina" if r < 0.8 else "oncall"
    return "oncall" if r < 0.7 else ("lina" if r < 0.85 else "zhaolei")


def _sent(n: Notification | None, at: datetime) -> None:
    if n is not None:
        n.status, n.sent_at, n.message_id = "SENT", at + timedelta(seconds=5), message_id()


def _owner_at(ctx: Ctx, r: Resource, at: datetime) -> str | None:
    ch = ctx.owner_changes.get(r.id)
    return ch[1] if ch and at < ch[0] else r.owner


def _make_alarm(ctx: Ctx, code: str, rule_id: str, name: str, spec: dict, sub: str, first: datetime,
                busy: dict[str, list[tuple[datetime, datetime]]]) -> AlarmEvent | None:
    """按真实规则生成一条已结束的历史告警（含日志、通知、升级）。先在内存里走完状态，最后一次写入，
    避免与当前活动告警的部分唯一索引（同一指纹只能有一条活动告警）冲突。"""
    db, rng = ctx.db, ctx.rng
    row = ctx.rules.get(rule_id)
    if row is None:
        return None
    r = ctx.res(code, name, row.resource_type)
    if r is None or r.lifecycle != "ACTIVE":
        return None
    rule = Rule.from_obj(row)
    facts: dict[str, Any] = {"critical": r.critical, "expected_running": True, "status_duration_min": 0}
    facts.update({k: _pick(rng, v) for k, v in spec.items()})
    m = rule.match(facts)
    if m is None:
        return None
    if rule_id == "R-CCE-POD-004":
        minutes = max(5, int(facts.get("backpressure_min", 5))) + rng.randint(3, 8)
    else:
        lo, hi = DURATION.get(rule_id, (15, 120))
        minutes = rng.randint(lo, hi)
    end = first + timedelta(minutes=minutes)
    if end > ctx.now - timedelta(minutes=40) or first < ctx.start:
        return None
    tenant, proj = ctx.tenants[code], ctx.project(ctx.tenants[code])
    fp = fingerprint(code, proj.project_id if proj else "", r.resource_type, r.resource_id, rule.id, sub)
    if any(s < end + timedelta(minutes=30) and first < e + timedelta(minutes=30) for s, e in busy.get(fp, [])):
        return None
    busy.setdefault(fp, []).append((first, end))
    step = INTERVAL_MIN.get(r.service, 2)
    alarm = AlarmEvent(
        fingerprint=fp, rule_id=rule.id, resource_ref=r.id, tenant_id=tenant.id, service=r.service,
        resource_type=r.resource_type, resource_name=r.name, sub_object=sub, severity=m.severity, status="FIRING",
        title=m.title, check_item=rule.check_item, current_value=m.current_value, threshold_text=m.threshold_text,
        owner=_owner_at(ctx, r, first), first_seen=first, last_seen=end - timedelta(minutes=step), resolved_at=end,
        count=max(1, minutes // step), reopen_count=0, notify_seq=0,
        detail={"facts": facts, "resource_id": r.resource_id, "critical": r.critical,
                "workspace": (r.attrs or {}).get("workspace"), "project": proj.project_id if proj else None,
                "region": proj.region_id if proj else None, "tenant": tenant.short_name or tenant.name,
                "tenant_code": code})
    # (时间, 动作, 原状态, 新状态, 操作人, 备注)；(时间, 类型, 收件人, 主题)
    logs: list[tuple[datetime, str, str | None, str, str, str]] = [
        (first, "FIRE", None, "FIRING", "system", f"{rule.name}：{m.title}")]
    notes: list[tuple[datetime, str, list[str], str]] = []
    emails, _routes = resolve_recipients(db, alarm)
    if emails:
        at = first + timedelta(seconds=20)
        notes.append((at, "FIRING", emails, build_subject(alarm, "FIRING", at)))
    rate, a_lo, a_hi = ACK_RATE.get(m.severity, (0.4, 5, 60))
    ack_at = first + timedelta(minutes=rng.randint(a_lo, a_hi)) if rng.random() < rate else None
    if ack_at is not None and ack_at >= end:
        ack_at = None
    for plan in escalation_plan(db, alarm):                 # 未认领升级（与 notifier 的升级规则一致）
        at = first + timedelta(minutes=plan["after_min"])
        if at < (ack_at or end):
            logs.append((at, "ESCALATE", "FIRING", "FIRING", "system", plan["note"]))
            notes.append((at, "ESCALATE", plan["emails"], build_subject(alarm, "ESCALATE", at)))
    ctx.firing_titles[id(alarm)] = m.title
    status = "FIRING"
    if ack_at is not None:
        who = _acker(ctx, code, alarm)
        note = rng.choice(ACK_NOTES.get(r.service, ["已认领"]))
        alarm.ack_by, alarm.ack_at, status = ctx.name(who), ack_at, "ACKED"
        logs.append((ack_at, "ACK", "FIRING", "ACKED", ctx.name(who), note))
        ctx.ack_notes[id(alarm)] = note
    close_rate, close_note = CLOSE.get(rule_id, (0.0, ""))
    if rng.random() < close_rate:
        who = next((u for u, x in ctx.users.items() if x.display_name == alarm.ack_by), None) or _acker(ctx, code, alarm)
        alarm.status = "CLOSED"
        logs.append((end, "CLOSE", status, "CLOSED", ctx.name(who), close_note))
    else:
        alarm.status = "RESOLVED"
        if rule.kind == "threshold":
            below = (rule.recover or {}).get("below")
            key = str(rule.expr.get("value", ""))
            rec = dict(facts)
            if below is not None and re.fullmatch(r"[a-z_0-9]+", key):
                rec[key] = round(rng.uniform(below * 0.6, below * 0.95), 1) if below > 5 else 0
            try:
                v = evaluate(rule.expr["value"], rec)
                alarm.current_value = f"{round(v, 1) if isinstance(v, float) else v}{rule.expr.get('unit', '')}"
            except Exception:  # noqa: BLE001  演示数据：取不到恢复值就保留触发值
                pass
            tpl = (rule.expr.get("title") or rule.name).split("（")[0]
            alarm.title = render_title(tpl, rec, value=alarm.current_value or "-")
        logs.append((end, "RESOLVE", status, "RESOLVED", "system", "满足恢复条件，自动恢复"))
        if emails:
            at = end + timedelta(seconds=15)
            notes.append((at, "RESOLVED", emails, build_subject(alarm, "RESOLVED", at)))
    alarm.notify_seq = len(notes)
    db.add(alarm)
    db.flush()
    ctx.firing_titles[alarm.id] = ctx.firing_titles.pop(id(alarm))
    if id(alarm) in ctx.ack_notes:
        ctx.ack_notes[alarm.id] = ctx.ack_notes.pop(id(alarm))
    for at, action, frm, to, who, note in logs:
        db.add(AlarmEventLog(alarm_id=alarm.id, action=action, from_status=frm, to_status=to, operator=who,
                             note=note, at=at))
    for at, kind, rcpt, subject in notes:
        db.add(Notification(alarm_id=alarm.id, kind=kind, channel="SMTP", recipients=rcpt, subject=subject,
                            status="SENT", retry=0, created_at=at, sent_at=at + timedelta(seconds=5),
                            message_id=message_id()))
    ctx.alarms.append(alarm)
    return alarm


def _alarm_history(ctx: Ctx) -> None:
    rng = ctx.rng
    busy: dict[str, list[tuple[datetime, datetime]]] = {}
    for a in ctx.db.scalars(select(AlarmEvent)):           # 当前活动告警占用的时间段
        busy.setdefault(a.fingerprint, []).append((a.first_seen, ctx.now + timedelta(days=1)))
    first_day = ctx.start.astimezone(ctx.tz).date()
    today = ctx.now.astimezone(ctx.tz).date()
    for code, pool in POOLS.items():
        if code not in ctx.tenants:
            continue
        weights = [p[3] for p in pool]
        d = first_day
        while d <= today:
            weekend = d.weekday() >= 5
            if code == "tenant-a":
                n = rng.choice([1, 2, 2, 3] if weekend else [3, 4, 4, 5, 5, 6, 7])
            else:
                n = rng.choice([0, 1, 1, 2] if weekend else [1, 2, 2, 3, 3])
            for _ in range(n):
                for _try in range(4):
                    rule_id, name, spec, _w, sub, (h0, h1) = rng.choices(pool, weights=weights)[0]
                    first = ctx.local(d, rng.randint(h0, h1 - 1), rng.randint(0, 59), rng.randint(0, 59))
                    if _make_alarm(ctx, code, rule_id, name, spec, sub, first, busy):
                        break
            d += timedelta(days=1)
    for code, days, hh, mm, items in SPIKES:
        if code not in ctx.tenants:
            continue
        base = ctx.at(days, hh, mm)
        for i, (rule_id, name, spec) in enumerate(items):
            _make_alarm(ctx, code, rule_id, name, spec, "", base + timedelta(minutes=i * 3 + rng.randint(0, 2)), busy)
    ctx.db.flush()


def _escalate_active(ctx: Ctx) -> None:
    """当前活动告警：超过升级时间仍未认领（或认领晚于升级时间）的，补一条已发送的升级通知。"""
    db = ctx.db
    for a in db.scalars(select(AlarmEvent).where(AlarmEvent.status.in_(("FIRING", "ACKED")))).all():
        if db.scalar(select(AlarmEventLog.id).where(AlarmEventLog.alarm_id == a.id,
                                                    AlarmEventLog.action == "ESCALATE").limit(1)):
            continue
        for plan in escalation_plan(db, a):
            at = a.first_seen + timedelta(minutes=plan["after_min"])
            if at >= min(a.ack_at or ctx.now, ctx.now):
                break
            _sent(escalate(db, a, plan, at), at)
    db.flush()


# ---------------------------------------------------------------- 3. 容器事件（近 7 天）
POD_HISTORY: dict[str, list[tuple[str, str, int]]] = {
    "tenant-a": [("trade/order-service", "RESTART", 5), ("trade/order-service", "OOM_KILLED", 3),
                 ("trade/order-service", "PROBE_FAILED", 4), ("trade/payment-gateway", "RESTART", 3),
                 ("trade/payment-gateway", "CRASH_LOOP", 1), ("trade/payment-gateway", "PROBE_FAILED", 2),
                 ("trade/cart-service", "SCHEDULE_FAILED", 1), ("trade/cart-service", "SCALED", 2),
                 ("trade/report-export", "EVICTED", 2), ("member/member-api", "PROBE_FAILED", 2),
                 ("member/member-api", "SCALED", 3), ("member/member-api", "RESTART", 1),
                 ("member/member-points", "RESTART", 1), ("flink/flink-taskmanager", "BACKPRESSURE", 6),
                 ("flink/flink-taskmanager", "RESTART", 2), ("flink/flink-jobmanager", "RESTART", 1),
                 ("test/demo-app", "IMAGE_PULL_FAILED", 2)],
    "tenant-b": [("web/portal-api", "RESTART", 4), ("web/portal-api", "PROBE_FAILED", 3), ("web/portal-web", "SCALED", 3),
                 ("cms/cms-backend", "OOM_KILLED", 2), ("cms/cms-backend", "RESTART", 1),
                 ("cms/cms-worker", "BACKPRESSURE", 3), ("batch/report-job", "EVICTED", 1),
                 ("batch/image-resize", "SCALED", 4)],
}


def _pod_event(ctx: Ctx, tenant: HcsAccount, st: PodState | None, ns: str, workload: str, kind: str, etype: str,
               at: datetime, probes: dict[int, PodProbe], scale: dict[str, int]) -> PodEvent:
    rng = ctx.rng
    c = (st.containers or [{}])[0] if st is not None else {}
    cname, image = c.get("name") or workload.split("-")[0], c.get("image") or ""
    pod = st.pod if st is not None else ""
    restarts = rng.randint(1, 9)
    count, end, peak, source, detail = 1, None, None, "POD_STATUS", {}
    if etype == "RESTART":
        code = rng.choice([1, 143, 137, 2])
        reason, msg = "Error", f"容器 {cname} 重启（累计 {restarts} 次），上次退出：Error，exitCode={code}"
        detail = {"restart_count": restarts, "exit_code": code, "image": image}
    elif etype == "OOM_KILLED":
        limit = c.get("memory_limit") or "1Gi"
        reason, msg = "OOMKilled", f"容器 {cname} 内存超限被 OOM Kill（limit {limit}），已自动重启（累计 {restarts} 次）"
        detail = {"restart_count": restarts, "exit_code": 137, "image": image}
    elif etype == "CRASH_LOOP":
        count = rng.randint(6, 14)
        reason = "CrashLoopBackOff"
        msg = f"容器 {cname} 处于 CrashLoopBackOff（累计重启 {count} 次，上次退出：Error，exitCode=1）"
        end = at + timedelta(minutes=rng.randint(18, 45))
    elif etype == "BACKPRESSURE":
        probe = next((p for p in probes.values() if p.tenant_id == tenant.id and p.namespace == ns and p.enabled), None)
        minutes = rng.randint(8, 55)
        end, peak = at + timedelta(minutes=minutes), float(rng.randint(520, 960))
        reason = "BackPressure"
        if probe is not None:
            source = "PROBE"
            thr = f"{probe.op} {probe.threshold:g}{probe.unit}"
            msg = f"{probe.name}：{probe.metric} 峰值 {peak:g}{probe.unit}（阈值 {thr}），持续 {minutes} 分钟"
            detail = {"probe_id": probe.id, "probe_name": probe.name, "metric": probe.metric, "op": probe.op,
                      "threshold": probe.threshold, "unit": probe.unit}
        else:
            source, peak = "K8S_EVENT", float(rng.randint(12000, 26000))
            msg = f"consumer lag {peak:g} 超过 10000，消费积压（背压），持续 {minutes} 分钟"
            detail = {"k8s_type": "Warning", "involved_kind": "Pod", "component": "lag-exporter"}
        count = max(1, minutes // 2)
    elif etype == "PROBE_FAILED":
        source, reason, count = "K8S_EVENT", "Unhealthy", rng.randint(3, 12)
        msg = rng.choice(["Readiness probe failed: HTTP probe failed with statuscode: 503",
                          "Liveness probe failed: Get \"http://%s:8080/actuator/health\": context deadline exceeded"
                          % (st.pod_ip if st is not None and st.pod_ip else "10.0.0.1")])
        detail = {"k8s_type": "Warning", "involved_kind": "Pod", "component": "kubelet"}
        end = at + timedelta(minutes=rng.randint(1, 6))
    elif etype == "EVICTED":
        reason = "Evicted"
        msg = f"The node was low on resource: memory. Container {cname} was using 3.1Gi, which exceeds its request of 1Gi."
    elif etype == "SCHEDULE_FAILED":
        source, reason, count = "K8S_EVENT", "FailedScheduling", rng.randint(2, 6)
        msg = "0/6 nodes are available: 6 Insufficient cpu. preemption: 0/6 nodes are available: 6 No preemption victims found."
        detail = {"k8s_type": "Warning", "involved_kind": "Pod", "component": "default-scheduler"}
    elif etype == "IMAGE_PULL_FAILED":
        source, reason, count = "K8S_EVENT", "Failed", rng.randint(2, 5)
        img = image or f"swr.region-01.hcs.example.com/{ns}/{workload}:1.8.0"
        msg = f'Failed to pull image "{img}": rpc error: code = NotFound desc = manifest unknown'
        detail = {"k8s_type": "Warning", "involved_kind": "Pod", "component": "kubelet"}
    else:  # SCALED
        source, reason = "K8S_EVENT", "ScalingReplicaSet"
        cur = scale.get(workload, 3)
        nxt = max(2, cur + rng.choice([-1, 1, 1, 2]))
        scale[workload] = nxt
        rs = workload + ("-" + st.labels.get("pod-template-hash", "") if st is not None and st.labels else "")
        msg = f"Scaled {'up' if nxt > cur else 'down'} replica set {rs} to {nxt}"
        detail = {"k8s_type": "Normal", "involved_kind": kind, "component": "deployment-controller"}
        pod, cname = "", ""
    last = end or at
    return PodEvent(tenant_id=tenant.id, project_ref=st.project_ref if st else None,
                    cluster_id=st.cluster_id if st else "", cluster_name=st.cluster_name if st else "",
                    namespace=ns, workload_kind=kind, workload=workload, pod=pod, container=cname if pod else "",
                    node=st.node if st is not None and pod else None, event_type=etype,
                    level=EVENT_TYPES[etype]["level"], reason=reason, message=msg, source=source, count=count,
                    first_time=at, last_time=last, end_time=end, ongoing=False, peak=peak, detail=detail,
                    dedup_key=f"hist:{etype}:{pod or workload}:{int(at.timestamp())}", created_at=at)


def _pod_history(ctx: Ctx) -> None:
    db, rng = ctx.db, ctx.rng
    probes = {p.id: p for p in db.scalars(select(PodProbe))}
    scale: dict[str, int] = {}
    for code, items in POD_HISTORY.items():
        tenant = ctx.tenants.get(code)
        if tenant is None:
            continue
        states = list(db.scalars(select(PodState).where(PodState.tenant_id == tenant.id, PodState.deleted.is_(False))))
        for ref, etype, n in items:
            ns, workload = ref.split("/", 1)
            pods = [s for s in states if s.namespace == ns and s.workload == workload]
            if not pods:
                # 仅演示模板使用明确app标签匹配；事件仍写入真实直接owner，不再伪造Deployment。
                pods = [s for s in states if s.namespace == ns and (s.labels or {}).get("app") == workload]
            if not pods:
                continue
            for _ in range(n):
                at = ctx.now - timedelta(days=rng.uniform(0.2, 7.0))
                if etype != "IMAGE_PULL_FAILED":   # 业务高峰时段更集中
                    local = at.astimezone(ctx.tz)
                    at = local.replace(hour=rng.choice([10, 11, 14, 15, 16, 20, 21])).astimezone(UTC)
                    at = min(at, ctx.now - timedelta(hours=3))
                chosen = rng.choice(pods)
                db.add(_pod_event(ctx, tenant, chosen, ns, chosen.workload, chosen.workload_kind, etype, at, probes, scale))
    db.flush()


# ---------------------------------------------------------------- 4. 巡检运行记录
PARTIAL_ERRORS = {
    "ecs_status": {"tenant-a": "CES 批量查询超时（ReadTimeout），本轮 3 台 ECS 使用上一轮指标",
                   "tenant-b": "Prometheus 查询超时（ReadTimeout），本轮只巡检状态"},
    "gaussdb_status": {"tenant-a": "CES 指标查询失败 1 个实例：HTTP 500（CES.0003）"},
    "cce_pod_events": {"tenant-a": "背压探针抓取 flink/flink-taskmanager-9d7f6c5b8-a1s2d /metrics 超时",
                       "tenant-b": "events 列表分页 continue token 过期，已从头重试"},
    "dgc_realtime_status": {"tenant-a": "DataArts 作业列表接口限流（DLF.0815），1 个工作空间稍后重试"},
    "dcs_status": {"tenant-a": "CES 批量查询部分失败：2 个实例无数据", "tenant-b": "CES 未开通，内存指标改用实例详情字段"},
}
OUTAGE = ("tenant-a", 9, 2, "IAM 获取 Token 失败：HTTP 503 Service Unavailable（APIGW.0503，HCS 网关维护窗口）")


def _run_history(ctx: Ctx) -> None:
    db, rng = ctx.db, ctx.rng
    code_of = {t.id: c for c, t in ctx.tenants.items()}
    real = list(db.scalars(select(InspectionRun).order_by(InspectionRun.started_at)))
    if not real:
        return
    first_real = real[0].started_at
    latest: dict[tuple[str, int | None], InspectionRun] = {}
    for r in real:
        latest[(r.task_code, r.tenant_id)] = r
    # 每个租户、每个服务的告警时间段（推算历史上每次巡检的异常数）
    spans: dict[tuple[int | None, str], list[tuple[datetime, datetime]]] = {}
    for a in db.scalars(select(AlarmEvent)):
        spans.setdefault((a.tenant_id, a.service), []).append((a.first_seen, a.resolved_at or ctx.now))
    changes: dict[int | None, list[tuple[datetime, str, str]]] = {}
    for ch in db.scalars(select(ResourceChange).where(ResourceChange.source == "SYNC")):
        changes.setdefault(ch.tenant_id, []).append((ch.detected_at, ch.resource_type, ch.change_type))
    service_of = {"dgc_realtime_status": "dataarts", "dgc_batch_instance": "dataarts", "mrs_cluster_status": "mrs",
                  "mrs_job_status": "mrs", "gaussdb_status": "gaussdb", "dcs_status": "dcs", "ecs_status": "ecs",
                  "cce_pod_events": "cce"}
    out_code, out_days, out_hour, out_msg = OUTAGE
    outage_at = ctx.at(out_days, out_hour, 5)
    rows: list[InspectionRun] = []
    for (task, tid), tpl in latest.items():
        code = code_of.get(tid or 0, "")
        offset = zlib.crc32(f"{task}/{code}".encode()) % 50 + 3
        times: list[datetime] = []
        t = first_real.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1) + timedelta(minutes=offset)
        while t > ctx.start:
            times.append(t)
            t -= timedelta(hours=1) if t > ctx.now - timedelta(hours=24) else timedelta(hours=6)
        if code == out_code:
            times.append(outage_at)
        times.sort()
        dur = ((tpl.finished_at or tpl.started_at) - tpl.started_at).total_seconds() or 1.0
        prev_t = ctx.start
        svc = service_of.get(task)
        for t in times:
            status, err, abnormal = "SUCCESS", None, 0
            if code == out_code and t == outage_at:
                status, err = "FAILED", out_msg
            elif task in PARTIAL_ERRORS and code in PARTIAL_ERRORS[task] and rng.random() < 0.03:
                status, err = "PARTIAL", PARTIAL_ERRORS[task][code]
            if svc and status != "FAILED":
                abnormal = sum(1 for s, e in spans.get((tid, svc), []) if s <= t < e)
            stats = dict(tpl.stats or {})
            if task == "ledger_sync":
                stats = {k: {**v, "created": 0, "updated": 0, "deleted": 0} for k, v in stats.items()}
                for at, rtype, kind in changes.get(tid, []):
                    if prev_t < at <= t and rtype in stats:
                        key = {"CREATED": "created", "DELETED": "deleted"}.get(kind, "updated")
                        stats[rtype][key] += 1
            rows.append(InspectionRun(
                task_code=task, tenant_id=tid, trigger_type="SCHEDULE", started_at=t,
                finished_at=t + timedelta(seconds=max(0.3, dur * rng.uniform(0.7, 1.5))), status=status,
                resource_total=0 if status == "FAILED" else tpl.resource_total, abnormal_total=abnormal,
                api_calls=1 if status == "FAILED" else tpl.api_calls, error_summary=err,
                stats={} if status == "FAILED" else stats))
            prev_t = t
    db.add_all(rows)
    db.flush()


# ---------------------------------------------------------------- 5. 报表
def _reports(ctx: Ctx) -> list[Report]:
    db = ctx.db
    today = ctx.now.astimezone(ctx.tz).date()
    monday = today - timedelta(days=today.weekday())
    last_month = today.replace(day=1) - timedelta(days=1)
    plan: list[tuple[str, date | None, HcsAccount | None]] = [("DAILY", today - timedelta(days=i), None)
                                                              for i in range(7, 0, -1)]
    plan += [("WEEKLY", monday - timedelta(weeks=w), None) for w in range(3, 0, -1)]
    plan.append(("MONTHLY", last_month, None))
    for t in ctx.tenants.values():
        plan += [("DAILY", today - timedelta(days=i), t) for i in range(3, 0, -1)]
        plan += [("WEEKLY", monday - timedelta(weeks=1), t), ("MONTHLY", last_month, t)]
    leaders = db.scalar(select(ContactGroup).where(ContactGroup.code == "leaders"))
    fallback = [c.email for c in db.scalars(select(Contact).where(Contact.id.in_(leaders.member_ids or [])))] \
        if leaders else []
    out: list[Report] = []
    for kind, ref, tenant in plan:
        rpt = generate(db, kind, ref, tenant_id=tenant.id if tenant else None)
        due = {"DAILY": timedelta(hours=8, minutes=30), "WEEKLY": timedelta(hours=9),
               "MONTHLY": timedelta(hours=9)}[kind]
        at = min(rpt.period_end + due + timedelta(seconds=ctx.rng.randint(3, 40)), ctx.now - timedelta(minutes=5))
        to = default_recipients(db, rpt) or fallback
        rpt.created_at, rpt.sent_to, rpt.sent_at = at, to, (at + timedelta(seconds=8)) if to else None
        out.append(rpt)
    ledger = generate(db, "LEDGER", created_by=ctx.name("oncall"))
    ledger.created_at = ctx.now - timedelta(hours=3, minutes=12)
    out.append(ledger)
    db.flush()
    return out


# ---------------------------------------------------------------- 6. 操作审计
def _audit_history(ctx: Ctx, reports: list[Report]) -> None:
    db, rng = ctx.db, ctx.rng
    a_id = ctx.tenants["tenant-a"].id if "tenant-a" in ctx.tenants else None
    b = ctx.tenants.get("tenant-b")
    b_id = b.id if b else None
    # 登录：近 14 天的工作日
    for days in range(14, -1, -1):
        d = (ctx.now.astimezone(ctx.tz) - timedelta(days=days)).date()
        if d.weekday() >= 5:
            continue
        for who, p in (("zhaolei", 0.9), ("oncall", 1.0), ("lina", 0.8), ("wangfang", 0.8), ("admin", 0.3),
                       ("viewer", 0.15)):
            if rng.random() < p:
                at = ctx.local(d, rng.choice([8, 9, 9, 10, 13, 14]), rng.randint(0, 59), rng.randint(0, 59))
                if at < ctx.now - timedelta(minutes=10):
                    _audit(ctx, at, who, "auth.login", detail={"method": "password"})
    _audit(ctx, ctx.at(3, 9, 12), None, "auth.login_failed", result="FAILED", operator="zhangwei",
           detail={"reason": "用户名或密码错误"})
    _audit(ctx, ctx.at(1, 9, 3), "chenjing", "auth.login_failed", result="FAILED", detail={"reason": "账号已停用"})
    # 平台配置
    if b is not None:
        _audit(ctx, ctx.at(45, 15, 30), "admin", "tenant.create", target_type="tenant", target_id=b.id,
               target_name=b.name, tenant_id=b_id, detail={"auth_type": "AKSK", "projects": 1})
        _audit(ctx, ctx.at(45, 15, 36), "admin", "tenant.test", target_type="tenant", target_id=b.id,
               target_name=b.name, tenant_id=b_id, detail={"ok": True, "ces": False, "prometheus": None})
        _audit(ctx, ctx.at(18, 10, 20), "zhaolei", "tenant.update", target_type="tenant", target_id=b.id,
               target_name=b.name, tenant_id=b_id,
               detail={"changes": {"prometheus_url": [None, b.prometheus_url], "metrics_source": ["AUTO", "AUTO"]}})
        _audit(ctx, ctx.at(18, 10, 22), "zhaolei", "tenant.test", target_type="tenant", target_id=b.id,
               target_name=b.name, tenant_id=b_id, detail={"ok": True, "ces": False, "prometheus": True})
    for t in ctx.tenants.values():
        _audit(ctx, ctx.at(0, 9, 40), "zhaolei", "tenant.test", target_type="tenant", target_id=t.id,
               target_name=t.name, tenant_id=t.id, detail={"ok": True})
    for who, days, role, scope in (("lina", 40, "值班运维", ["租户A"]), ("wangfang", 40, "值班运维", ["租户B"]),
                                   ("viewer", 30, "只读访客", None)):
        u = ctx.user(who)
        if u is not None:
            _audit(ctx, ctx.at(days, 11, 0), "admin", "user.create", target_type="user", target_id=u.id,
                   target_name=u.username, detail={"role": role, "tenants": scope or "全部租户"})
    if (cj := ctx.user("chenjing")) is not None:
        _audit(ctx, ctx.at(5, 17, 40), "admin", "user.update", target_type="user", target_id=cj.id,
               target_name=cj.username, detail={"changes": {"enabled": [True, False]}, "note": "调岗，停用账号"})
    _audit(ctx, ctx.at(20, 16, 5), "admin", "role.update", target_type="role", target_name="值班运维",
           detail={"added": ["report:send"], "removed": []})
    _audit(ctx, ctx.at(25, 20, 15), "lina", "auth.password", target_type="user", target_name="lina")
    for rid, days, detail in (("R-DCS-005", 21, {"version": [1, 2], "change": "P3 阈值 75% → 80%"}),
                              ("R-ECS-006", 10, {"version": [1, 2], "change": "连续命中次数 1 → 2"})):
        row = ctx.rules.get(rid)
        if row is not None:
            _audit(ctx, ctx.at(days, 15, 10), "zhaolei", "rule.update", target_type="rule", target_id=rid,
                   target_name=row.name, detail=detail)
    probes = list(db.scalars(select(PodProbe).order_by(PodProbe.id)))
    for probe in probes:
        saver = "lina" if probe.tenant_id == a_id else "wangfang"
        _audit(ctx, ctx.at(8 if saver == "lina" else 6, 16, 30), saver, "probe.save", target_type="probe",
               target_id=probe.id, target_name=probe.name, tenant_id=probe.tenant_id,
               detail={"metric": probe.metric, "threshold": probe.threshold, "enabled": probe.enabled})
    _audit(ctx, ctx.at(1, 15, 2), "oncall", "podevent.collect", target_type="tenant", target_id=a_id,
           target_name="租户A", tenant_id=a_id, detail={"events": 3})
    _audit(ctx, ctx.at(3, 11, 20), "oncall", "ledger.sync", target_type="tenant", target_id=a_id,
           target_name="租户A", tenant_id=a_id, detail={"created": 0, "updated": 1, "deleted": 0})
    _audit(ctx, ctx.at(6, 10, 45), "zhaolei", "ledger.export", target_type="resource", target_name="资源台账",
           detail={"types": "全部", "rows": db.query(Resource).filter(Resource.lifecycle == "ACTIVE").count()})
    _audit(ctx, ctx.now - timedelta(hours=2, minutes=5), "zhaolei", "task.run", target_type="task",
           target_id="ecs_status", target_name="ECS 云服务器状态与资源使用", detail={"tenants": "全部"})
    # 告警处置：近 10 天的认领 / 关闭
    recent = [a for a in ctx.alarms if a.first_seen >= ctx.now - timedelta(days=10)]
    for a in recent:
        title = ctx.firing_titles.get(a.id, a.title)
        acker = next((u for u, x in ctx.users.items() if x.display_name == a.ack_by), None)
        if a.ack_at is not None and acker:
            _audit(ctx, a.ack_at, acker, "alarm.ack", target_type="alarm", target_id=a.id,
                   target_name=f"{a.resource_name} {title}", tenant_id=a.tenant_id,
                   detail={"note": ctx.ack_notes.get(a.id, "")})
        if a.status == "CLOSED" and a.resolved_at is not None:
            closer = next((lg.operator for lg in db.scalars(select(AlarmEventLog).where(
                AlarmEventLog.alarm_id == a.id, AlarmEventLog.action == "CLOSE"))), None)
            cu = next((u for u, x in ctx.users.items() if x.display_name == closer), None)
            if cu:
                _audit(ctx, a.resolved_at, cu, "alarm.close", target_type="alarm", target_id=a.id,
                       target_name=f"{a.resource_name} {title}", tenant_id=a.tenant_id)
    mail_ack = next((a for a in reversed(recent) if a.ack_at is not None and a.severity in ("P1", "P2")), None)
    if mail_ack is not None:
        _audit(ctx, mail_ack.ack_at or ctx.now, None, "alarm.mail_ack", target_type="alarm", target_id=mail_ack.id,
               target_name=f"{mail_ack.resource_name} {ctx.firing_titles.get(mail_ack.id, mail_ack.title)}",
               tenant_id=mail_ack.tenant_id, operator="zhaolei@example.com", detail={"via": "邮件一键认领链接"})
    # 报表
    weekly = next((r for r in reversed(reports) if r.kind == "WEEKLY" and r.tenant_id is None), None)
    if weekly is not None:
        _audit(ctx, ctx.at(2, 10, 30), "zhaolei", "report.send", target_type="report", target_id=weekly.id,
               target_name=weekly.title, detail={"to": ["cto-office@example.com"]})
    ledger = next((r for r in reports if r.kind == "LEDGER"), None)
    if ledger is not None:
        _audit(ctx, ledger.created_at, "oncall", "report.create", target_type="report", target_id=ledger.id,
               target_name=ledger.title)
    db.flush()


# ---------------------------------------------------------------- 入口
def seed_history(db: Session, tenants: dict[str, HcsAccount], users: dict[str, User], start: datetime,
                 now: datetime) -> None:
    ctx = Ctx(db=db, tenants=tenants, users=users, start=start, now=now, rng=random.Random(20261021),
              tz=ZoneInfo(get_settings().timezone), projects={p.id: p for p in db.scalars(select(HcsProject))},
              rules={r.id: r for r in db.scalars(select(AlarmRule))})
    _ledger_history(ctx)
    _alarm_history(ctx)
    _escalate_active(ctx)
    _pod_history(ctx)
    _run_history(ctx)
    db.commit()
    reports = _reports(ctx)
    _audit_history(ctx, reports)
    db.commit()
