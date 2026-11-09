"""初始化与演示数据：内置角色 / 任务 / 规则幂等写入，管理员只创建一次；演示数据只在空库写入一次。"""
from sqlalchemy import func, select

from app.core.security import verify_password
from app.demo.seed import seed_demo
from app.models import (
    AlarmRule,
    AuditLog,
    HcsAccount,
    InspectionTask,
    PodEvent,
    Report,
    Resource,
    ResourceChange,
    Role,
    User,
)
from app.services.bootstrap import BUILTIN_TASKS, bootstrap

BOOT = "Boot-pass-001"        # 仅测试库


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_bootstrap_idempotent_and_creates_admin_once(mem_db):
    first = bootstrap(mem_db, admin_password=BOOT)
    assert first["tasks"] == len(BUILTIN_TASKS) and first["rules"] > 0 and first["admin_created"] is True
    assert first["roles"] == 4 and {r.code for r in mem_db.scalars(select(Role))} == {"admin", "ops_lead", "oncall",
                                                                                     "viewer"}
    assert mem_db.get(InspectionTask, "ledger_sync") is not None and mem_db.get(InspectionTask, "cce_pod_events")
    admin = mem_db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None and verify_password(BOOT, admin.password_hash)
    role = mem_db.get(Role, admin.role_id)
    assert role.code == "admin" and role.permissions == ["*"] and admin.tenant_ids is None
    second = bootstrap(mem_db, admin_password="Other-pass-002")
    assert second == {"roles": 0, "tasks": 0, "rules": 0, "admin_created": False}
    assert verify_password(BOOT, mem_db.get(User, admin.id).password_hash)
    assert _count(mem_db, InspectionTask) == len(BUILTIN_TASKS)


def test_demo_seed_after_bootstrap_without_admin(demo_template, demo_db):
    # 模板库由 conftest.demo_template 按“bootstrap（不建 admin）→ seed_demo”写入，会话内只写一次
    assert demo_template["users_after_bootstrap"] == 0
    assert demo_template["seeded"] is True
    assert demo_db.scalar(select(func.count()).select_from(User).where(User.login_enabled.is_(True))) == 7
    assert demo_db.scalar(select(func.count()).select_from(User).where(User.login_enabled.is_(False))) >= 3
    assert _count(demo_db, User) >= 10 and seed_demo(demo_db) is False
    assert _count(demo_db, HcsAccount) == 2
    assert _count(demo_db, AlarmRule) == len({r.id for r in demo_db.scalars(select(AlarmRule))})
    assert _count(demo_db, Resource) == 227 and _count(demo_db, ResourceChange) >= 40
    assert _count(demo_db, Report) == 22 and _count(demo_db, PodEvent) > 50 and _count(demo_db, AuditLog) > 100
    # 每条资源 / 告警都归属租户，负责人只留给“新纳管待认领”的少数资源
    assert demo_db.scalar(select(func.count()).select_from(Resource).where(Resource.tenant_id.is_(None))) == 0
    no_owner = demo_db.scalars(select(Resource.name).where(Resource.owner.is_(None),
                                                           Resource.resource_type == "ecs_server")).all()
    assert sorted(no_owner) == ["ecs-etl-03", "ecs-portal-03"]
