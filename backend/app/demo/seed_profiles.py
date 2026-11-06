"""演示数据：台账管理字段（业务系统 / 负责人 / 环境 / 核心资源 / 允许关机 / 备注）。

真实环境中这些字段由运维在“资源台账”页面维护（或 Excel 批量导入），同步任务不会覆盖；
这里按资源名称前缀、容器命名空间给两个演示租户预置一份，保证页面、告警、报表里的“负责人 / 业务系统”有数据。
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, HcsAccount, HcsProject, Resource
from app.services.catalog import LEDGER_TYPES

JOB_TYPES = ("dgc_realtime_job", "dgc_batch_job", "mrs_job")
_TEST = re.compile(r"(^|[-/])(test|stage|staging|uat)([-/]|$)")
_DEV = re.compile(r"(^|[-/])dev([-/]|$)")

# rules：(名称前缀, 业务系统, 负责人)，按顺序匹配，先命中先用；namespaces：容器工作负载按命名空间归属；
# unassigned：最近新纳管、还没人认领的资源（台账“未分配负责人”筛选 / 汇总卡片有数据）
PROFILES: dict[str, dict] = {
    "tenant-a": {
        "rules": [
            (("ecs-report-",), "数据平台", "陈静"),          # 负责人交接演示：历史里有一条 陈静 → 李娜 的人工修改
            (("ecs-app-", "ecs-api-gw-", "elb-prod-api", "eip-elb-api", "gs-core-", "gs-bill-", "redis-prod-",
              "redis-session-", "redis-lock-", "redis-mq-", "subnet-prod-app"), "电商交易", "张伟"),
            (("ecs-nginx-", "ecs-web-", "elb-prod-web", "eip-nginx-", "eip-elb-web", "redis-cache-",
              "subnet-prod-web"), "电商门户", "周敏"),
            (("gs-crm-", "redis-rank-"), "会员中心", "孙浩"),
            (("ecs-etl-", "ecs-job-", "mrs-", "dws-", "gs-report-", "vol-etl-", "subnet-prod-bigdata"), "数据平台", "李娜"),
            (("gs-audit-",), "日志审计", "陈静"),
            (("ecs-bastion-", "ecs-jenkins-", "ecs-gitlab-", "ecs-ntp-dns-", "ecs-inspector-", "eip-bastion",
              "vpc-mgmt", "subnet-mgmt"), "运维管理", "赵磊"),
            (("ecs-test-", "ecs-dev-", "cce-test-", "elb-test-", "vpc-test",
              "subnet-test-"), "测试环境", "孙浩"),
            (("cce-prod-", "subnet-prod-cce"), "容器平台", "陈静"),
        ],
        "namespaces": {"trade": ("电商交易", "张伟"), "member": ("会员中心", "孙浩"), "flink": ("数据平台", "李娜"),
                       "kube-system": ("容器平台", "陈静"), "test": ("测试环境", "孙浩")},
        "jobs": ("数据平台", "李娜"),
        "default": ("基础设施", "陈静"),
        "critical": {"ecs-app-01", "ecs-app-02", "ecs-app-03", "ecs-api-gw-01", "ecs-api-gw-02", "elb-prod-api",
                     "gs-core-01", "gs-bill-01", "redis-prod-01", "redis-session-01", "cce-prod-01", "mrs-rt-01"},
        "unassigned": {"ecs-etl-03", "ecs-etl-03-sys", "ecs-etl-03-data-01", "vol-etl-scratch"},
        "allow_off": {"ecs-test-02": "测试机，非工作时间关机节省资源（允许关机，不告警）",
                      "ecs-dev-01": "开发机，按需开机（允许关机，不告警）"},
        "remarks": {"ecs-bastion-01": "堡垒机，仅运维网段可访问", "ecs-inspector-01": "巡检平台部署节点",
                    "eip-idle-01": "活动结束后未绑定，待确认是否释放", "vol-backup-old-01": "迁移前旧备份盘，计划月底清理",
                    "vol-migrate-tmp": "数据迁移临时盘", "gs-core-01": "核心交易库，变更走 CAB 审批",
                    "mrs-rt-01": "实时计算集群，承载 Flink 实时作业", "ecs-api-gw-01": "API 网关主节点（双活）"},
    },
    "tenant-b": {
        "rules": [
            (("ecs-portal-", "elb-portal", "eip-elb-portal", "redis-portal-",
              "subnet-web-front", "subnet-web-app"), "门户网站", "王芳"),
            (("ecs-cms-", "redis-cms-"), "内容管理", "刘强"),
            (("ecs-search-", "css-web-search"), "搜索推荐", "王芳"),
            (("ecs-batch-",), "批处理", "刘强"),
            (("ecs-ops-", "ecs-prom-", "eip-ops-", "subnet-web-ops"), "运维监控", "刘强"),
            (("ecs-stage-",), "预发环境", "王芳"),
            (("cce-web-", "subnet-web-cce"), "容器平台", "刘强"),
        ],
        "namespaces": {"web": ("门户网站", "王芳"), "cms": ("内容管理", "刘强"), "batch": ("批处理", "刘强"),
                       "monitoring": ("运维监控", "刘强"), "kube-system": ("容器平台", "刘强")},
        "jobs": ("数据平台", "王芳"),
        "default": ("基础设施", "刘强"),
        "critical": {"ecs-portal-01", "ecs-portal-02", "elb-portal", "redis-portal-01", "cce-web-01"},
        "unassigned": {"ecs-portal-03", "ecs-portal-03-sys"},
        "allow_off": {"ecs-stage-01": "预发环境，仅发布窗口开机（允许关机，不告警）"},
        "remarks": {"ecs-prom-01": "租户侧 Prometheus（CES 不可用时的 ECS 指标来源）",
                    "eip-web-spare": "备用 EIP，大促扩容预留", "vol-web-old-logs": "历史日志盘，待归档后释放",
                    "ecs-search-01": "搜索服务，已提工单排查宿主机故障"},
    },
}


def profile_of(prof: dict, r: Resource) -> tuple[str, str]:
    """返回（业务系统, 负责人）。"""
    if r.resource_type in JOB_TYPES:
        return prof["jobs"]
    if r.resource_type == "cce_workload":
        return prof["namespaces"].get((r.attrs or {}).get("namespace"), prof["default"])
    for prefixes, business, owner in prof["rules"]:
        if r.name.startswith(prefixes):
            return business, owner
    return prof["default"]


def env_of(r: Resource, project: HcsProject | None) -> str:
    attrs = r.attrs or {}
    keys = [r.name, str(attrs.get("namespace") or ""), str(attrs.get("cluster") or "")]
    if any(_DEV.search(k) for k in keys):
        return "DEV"
    if any(_TEST.search(k) for k in keys):
        return "TEST"
    return project.env if project is not None else "PROD"


def apply_profiles(db: Session, tenants: dict[str, HcsAccount], start: datetime, now: datetime) -> int:
    """只补空字段（可重复执行）；同时把 first_seen 调整为“纳管时间”：云上创建早于演示历史起点的按起点算。"""
    projects = {p.id: p for p in db.scalars(select(HcsProject))}
    changed = 0
    for code, tenant in tenants.items():
        prof = PROFILES.get(code)
        if prof is None:
            continue
        for r in db.scalars(select(Resource).where(Resource.tenant_id == tenant.id)):
            business, owner = (None, None) if r.name in prof["unassigned"] else profile_of(prof, r)
            r.business = r.business or business
            r.owner = r.owner or owner
            r.env_type = r.env_type or env_of(r, projects.get(r.project_ref or 0))
            if r.resource_type in LEDGER_TYPES:
                r.critical = r.critical or r.name in prof["critical"]
                if r.name in prof["allow_off"] and "running" not in (r.expected_state or {}):
                    r.expected_state = {**(r.expected_state or {}), "running": False}
                    r.remark = r.remark or prof["allow_off"][r.name]
                r.remark = r.remark or prof["remarks"].get(r.name)
            seen = min(max(start, r.created_time) if r.created_time else start, now)
            if r.first_seen is None or r.first_seen > seen + timedelta(hours=2):   # 已按变更记录回拨过的不再覆盖
                r.first_seen = seen
            changed += 1
    db.flush()
    owners = {r.id: r.owner for r in db.scalars(select(Resource).where(Resource.owner.is_not(None)))}
    for a in db.scalars(select(AlarmEvent).where(AlarmEvent.owner.is_(None), AlarmEvent.resource_ref.is_not(None))):
        a.owner = owners.get(a.resource_ref or 0)
    db.commit()
    return changed
