"""生产初始化（幂等，可重复执行）：内置角色、内置规则、巡检任务（含台账同步）、首个管理员。

与演示数据（seed.py）的区别：这里只写平台运行必需的基础数据，不写任何演示资源、告警和账号口令。
用法：迁移完成后执行 python -m app.workers.bootstrap；首次部署时通过 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin。
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import BUILTIN_ROLES
from app.core.security import hash_password
from app.models import InspectionTask, Role, User
from app.rules.loader import sync_default_rules

# (任务代码, 名称, 服务, 默认周期秒[, crontab]) —— 巡检任务与 collectors/registry.py 中的 TASKS 一一对应；
# ledger_sync 为台账同步，data_cleanup 为数据清理（按“系统设置 → 数据保留”删除过期数据，默认每天 03:30）
BUILTIN_TASKS: list[tuple] = [("dgc_realtime_status", "DataArts 实时作业状态", "dataarts", 60),
                 ("dgc_batch_instance", "DataArts 批作业实例", "dataarts", 300),
                 ("mrs_cluster_status", "MRS 集群状态", "mrs", 120),
                 ("mrs_job_status", "MRS 作业执行", "mrs", 300),
                 ("gaussdb_status", "GaussDB 实例状态与容量", "gaussdb", 120),
                 ("dcs_status", "DCS 实例状态与内存", "dcs", 60),
                 ("ecs_status", "ECS 云服务器状态与资源使用", "ecs", 120),
                 ("cce_pod_events", "CCE 容器事件采集", "cce", 60),
                 ("ledger_sync", "资源台账同步", "ledger", 3600),
                 ("data_cleanup", "数据清理（按保留期删除过期数据）", "platform", 86400, "30 3 * * *")]


def ensure_tasks(db: Session) -> int:
    """补齐内置任务；已存在的任务不覆盖（保留页面上调整过的周期、启停状态）。"""
    added = 0
    for code, name, service, interval, *cron in BUILTIN_TASKS:
        if db.get(InspectionTask, code) is None:
            db.add(InspectionTask(code=code, name=name, service=service, interval_sec=interval,
                                  cron=cron[0] if cron else None, params={}))
            added += 1
    db.flush()
    return added


def ensure_roles(db: Session) -> int:
    """补齐 4 个内置角色；已存在的不覆盖（页面上调整过的权限保留）。平台管理员角色始终为全部权限。"""
    added = 0
    for r in BUILTIN_ROLES:
        role = db.scalar(select(Role).where(Role.code == r["code"]))
        if role is None:
            db.add(Role(code=r["code"], name=r["name"], description=r["description"], permissions=list(r["permissions"]),
                        builtin=True))
            added += 1
        elif r["code"] == "admin":
            role.permissions, role.builtin = ["*"], True
    db.flush()
    return added


def admin_role(db: Session) -> Role:
    ensure_roles(db)
    role = db.scalar(select(Role).where(Role.code == "admin"))
    assert role is not None
    return role


def ensure_admin(db: Session, password: str, username: str = "admin", email: str | None = None) -> bool:
    """仅当系统中还没有任何用户时创建首个管理员（平台管理员角色、全部租户），避免重复执行时覆盖已有口令。"""
    if db.scalar(select(func.count()).select_from(User)):
        return False
    db.add(User(username=username, display_name="管理员", email=email, password_hash=hash_password(password),
                role_id=admin_role(db).id, tenant_ids=None))
    db.flush()
    return True


def bootstrap(db: Session, admin_password: str | None = None) -> dict:
    result = {"roles": ensure_roles(db), "tasks": ensure_tasks(db), "rules": sync_default_rules(db),
              "admin_created": ensure_admin(db, admin_password) if admin_password else False}
    db.commit()
    return result
