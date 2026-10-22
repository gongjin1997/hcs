"""权限模型：功能权限看角色（一组权限点），数据权限看用户的租户范围（tenant_ids 为空 = 全部租户）。

- 权限点按“模块:动作”命名，前端菜单 / 按钮与后端接口使用同一套编码（v-perm="'alarm:handle'"、require_perm("alarm:handle")）
- 内置 4 个角色，首次启动写入 sys_role；之后可在“角色与权限”页面调整，也可新增自定义角色
- "*" 表示全部权限（平台管理员）
"""

ALL = "*"

PERMISSIONS: list[dict[str, str]] = [
    {"code": "dashboard:view", "name": "查看总览 / 大屏", "group": "总览"},
    {"code": "alarm:view", "name": "查看告警", "group": "告警中心"},
    {"code": "alarm:handle", "name": "认领 / 关闭告警", "group": "告警中心"},
    {"code": "podevent:view", "name": "查看容器事件", "group": "容器事件"},
    {"code": "podevent:config", "name": "配置背压探针 / 手动采集", "group": "容器事件"},
    {"code": "ledger:view", "name": "查看资源台账", "group": "资源台账"},
    {"code": "ledger:edit", "name": "维护管理字段", "group": "资源台账"},
    {"code": "ledger:sync", "name": "手动同步台账", "group": "资源台账"},
    {"code": "ledger:export", "name": "导出台账 Excel", "group": "资源台账"},
    {"code": "report:view", "name": "查看 / 导出报表", "group": "报表中心"},
    {"code": "report:manage", "name": "生成 / 发送报表", "group": "报表中心"},
    {"code": "report:delete", "name": "删除报表", "group": "报表中心"},
    {"code": "rule:view", "name": "查看规则", "group": "规则管理"},
    {"code": "rule:edit", "name": "修改规则", "group": "规则管理"},
    {"code": "notify:view", "name": "查看通知路由与记录", "group": "通知"},
    {"code": "task:view", "name": "查看巡检任务", "group": "系统管理"},
    {"code": "task:run", "name": "立即执行任务", "group": "系统管理"},
    {"code": "tenant:view", "name": "查看租户接入", "group": "系统管理"},
    {"code": "tenant:manage", "name": "管理租户接入（含 AK/SK）", "group": "系统管理"},
    {"code": "user:manage", "name": "用户管理", "group": "系统管理"},
    {"code": "role:manage", "name": "角色与权限", "group": "系统管理"},
    {"code": "audit:view", "name": "查看操作审计", "group": "系统管理"},
]
PERMISSION_CODES: list[str] = [p["code"] for p in PERMISSIONS]
PERMISSION_NAMES: dict[str, str] = {p["code"]: p["name"] for p in PERMISSIONS}

_VIEW = ["dashboard:view", "alarm:view", "podevent:view", "ledger:view", "report:view", "rule:view", "notify:view",
         "task:view"]
_SYSTEM = {"tenant:manage", "user:manage", "role:manage"}

BUILTIN_ROLES: list[dict] = [
    {"code": "admin", "name": "平台管理员", "permissions": [ALL],
     "description": "全部功能，包括租户接入（AK/SK）、用户与角色管理"},
    {"code": "ops_lead", "name": "运维负责人", "permissions": [c for c in PERMISSION_CODES if c not in _SYSTEM],
     "description": "全部业务功能（含规则修改、报表删除、操作审计），不含系统管理"},
    {"code": "oncall", "name": "值班运维",
     "permissions": [*_VIEW, "tenant:view", "alarm:handle", "ledger:edit", "ledger:sync", "ledger:export",
                     "report:manage", "task:run"],
     "description": "日常值班：处置告警、维护台账、生成 / 发送报表、执行巡检任务"},
    {"code": "viewer", "name": "只读访客", "permissions": list(_VIEW),
     "description": "只能查看，不能做任何修改（适合领导、业务方）"},
]


def expand(perms: list[str] | None) -> list[str]:
    """角色权限点 → 实际生效的权限点（"*" 展开为全部；忽略已下线的未知编码）。"""
    perms = perms or []
    if ALL in perms:
        return list(PERMISSION_CODES)
    return [c for c in PERMISSION_CODES if c in perms]


def normalize(perms: list[str]) -> list[str]:
    """保存角色前校验：未知权限点报错。"""
    unknown = [p for p in perms if p != ALL and p not in PERMISSION_NAMES]
    if unknown:
        raise ValueError(f"未知权限点：{', '.join(unknown)}")
    return [ALL] if ALL in perms else [c for c in PERMISSION_CODES if c in perms]
