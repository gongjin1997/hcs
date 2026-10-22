"""页面可修改的系统设置（sys_setting 表，键 → JSON）。

- 环境变量（INSPECTOR_*）里的同名配置只作为默认值；页面保存后以数据库为准，各进程每次使用时读取（无需重启）
- 每个键对应一个 Pydantic 模型：保存前校验取值范围，读取时缺失字段补默认值（新版本加字段不需要迁移）
"""
import re
from typing import Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BizError, ErrorCode
from app.models import SysSetting

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REPORT_KINDS = ("DAILY", "WEEKLY", "MONTHLY")


def _emails(v: list[str]) -> list[str]:
    out: list[str] = []
    for e in (x.strip() for x in v):
        if not e:
            continue
        if not EMAIL.match(e):
            raise ValueError(f"邮箱格式不正确：{e}")
        if e not in out:
            out.append(e)
    return out


class Retention(BaseModel):
    """数据保留天数（每天凌晨“数据清理”任务按此删除过期数据）。"""

    inspection_run_days: int = Field(90, ge=7, le=3650, description="巡检 / 同步运行记录")
    notification_days: int = Field(90, ge=7, le=3650, description="通知发送记录")
    alarm_closed_days: int = Field(180, ge=30, le=3650, description="已恢复 / 已关闭的告警（含处理时间线）")
    audit_log_days: int = Field(180, ge=90, le=3650, description="操作审计（建议不少于 180 天）")
    resource_change_days: int = Field(365, ge=30, le=3650, description="台账变更记录")
    released_resource_days: int = Field(365, ge=30, le=3650, description="云上已释放的资源")
    report_days: int = Field(365, ge=30, le=3650, description="历史报表")
    pod_event_days: int = Field(30, ge=1, le=365, description="容器事件")


class ReportSettings(BaseModel):
    """定时报表：每类报表的发送时间（crontab，平台时区）、是否启用、合并报表收件人。租户报表收件人在租户接入里配置。"""

    cron: dict[str, str] = {}
    enabled: dict[str, bool] = {}
    mail_to: list[str] = []

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: dict[str, str]) -> dict[str, str]:
        out = {}
        for k, expr in v.items():
            if k not in REPORT_KINDS:
                raise ValueError(f"未知报表类型：{k}")
            expr = " ".join(expr.split())
            try:
                CronTrigger.from_crontab(expr)
            except ValueError as e:
                raise ValueError(f"{k} 的 crontab 表达式不正确：{expr}") from e
            out[k] = expr
        return out

    @field_validator("mail_to")
    @classmethod
    def _mail(cls, v: list[str]) -> list[str]:
        return _emails(v)


class PlatformAlarm(BaseModel):
    """平台自身告警：任务连续失败、租户认证失败、通知最终发送失败、后台进程心跳超时。"""

    enabled: bool = True
    task_fail_threshold: int = Field(3, ge=1, le=20, description="同一任务同一租户连续失败 N 次告警")
    auth_fail_alarm: bool = True
    notify_fail_alarm: bool = True
    heartbeat_timeout_min: int = Field(3, ge=1, le=60, description="调度 / 通知进程超过 N 分钟无心跳告警")
    severity: Literal["P1", "P2", "P3"] = "P2"
    mail_to: list[str] = Field(default_factory=list, description="没有通知路由命中时的收件人；为空发给有邮箱的平台管理员")

    @field_validator("mail_to")
    @classmethod
    def _mail(cls, v: list[str]) -> list[str]:
        return _emails(v)


class DictItem(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    remark: str | None = Field(None, max_length=128)


class LedgerDict(BaseModel):
    """台账字典：业务系统、负责人的统一取值（台账编辑和 Excel 导入时使用，避免同一个系统多种写法）。"""

    business: list[DictItem] = []
    owners: list[DictItem] = []
    enforce: bool = False          # 开启后只能从字典中选择（Excel 导入时字典外的取值会被拒绝）

    @field_validator("business", "owners")
    @classmethod
    def _unique(cls, v: list[DictItem]) -> list[DictItem]:
        seen: set[str] = set()
        out = []
        for i in v:
            i.name = i.name.strip()
            if i.name and i.name not in seen:
                seen.add(i.name)
                out.append(i)
        return out


MODELS: dict[str, type[BaseModel]] = {"retention": Retention, "report": ReportSettings,
                                      "platform_alarm": PlatformAlarm, "ledger_dict": LedgerDict}
LABELS = {"retention": "数据保留", "report": "定时报表", "platform_alarm": "平台自身告警", "ledger_dict": "台账字典"}


def _defaults(key: str) -> dict:
    s = get_settings()
    if key == "retention":
        return {"pod_event_days": s.pod_event_retention_days}
    if key == "report":
        return {"cron": dict(s.report_cron), "enabled": {k: bool(s.report_cron.get(k)) for k in REPORT_KINDS},
                "mail_to": list(s.report_mail_to)}
    return {}


def get(db: Session, key: str) -> BaseModel:
    model = MODELS[key]
    row = db.get(SysSetting, key)
    data = {**_defaults(key), **((row.value if row else None) or {})}
    try:
        return model.model_validate(data)
    except ValueError:                     # 库里的历史值不合法（例如手工改库）：退回默认值，不影响运行
        return model.model_validate(_defaults(key))


def get_retention(db: Session) -> Retention:
    v = get(db, "retention")
    assert isinstance(v, Retention)
    return v


def get_report(db: Session) -> ReportSettings:
    v = get(db, "report")
    assert isinstance(v, ReportSettings)
    return v


def get_platform_alarm(db: Session) -> PlatformAlarm:
    v = get(db, "platform_alarm")
    assert isinstance(v, PlatformAlarm)
    return v


def get_ledger_dict(db: Session) -> LedgerDict:
    v = get(db, "ledger_dict")
    assert isinstance(v, LedgerDict)
    return v


def explain(e: ValidationError, model: type[BaseModel]) -> str:
    """Pydantic 校验错误 → 中文提示（字段用 description 作为名称）。"""
    msgs = []
    for err in e.errors():
        field = str(err["loc"][0]) if err["loc"] else ""
        info = model.model_fields.get(field)
        label = (info.description if info and info.description else field) or "参数"
        ctx = err.get("ctx") or {}
        kind = err["type"]
        if kind == "greater_than_equal":
            msgs.append(f"{label}不能小于 {ctx.get('ge')}")
        elif kind == "less_than_equal":
            msgs.append(f"{label}不能大于 {ctx.get('le')}")
        elif kind == "value_error":
            msgs.append(str(ctx.get("error") or err["msg"]).removeprefix("Value error, "))
        elif kind in ("int_parsing", "int_type", "bool_parsing", "bool_type"):
            msgs.append(f"{label}取值类型不正确")
        else:
            msgs.append(f"{label}：{err['msg']}")
    return "；".join(msgs)


def save(db: Session, key: str, data: dict, operator: str) -> tuple[BaseModel, dict]:
    """校验并保存，返回（新值, 变更明细 {字段: [旧, 新]}）。调用方负责提交事务和写审计。"""
    if key not in MODELS:
        raise BizError(ErrorCode.NOT_FOUND, f"未知设置项：{key}", 404)
    before = get(db, key).model_dump(mode="json")
    try:
        value = MODELS[key].model_validate({**before, **data})
    except ValidationError as e:
        raise BizError(ErrorCode.BAD_REQUEST, explain(e, MODELS[key])) from e
    after = value.model_dump(mode="json")
    row = db.get(SysSetting, key)
    if row is None:
        row = SysSetting(key=key, value=after, updated_by=operator)
        db.add(row)
    else:
        row.value, row.updated_by = after, operator
    db.flush()
    return value, {k: [before.get(k), after[k]] for k in after if before.get(k) != after[k]}


def meta(db: Session, key: str) -> dict:
    row = db.get(SysSetting, key)
    return {"updated_by": row.updated_by if row else None, "updated_at": row.updated_at if row else None,
            "customized": row is not None}
