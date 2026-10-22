"""演示数据（INSPECTOR_DEMO_SEED=true，仅开发 / 演示 / 自动化测试；prod 环境 main.py 会忽略）。

数据走真实链路：两个租户的资源都来自 Mock HCS（进程内 TestClient，见 mock_hcs/），台账同步、连通性测试与能力探测、
ECS 状态巡检、容器事件采集、规则评估、告警状态机都执行正式代码；历史数据（已恢复告警、台账变更、容器事件、巡检
运行记录、操作审计、报表）由 seed_history.py 补充。

演示租户：
- 租户A · 核心生产：用户名密码换 Token；有云监控 CES；DataArts / MRS / GaussDB / 2 个 CCE 集群
- 租户B · 互联网业务：AK/SK 签名；局点没有 CES（自动探测后改用租户 Prometheus）；1 个 CCE 集群

演示账号（口令仅用于演示环境）：见 DEMO_USERS。lina 只能看租户A，wangfang 只能看租户B，chenjing 已停用。
"""
import logging
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.crypto import encrypt
from app.core.security import hash_password
from app.ledger.sync import sync_ledger
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    Contact,
    ContactGroup,
    HcsAccount,
    HcsProject,
    Notification,
    NotifyRoute,
    PodEvent,
    PodProbe,
    PodState,
    Resource,
    ResourceChange,
    Role,
    User,
)
from app.models.base import utcnow
from app.notify.render import build_subject
from app.rules.loader import sync_default_rules
from app.services.bootstrap import ensure_roles, ensure_tasks
from app.services.pipeline import run_task
from app.services.tenancy import Target, resolve_targets
from app.services.tenant_check import check_tenant
from mock_hcs import openapi as mock_oa
from mock_hcs import tenants as mt
from mock_hcs.app import app as mock_app

from . import seed_history as hist
from .seed_profiles import PROFILES, apply_profiles
from .seed_v04 import seed_v04

log = logging.getLogger(__name__)
MOCK = "http://mock"                       # 进程内 Mock HCS（不依赖单独启动的 Mock 服务）
IAM_ENDPOINT = f"https://iam-apigateway-proxy.{mt.EXTERNAL_DOMAIN}"

DEMO_USERS = [  # 用户名, 口令, 显示名, 邮箱, 角色, 租户范围（None = 全部租户）, 启用
    ("admin", "admin123", "系统管理员", "admin@example.com", "admin", None, True),
    ("zhaolei", "zhaolei123", "赵磊", "zhaolei@example.com", "ops_lead", None, True),
    ("oncall", "oncall123", "张伟", "zhangwei@example.com", "oncall", None, True),
    ("lina", "lina12345", "李娜", "lina@example.com", "oncall", ["tenant-a"], True),
    ("wangfang", "wangfang123", "王芳", "wangfang@example.com", "oncall", ["tenant-b"], True),
    ("viewer", "viewer123", "刘洋", "liuyang@example.com", "viewer", None, True),
    ("chenjing", "chenjing123", "陈静", "chenjing@example.com", "oncall", None, False),
]
CONTACTS = [("张伟", "zhangwei@example.com", "13800000001"), ("李娜", "lina@example.com", "13800000002"),
            ("陈静", "chenjing@example.com", "13800000003"), ("赵磊", "zhaolei@example.com", "13800000004"),
            ("王芳", "wangfang@example.com", "13800000005"), ("刘强", "liuqiang@example.com", "13800000006"),
            ("周敏", "zhoumin@example.com", "13800000007"), ("孙浩", "sunhao@example.com", "13800000008")]
GROUPS = [("ops-core", "核心运维组", ["张伟", "李娜", "陈静"], ["赵磊"]),
          ("ops-web", "互联网运维组", ["王芳", "刘强"], ["刘强"]),
          ("dba", "DBA 组", ["周敏", "李娜"], ["赵磊"]),
          ("bigdata", "大数据组", ["孙浩", "李娜"], ["赵磊"]),
          ("leaders", "运维负责人", ["赵磊"], ["赵磊"])]
ESCALATE = [{"after_min": 15, "to": "leader", "severities": ["P1", "P2"]}]

# ECS 状态时间线（巡检前写入，状态持续时长、24 小时重启次数由此计算）：名称, 类型, 距今分钟, 原状态, 新状态
ECS_TIMELINE: dict[str, list[tuple]] = {
    "tenant-a": [("ecs-app-04", "STATUS", 47, "ACTIVE", "SHUTOFF"),
                 ("ecs-api-gw-01", "STATUS", 552, "ACTIVE", "HARD_REBOOT"),
                 ("ecs-api-gw-01", "STATUS", 548, "HARD_REBOOT", "ACTIVE"),
                 ("ecs-api-gw-01", "STATUS", 95, "ACTIVE", "REBOOT"), ("ecs-api-gw-01", "STATUS", 92, "REBOOT", "ACTIVE"),
                 ("ecs-web-04", "STATUS", 6 * 1440 + 35, "ACTIVE", "REBOOT"),
                 ("ecs-web-04", "STATUS", 6 * 1440 + 31, "REBOOT", "ACTIVE"),
                 ("ecs-test-02", "STATUS", 12 * 1440 + 620, "ACTIVE", "SHUTOFF"),
                 ("ecs-dev-01", "STATUS", 20 * 1440 + 290, "ACTIVE", "SHUTOFF")],
    "tenant-b": [("ecs-search-01", "STATUS", 118, "ACTIVE", "ERROR"),
                 ("ecs-batch-02", "STATUS", 52, "ACTIVE", "MIGRATING"),
                 ("ecs-cms-02", "BOOT", 465, None, None), ("ecs-cms-02", "BOOT", 190, None, None),
                 ("ecs-cms-02", "BOOT", 0, None, None),                     # 0 = 取 Mock 中的实际开机时间
                 ("ecs-stage-01", "STATUS", 3 * 1440 + 130, "ACTIVE", "SHUTOFF")],
}
# 活动告警的发生时间（距今分钟）：(规则, 资源名) → 分钟；未列出的按指纹生成 20~280 分钟
ALARM_AGE = {("R-ECS-002", "ecs-app-04"): 45, ("R-ECS-001", "ecs-search-01"): 116, ("R-ECS-003", "ecs-batch-02"): 22,
             ("R-ECS-007", "ecs-api-gw-01"): 94, ("R-ECS-007", "ecs-cms-02"): 24,
             ("R-CCE-POD-004", "flink/flink-taskmanager"): 13, ("R-CCE-POD-004", "cms/cms-worker"): 11,
             ("R-CCE-POD-001", "web/portal-api"): 2, ("R-CCE-POD-002", "trade/order-service"): 6,
             ("R-CCE-POD-003", "trade/payment-gateway"): 31, ("R-CCE-POD-005", "trade/report-export"): 38,
             ("R-DGC-RT-001", "rt_payment_risk"): 128, ("R-DGC-RT-002", "rt_inventory_cdc"): 362,
             ("R-DGC-RT-005", "rt_user_behavior"): 41, ("R-DGC-BAT-001", "dws_settle_daily"): 42,
             ("R-DGC-BAT-003", "ads_report_gen"): 48, ("R-MRS-JOB-001", "hive_agg_hourly"): 4,
             ("R-DCS-005", "redis-prod-01"): 186, ("R-GS-006", "gs-core-01"): 240}
# 已认领的活动告警：(规则, 资源名) → (认领人, 发生后几分钟认领, 备注)
ACKS = {("R-DGC-RT-002", "rt_inventory_cdc"): ("张伟", 9, "已联系作业负责人：计划内停机维护，预计 18:00 前恢复"),
        ("R-GS-006", "gs-core-01"): ("李娜", 25, "已提交扩容申请，先归档历史分区"),
        ("R-ECS-003", "ecs-batch-02"): ("王芳", 6, "宿主机维护触发热迁移，观察中"),
        ("R-CCE-POD-002", "trade/order-service"): ("张伟", 3, "已调大 limit 至 2Gi，观察")}


def _tenants(db: Session) -> dict[str, HcsAccount]:
    out: dict[str, HcsAccount] = {}
    for t in mt.TENANTS:
        acc = db.scalar(select(HcsAccount).where(HcsAccount.code == t["code"]))
        if acc is None:
            secret = t["password"] if t["auth_type"] == "PASSWORD" else t["secret_key"]
            acc = HcsAccount(code=t["code"], name=t["name"], short_name=t["short_name"], color=t["color"],
                             iam_endpoint=IAM_ENDPOINT, domain_name=t["domain_name"], domain_id=t["domain_id"],
                             auth_type=t["auth_type"], username=t.get("username"), access_key=t.get("access_key"),
                             secret_cipher=encrypt(secret), endpoints={}, enabled=True,
                             report_mail_to=list(t["report_mail_to"]), metrics_source=t["metrics_source"],
                             prometheus_url=t["prometheus_url"], capabilities={}, remark=t["remark"], last_check={})
            db.add(acc)
            db.flush()
            db.add(HcsProject(account_id=acc.id, region_id=mt.REGION, project_id=t["project_id"],
                              project_name=t["project_name"], external_domain=mt.EXTERNAL_DOMAIN, env="PROD",
                              workspaces=list(t["workspaces"]), enabled=True))
        out[t["code"]] = acc
    db.flush()
    return out


def _users(db: Session, tenants: dict[str, HcsAccount], now) -> dict[str, User]:
    roles = {r.code: r for r in db.scalars(select(Role))}
    out: dict[str, User] = {}
    for i, (username, pwd, display, email, role, scope, enabled) in enumerate(DEMO_USERS):
        u = User(username=username, display_name=display, email=email, password_hash=hash_password(pwd),
                 role_id=roles[role].id, tenant_ids=[tenants[c].id for c in scope] if scope else None, enabled=enabled,
                 last_login_at=now - timedelta(hours=1 + i * 5) if enabled else now - timedelta(days=40))
        db.add(u)
        out[username] = u
    db.flush()
    return out


def _notify(db: Session, tenants: dict[str, HcsAccount]) -> None:
    ids: dict[str, int] = {}
    for name, email, phone in CONTACTS:
        c = Contact(name=name, email=email, phone=phone, enabled=True)
        db.add(c)
        db.flush()
        ids[name] = c.id
    for code, name, members, leaders in GROUPS:
        db.add(ContactGroup(code=code, name=name, member_ids=[ids[m] for m in members],
                            leader_ids=[ids[m] for m in leaders]))
    b = tenants["tenant-b"].id
    for name, prio, match, group, esc, cont in [
        ("P1 紧急 → 运维负责人（抄送）", 5, {"severities": ["P1"]}, "leaders", [], True),
        ("租户B 告警 → 互联网运维组", 10, {"tenant_ids": [b]}, "ops-web", ESCALATE, False),
        ("数据库 / 缓存 → DBA 组（抄送）", 20, {"services": ["gaussdb", "rds", "dcs"]}, "dba", [], True),
        ("大数据作业 → 大数据组（抄送）", 30, {"services": ["dataarts", "mrs"]}, "bigdata", [], True),
        ("默认 → 核心运维组", 100, {}, "ops-core", ESCALATE, False),
    ]:
        db.add(NotifyRoute(name=name, priority=prio, match=match, group_code=group, escalation=esc,
                           continue_match=cont, enabled=True))
    db.flush()


def _resource(db: Session, tenant_id: int, rtype: str, name: str) -> Resource | None:
    return db.scalar(select(Resource).where(Resource.tenant_id == tenant_id, Resource.resource_type == rtype,
                                            Resource.name == name, Resource.lifecycle == "ACTIVE"))


def _ecs_timeline(db: Session, tenants: dict[str, HcsAccount], now) -> None:
    """ECS 状态变更 / 重启记录（与 ECS 巡检写入的格式一致），并把当前状态的开始时间回拨到对应时刻。"""
    for code, items in ECS_TIMELINE.items():
        acc, ds = tenants[code], mt.BY_CODE[code]["ds"]
        usage = mock_oa.ecs_usage(ds)
        boots: dict[str, int] = {}
        for name, kind, minutes, old, new in items:
            r = _resource(db, acc.id, "ecs_server", name)
            if r is None:
                continue
            if kind == "STATUS":
                at = now - timedelta(minutes=minutes)
                db.add(ResourceChange(tenant_id=acc.id, resource_ref=r.id, resource_type=r.resource_type,
                                      resource_id=r.resource_id, name=r.name, change_type="STATUS",
                                      changes=[{"field": "status_raw", "label": "状态", "old": old, "new": new}],
                                      source="INSPECT", operator="system", detected_at=at))
                if new == r.status_raw:
                    r.status_since = at
                continue
            boot = usage[name]["boot"] if minutes == 0 else int((now - timedelta(minutes=minutes)).timestamp())
            prev = boots.get(name, boot - 3 * 86400)
            boots[name] = boot
            db.add(ResourceChange(tenant_id=acc.id, resource_ref=r.id, resource_type=r.resource_type,
                                  resource_id=r.resource_id, name=r.name, change_type="REBOOT",
                                  changes=[{"field": "boot_time", "label": "开机时间",
                                            "old": hist.iso_ts(prev), "new": hist.iso_ts(boot)}],
                                  source="INSPECT", operator="system", detected_at=hist.from_ts(boot)))
    db.commit()


def _probes(db: Session, tenants: dict[str, HcsAccount], now) -> PodProbe:
    a, b = tenants["tenant-a"], tenants["tenant-b"]
    flink = PodProbe(tenant_id=a.id, name="Flink 背压（TaskManager）", cluster_id=mt.TENANT_A["ds"].rid("cce", "cce-prod-01"),
                     namespace="flink", selector="component=taskmanager", port=9249, path="/metrics",
                     metric=mt.TENANT_A["ds"].FLINK_BP, label_filter="job_name=rt_order_sync", agg="max", op=">=",
                     threshold=500, unit=" ms/s", for_minutes=3, recover_minutes=5, enabled=True, last_result={},
                     updated_by="张伟", updated_at=now - timedelta(days=12))
    db.add(flink)
    db.add(PodProbe(tenant_id=b.id, name="CMS 消费积压（kafka lag）", cluster_id="*", namespace="cms",
                    selector="app=cms-worker", port=9308, path="/metrics", metric="kafka_consumergroup_lag",
                    label_filter="topic=cms-publish", agg="sum", op=">=", threshold=100000, unit=" 条", for_minutes=5,
                    recover_minutes=10, enabled=False, last_result={}, updated_by="王芳",
                    updated_at=now - timedelta(days=3)))
    db.commit()
    return flink


def _run(db: Session, http: TestClient, targets: list[Target], code: str, times: int = 1) -> None:
    for _ in range(times):
        for tg in targets:
            run_task(db, code, client=tg.client(http=http, mock_url=MOCK), target=tg)


def _backdate_probe(db: Session, probe: PodProbe, minutes: int, now) -> None:
    """背压探针：把“超过阈值的开始时间”回拨，下一轮采集即形成持续中的背压事件（持续 N 分钟）。"""
    key = f"bp{probe.id}"
    for st in db.scalars(select(PodState).where(PodState.tenant_id == probe.tenant_id)):
        d = (st.data or {}).get(key)
        if d and d.get("over_since"):
            st.data = {**st.data, key: {**d, "over_since": (now - timedelta(minutes=minutes)).isoformat()}}
    db.commit()


def _recent_restarts(db: Session, tenant: HcsAccount, workload: str, now) -> None:
    """租户B portal-api：近 10 分钟内另外两次重启（Mock 只保留最近一次退出信息，采集只能补记 1 次）。"""
    st = next((s for s in db.scalars(select(PodState).where(PodState.tenant_id == tenant.id,
                                                            PodState.workload == workload)) if s.restarts >= 3), None)
    if st is None or not st.containers:
        return
    c = st.containers[0]
    rc = int(c.get("restart_count") or 0)
    for back, ago in ((2, 8), (1, 5)):
        n = rc - back
        at = now - timedelta(minutes=ago)
        db.add(PodEvent(tenant_id=tenant.id, cluster_id=st.cluster_id, cluster_name=st.cluster_name,
                        namespace=st.namespace, workload_kind=st.workload_kind, workload=st.workload, pod=st.pod,
                        container=c["name"], node=st.node, event_type="RESTART", level="WARNING", reason="Error",
                        message=f"容器 {c['name']} 重启（累计 {n} 次），上次退出：Error，exitCode=137",
                        source="POD_STATUS", count=1, first_time=at, last_time=at, ongoing=False,
                        detail={"restart_count": n, "exit_code": 137, "image": c.get("image")},
                        dedup_key=f"restart:{st.key}/{c['name']}/{n}", created_at=at))
    db.commit()


def _shift_pod_events(db: Session, now) -> None:
    """持续中的 CrashLoopBackOff、刚发生的驱逐：把开始时间调整到与告警发生时间一致。"""
    for e in db.scalars(select(PodEvent).where(PodEvent.event_type.in_(("CRASH_LOOP", "EVICTED")))):
        at = now - timedelta(minutes=33 if e.event_type == "CRASH_LOOP" else 39)
        e.first_time = at
        if e.event_type == "EVICTED":
            e.last_time = at
    db.commit()


def _age(alarm: AlarmEvent) -> int:
    if (alarm.rule_id, alarm.resource_name) in ALARM_AGE:
        return ALARM_AGE[(alarm.rule_id, alarm.resource_name)]
    return 20 + int(alarm.fingerprint[:6], 16) % 260


def _adjust_alarms(db: Session, now) -> None:
    """活动告警：发生时间、次数、日志、通知记录调整为“已运行一段时间”的样子；部分告警已认领、已升级通知。"""
    for a in db.scalars(select(AlarmEvent).where(AlarmEvent.status.in_(("FIRING", "ACKED")))):
        first = now - timedelta(minutes=_age(a))
        a.first_seen, a.count = first, max(a.count, _age(a) // 2 + 1)
        logs = list(db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == a.id).order_by(AlarmEventLog.id)))
        for i, lg in enumerate(logs):
            lg.at = first if lg.action == "FIRE" else min(now, first + timedelta(minutes=i))
        for n in db.scalars(select(Notification).where(Notification.alarm_id == a.id)):
            n.created_at = first + timedelta(seconds=20)
            n.status, n.sent_at, n.message_id = "SENT", n.created_at + timedelta(seconds=6), hist.message_id()
        ack = ACKS.get((a.rule_id, a.resource_name))
        if ack and a.status == "FIRING":
            who, after, note = ack
            a.status, a.ack_by, a.ack_at = "ACKED", who, first + timedelta(minutes=after)
            db.add(AlarmEventLog(alarm_id=a.id, action="ACK", from_status="FIRING", to_status="ACKED", operator=who,
                                 note=note, at=a.ack_at))
        if a.resource_name == "ecs-search-01" and a.status == "FIRING":         # 15 分钟未认领：升级通知负责人
            at = first + timedelta(minutes=15)
            db.add(Notification(alarm_id=a.id, kind="ESCALATE", channel="SMTP", recipients=["liuqiang@example.com"],
                                subject=build_subject(a, "ESCALATE", at), status="SENT", retry=0, created_at=at,
                                sent_at=at + timedelta(seconds=5), message_id=hist.message_id()))
            db.add(AlarmEventLog(alarm_id=a.id, action="ESCALATE", from_status="FIRING", to_status="FIRING",
                                 operator="system", note="15 分钟未认领，升级通知组长 刘强", at=at))
            a.notify_seq += 1
    db.commit()


def seed_demo(db: Session) -> bool:
    """写入演示数据；已有用户时不做任何事（返回 False）。"""
    if db.scalar(select(func.count()).select_from(User)):
        return False
    now = utcnow()
    ensure_roles(db)
    ensure_tasks(db)
    sync_default_rules(db)
    tenants = _tenants(db)
    users = _users(db, tenants, now)
    _notify(db, tenants)
    db.commit()

    http = TestClient(mock_app, base_url=MOCK)
    for acc in tenants.values():                       # 连通性测试 + 能力探测：租户B 探测到“没有 CES”
        check_tenant(db, acc, http=http, mock_url=MOCK)
    targets = resolve_targets(db, [a.id for a in tenants.values()])
    for tg in targets:
        sync_ledger(db, client=tg.client(http=http, mock_url=MOCK), target=tg)
    start = hist.history_start(now)
    apply_profiles(db, tenants, start, now)            # 负责人 / 业务系统 / 环境 / 核心 / 允许关机
    hist.retime_created(db, start, now)
    _ecs_timeline(db, tenants, now)
    probe = _probes(db, tenants, now)

    _run(db, http, targets, "ecs_status", 3)
    for code, times in (("dgc_realtime_status", 2), ("dgc_batch_instance", 1), ("mrs_cluster_status", 2),
                        ("mrs_job_status", 1), ("gaussdb_status", 3), ("dcs_status", 3)):
        _run(db, http, targets, code, times)
    _run(db, http, targets, "cce_pod_events")
    _backdate_probe(db, probe, 18, utcnow())
    _recent_restarts(db, tenants["tenant-b"], "portal-api", utcnow())
    _run(db, http, targets, "cce_pod_events", 2)
    apply_profiles(db, tenants, start, now)            # 巡检过程中新出现的对象（作业、容器工作负载）
    _shift_pod_events(db, utcnow())
    _adjust_alarms(db, utcnow())

    hist.seed_history(db, tenants, users, start, utcnow())
    seed_v04(db, tenants, users)
    log.info("demo data seeded: tenants=%s, profiles=%s", list(tenants), list(PROFILES))
    return True
