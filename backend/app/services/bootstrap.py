"""生产初始化（幂等，可重复执行）：内置角色、内置规则、巡检任务（含台账同步）、首个管理员。

与演示数据（seed.py）的区别：这里只写平台运行必需的基础数据，不写任何演示资源、告警和账号口令。
用法：迁移完成后执行 python -m app.workers.bootstrap；首次部署时通过 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin。
"""
from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.permissions import BUILTIN_ROLES
from app.core.security import hash_password
from app.models import CollectSwitch, HcsAccount, InspectionTask, Resource, Role, SysSetting, User
from app.rules.loader import sync_default_rules
from app.services import collect_switch
from app.services.catalog import TASK_SPECS

# (任务代码, 名称, 服务, 默认周期秒[, crontab])：由 catalog.TASK_SPECS 派生，保留元组形式供旧调用方使用
BUILTIN_TASKS: list[tuple] = [(t.code, t.name, t.service, t.interval_sec, *((t.cron,) if t.cron else ()))
                              for t in TASK_SPECS]


def _mrs_scope_dims(value) -> set[str] | None:
    rules = value.get("rules") if isinstance(value, dict) else None
    if not isinstance(rules, list):
        return None
    dims = set()
    for rule in rules:
        if (not isinstance(rule, dict) or rule.get("effect") not in ("include", "exclude")
                or not isinstance(rule.get("match"), dict) or not rule["match"]):
            return None
        for key, patterns in rule["match"].items():
            if (not isinstance(key, str) or not isinstance(patterns, list) or not patterns
                    or any(not isinstance(pattern, str) or not pattern for pattern in patterns)):
                return None
            dims.add(key)
    return dims


def _mrs_split_config(db: Session, old: InspectionTask | None, *, fresh_install: bool) -> None:
    """create_all/旧部署的幂等配置补齐；正式数据和告警迁移仍由 0022 负责。"""
    original = {row.tenant_id: row.enabled for row in db.scalars(select(CollectSwitch).where(
        CollectSwitch.resource_type == "mrs_job"))}
    for tid, enabled in {0: old is not None, **original}.items():
        if db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == tid,
                                                CollectSwitch.resource_type == "dgc_mrs_node")) is None:
            db.add(CollectSwitch(tenant_id=tid, resource_type="dgc_mrs_node", enabled=enabled,
                                 updated_by="system:mrs-split-0022"))
    if old is None:
        # 原任务缺失不等于空库；旧环境的集群授权（含无记录=开启）不能被重置。
        for kind in ("mrs_job", "mrs_cluster") if fresh_install else ():
            if db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == 0,
                                                    CollectSwitch.resource_type == kind)) is None:
                db.add(CollectSwitch(tenant_id=0, resource_type=kind, enabled=False, updated_by="system"))
        return
    legacy = {}
    for setting in list(db.scalars(select(SysSetting).where(SysSetting.key.like("collect_scope:%:mrs_job")))):
        parts = setting.key.split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        value = setting.value
        dims = _mrs_scope_dims(value)
        tid = int(parts[1])
        legacy[tid] = {
            "native_block": dims is None or bool(dims) and (
                dims <= {"job"} or bool(dims - {"cluster", "job", "id"})),
            "node_block": dims is None or bool(dims - {"workspace", "job", "node"}),
        }
        key = f"collect_scope:{tid}:dgc_mrs_node"
        if db.get(SysSetting, key) is None:
            db.add(SysSetting(key=key, value=deepcopy(value), updated_by=setting.updated_by))
    # Materialize inherited switches/scopes before validating each type's independent authorization.
    db.flush()
    node_scopes = {}
    for setting in db.scalars(select(SysSetting).where(SysSetting.key.like("collect_scope:%:dgc_mrs_node"))):
        parts = setting.key.split(":")
        if len(parts) == 3 and parts[1].isdigit():
            dims = _mrs_scope_dims(setting.value)
            node_scopes[int(parts[1])] = dims is None or bool(dims - {"workspace", "job", "node"})
    for tid in {0, *db.scalars(select(HcsAccount.id)), *original, *legacy, *node_scopes}:
        native_block = legacy.get(tid, legacy.get(0, {})).get("native_block", False)
        node_block = node_scopes.get(tid, node_scopes.get(0, False))
        for kind, blocked in (("mrs_job", native_block), ("dgc_mrs_node", node_block)):
            if not blocked:
                continue
            row = db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == tid,
                                                       CollectSwitch.resource_type == kind))
            if row is None:
                db.add(CollectSwitch(tenant_id=tid, resource_type=kind, enabled=False,
                                     updated_by="system:mrs-split-0022"))
            elif row.enabled:
                row.enabled, row.updated_by = False, "system:mrs-split-0022"


def ensure_tasks(db: Session) -> int:
    """补齐内置任务；已存在的任务不覆盖（保留页面上调整过的周期、启停状态）。"""
    added = 0
    old_mrs = db.get(InspectionTask, "mrs_job_status")
    if old_mrs is None or db.get(InspectionTask, "dgc_mrs_node_status") is None:
        fresh_install = not any(db.scalar(select(model).limit(1)) is not None
                                for model in (User, HcsAccount, Resource, InspectionTask))
        _mrs_split_config(db, old_mrs, fresh_install=fresh_install)
    mrs_schedule = ({k: deepcopy(getattr(old_mrs, k)) for k in ("interval_sec", "cron", "enabled", "params")}
                    if old_mrs is not None else None)
    # 拆分已有任务时保持管理员原先批准的范围、周期与停用状态；新库仍按生产默认关闭。
    old_gauss = db.get(InspectionTask, "gaussdb_status")
    split_schedule = ({"interval_sec": old_gauss.interval_sec, "cron": old_gauss.cron,
                       "enabled": old_gauss.enabled} if old_gauss is not None else None)
    if old_gauss is not None and old_gauss.name == "GaussDB 实例状态与容量":
        old_gauss.name = "GaussDB 实例状态"
    for spec in TASK_SPECS:
        if db.get(InspectionTask, spec.code) is None:
            schedule = {"interval_sec": spec.interval_sec, "cron": spec.cron,
                        "enabled": get_settings().env != "prod" and spec.default_enabled}
            if split_schedule is not None and spec.code in ("gaussdb_storage", "gaussdb_components"):
                schedule.update(split_schedule)
            params = {}
            if spec.code == "dgc_mrs_node_status" and mrs_schedule is not None:
                schedule.update({k: v for k, v in mrs_schedule.items() if k != "params"})
                params = mrs_schedule["params"]
            db.add(InspectionTask(code=spec.code, name=spec.name, service=spec.service, params=params, **schedule))
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
