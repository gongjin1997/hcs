"""操作审计：登录、权限与租户配置变更、告警处置、台账维护、报表发送等写操作都会记一条（audit_log）。

用法：路由里注入 audit: Auditor = Depends(get_auditor)，业务成功后 audit.log("alarm.ack", target=...)；
登录等匿名接口用 get_anon_auditor，并显式传入 username / operator。
"""
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import event
from sqlalchemy.orm import Session

from .db import get_db
from .deps import CurrentUser, get_current_user

log = logging.getLogger("audit")
_PENDING = "_audit_log_records"


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    if session.in_nested_transaction():
        return
    for _, fields in session.info.pop(_PENDING, []):
        log.info("audit.committed", extra=fields)


@event.listens_for(Session, "after_soft_rollback")
def _rolled_back(session: Session, previous_transaction) -> None:
    def belongs(transaction) -> bool:
        while transaction is not None:
            if transaction is previous_transaction:
                return True
            transaction = transaction.parent
        return False

    if previous_transaction.parent is not None:
        session.info[_PENDING] = [(tx, fields) for tx, fields in session.info.get(_PENDING, []) if not belongs(tx)]
    else:
        session.info.pop(_PENDING, None)

ACTIONS: dict[str, str] = {
    "auth.login": "登录", "auth.login_failed": "登录失败",
    "auth.password": "修改密码",
    "sms.global.update": "修改短信总开关", "sms.tenant.update": "修改租户短信配置",
    "user.import": "Excel 导入用户", "user.export": "导出用户",
    "user.create": "新增用户", "user.update": "修改用户", "user.reset_password": "重置密码",
    "role.create": "新增角色", "role.update": "修改角色", "role.delete": "删除角色",
    "tenant.create": "新增租户", "tenant.update": "修改租户", "tenant.delete": "删除租户", "tenant.test": "租户连通性测试",
    "rule.create": "新增规则", "rule.update": "修改规则",
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
    "notify.test_smtp": "测试 SMTP 连接", "notify.test_imap": "测试 IMAP 连接",
    "collect.update": "修改采集开关", "setting.update": "修改系统设置", "report.schedule": "修改报表设置",
    "ledger.classification": "维护资源分类映射",
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
        # 文件日志只镜像已提交的动作元数据；明细仍在审计表，不输出口令/业务字段。
        self.db.info.setdefault(_PENDING, []).append((
            self.db.get_nested_transaction() or self.db.get_transaction(),
            {"action": action, "target_type": target_type, "target_id": str(target_id) if target_id is not None else None,
             "tenant_id": tenant_id, "user_id": user_id if user_id is not None else (u.id if u else None),
             "result": result, "client_ip": self.ip}))
        if commit:
            self.db.commit()


def get_auditor(request: Request, db: Session = Depends(get_db),
                user: CurrentUser = Depends(get_current_user)) -> Auditor:
    return Auditor(db, user, client_ip(request))


def get_anon_auditor(request: Request, db: Session = Depends(get_db)) -> Auditor:
    return Auditor(db, None, client_ip(request))
