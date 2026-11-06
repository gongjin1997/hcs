"""生产初始化（幂等，可重复执行）：内置角色、内置规则、巡检任务（含台账同步）、首个管理员。

与演示数据（seed.py）的区别：这里只写平台运行必需的基础数据，不写任何演示资源、告警和账号口令。
用法：迁移完成后执行 python -m app.workers.bootstrap；首次部署时通过 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin。
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.permissions import BUILTIN_ROLES
from app.core.security import hash_password
from app.models import HcsAccount, InspectionTask, Resource, Role, User
from app.rules.loader import sync_default_rules
from app.services import collect_switch
from app.services.catalog import TASK_SPECS

# (任务代码, 名称, 服务, 默认周期秒[, crontab])：由 catalog.TASK_SPECS 派生，保留元组形式供旧调用方使用
BUILTIN_TASKS: list[tuple] = [(t.code, t.name, t.service, t.interval_sec, *((t.cron,) if t.cron else ()))
                              for t in TASK_SPECS]


def ensure_tasks(db: Session) -> int:
    """补齐内置任务；已存在的任务不覆盖（保留页面上调整过的周期、启停状态）。"""
    added = 0
    for spec in TASK_SPECS:
        if db.get(InspectionTask, spec.code) is None:
            db.add(InspectionTask(code=spec.code, name=spec.name, service=spec.service, interval_sec=spec.interval_sec,
                                  cron=spec.cron, params={},
                                  enabled=get_settings().env != "prod" and spec.default_enabled))
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
    # 只初始化真正的新生产库；已有用户、租户、资源或任务均表明旧环境，不能重置其全局开关。
    if get_settings().env == "prod" and not any(
        db.scalar(select(model).limit(1)) is not None for model in (User, HcsAccount, Resource, InspectionTask)
    ):
        collect_switch.initialize_disabled(db, collect_switch.GLOBAL, "bootstrap")
    result = {"roles": ensure_roles(db), "tasks": ensure_tasks(db), "rules": sync_default_rules(db),
              "admin_created": ensure_admin(db, admin_password) if admin_password else False}
    db.commit()
    return result
