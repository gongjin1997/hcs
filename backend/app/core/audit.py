"""操作审计：登录、权限与租户配置变更、告警处置、台账维护、报表发送等写操作都会记一条（audit_log）。

用法：路由里注入 audit: Auditor = Depends(get_auditor)，业务成功后 audit.log("alarm.ack", target=...)；
登录等匿名接口用 get_anon_auditor，并显式传入 username / operator。
"""
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import get_db
from .deps import CurrentUser, get_current_user

ACTIONS: dict[str, str] = {
    "auth.login": "登录", "auth.login_failed": "登录失败",
    "auth.password": "修改密码",
    "user.create": "新增用户", "user.update": "修改用户", "user.reset_password": "重置密码",
    "role.create": "新增角色", "role.update": "修改角色", "role.delete": "删除角色",
    "tenant.create": "新增租户", "tenant.update": "修改租户", "tenant.delete": "删除租户", "tenant.test": "租户连通性测试",
    "rule.update": "修改规则",
    "alarm.ack": "认领告警", "alarm.close": "关闭告警", "alarm.batch_ack": "批量认领告警", "alarm.mail_ack": "邮件链接认领",
    "ledger.update": "维护台账字段", "ledger.sync": "手动同步台账", "ledger.export": "导出台账",
    "report.create": "生成报表", "report.send": "发送报表", "report.delete": "删除报表",
    "probe.save": "保存背压探针", "probe.delete": "删除背压探针", "podevent.collect": "手动采集容器事件",
    "task.run": "立即执行任务", "task.update": "修改巡检任务",
    "silence.create": "新建静默 / 维护窗口", "silence.update": "修改静默 / 维护窗口", "silence.cancel": "取消静默",
    "alarm.silence": "快速静默告警",
    "notify.route.create": "新增通知路由", "notify.route.update": "修改通知路由", "notify.route.delete": "删除通知路由",
    "notify.route.reorder": "调整路由顺序", "notify.group.create": "新增联系组", "notify.group.update": "修改联系组",
    "notify.group.delete": "删除联系组", "notify.contact.create": "新增联系人", "notify.contact.update": "修改联系人",
    "notify.contact.delete": "删除联系人", "notify.test_mail": "发送测试邮件",
    "collect.update": "修改采集开关", "setting.update": "修改系统设置", "report.schedule": "修改报表设置",
    "ledger.dict": "维护台账字典", "ledger.import": "Excel 导入台账字段",
}


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else None)


@dataclass
class Auditor:
    db: Session
    user: CurrentUser | None
    ip: str | None

    def log(self, action: str, *, target_type: str | None = None, target_id: Any = None, target_name: str | None = None,
            tenant_id: int | None = None, result: str = "SUCCESS", detail: dict | None = None,
            operator: str | None = None, username: str | None = None, user_id: int | None = None,
            commit: bool = True) -> None:
        from app.models import AuditLog

        u = self.user
        self.db.add(AuditLog(
            user_id=user_id if user_id is not None else (u.id if u else None),
            username=username or (u.username if u else None),
            operator=operator or (u.display_name if u else username or "-"),
            action=action, target_type=target_type, target_id=None if target_id is None else str(target_id),
            target_name=(target_name or "")[:255] or None, tenant_id=tenant_id, result=result,
            detail=detail or {}, ip=self.ip))
        if commit:
            self.db.commit()


def get_auditor(request: Request, db: Session = Depends(get_db),
                user: CurrentUser = Depends(get_current_user)) -> Auditor:
    return Auditor(db, user, client_ip(request))


def get_anon_auditor(request: Request, db: Session = Depends(get_db)) -> Auditor:
    return Auditor(db, None, client_ip(request))
