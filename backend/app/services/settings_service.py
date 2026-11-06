"""页面可修改的系统设置（sys_setting 表，键 → JSON）。

- 环境变量（INSPECTOR_*）里的同名配置只作为默认值；页面保存后以数据库为准，各进程每次使用时读取（无需重启）
- 每个键对应一个 Pydantic 模型：保存前校验取值范围，读取时缺失字段补默认值（新版本加字段不需要迁移）
"""
import math
import re
from typing import Annotated, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BizError, ErrorCode
from app.models import SysSetting, User
from app.services.recipients import emails_for_users, ids_for_emails, validate_user_ids

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
    alarm_closed_days: int = Field(180, ge=30, le=3650, description="已结束的告警（含处理时间线）")
    audit_log_days: int = Field(180, ge=90, le=3650, description="操作审计（建议不少于 180 天）")
    resource_change_days: int = Field(365, ge=30, le=3650, description="台账变更记录")
    released_resource_days: int = Field(365, ge=30, le=3650, description="云上已释放的资源")
    report_days: int = Field(365, ge=30, le=3650, description="历史报表")
    pod_event_days: int = Field(30, ge=1, le=365, description="容器事件")


class ReportSettings(BaseModel):
    """定时报表：每类报表的发送时间（crontab，平台时区）、是否启用、合并报表收件人。租户报表收件人在租户接入里配置。"""

    cron: dict[str, str] = Field(default_factory=dict, description="发送时间（crontab：分 时 日 月 周）")
    enabled: dict[str, bool] = Field(default_factory=dict, description="是否启用")
    recipient_user_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(
        default_factory=list, description="合并报表收件人员")
    mail_to: list[str] = Field(default_factory=list, description="合并报表收件邮箱（只读，来自用户管理）")

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

    enabled: bool = Field(True, description="启用平台自身告警")
    task_fail_threshold: int = Field(3, ge=1, le=20, description="任务连续失败 N 次告警（按租户计）")
    auth_fail_alarm: bool = Field(True, description="租户认证失败（401/403）立即告警")
    notify_fail_alarm: bool = Field(True, description="通知重试 5 次仍失败时告警")
    heartbeat_timeout_min: int = Field(3, ge=1, le=60, description="后台进程无心跳 N 分钟告警")
    severity: Literal["P1", "P2", "P3"] = Field("P2", description="平台告警级别")
    recipient_user_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(
        default_factory=list, description="平台告警收件人员")
    mail_to: list[str] = Field(default_factory=list, description="收件邮箱（只读）；未选择人员时发给全租户平台管理员")

    @field_validator("mail_to")
    @classmethod
    def _mail(cls, v: list[str]) -> list[str]:
        return _emails(v)


class DictItem(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    remark: str | None = Field(None, max_length=128)


class LedgerDict(BaseModel):
    """业务系统字典；owners 为统一用户目录的只读兼容投影。"""

    business: list[DictItem] = Field(default_factory=list, description="业务系统")
    owners: list[DictItem] = Field(default_factory=list, description="负责人（只读，来自用户管理）")
    enforce: bool = Field(False, description="业务系统只能从字典选择")

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


class CollectionSettings(BaseModel):
    hcs_concurrency: int = Field(4, ge=1, le=16, description="HCS 明细请求并发数")
    hcs_qps: dict[str, float] = Field(default_factory=dict, description="各服务每秒请求数上限")

    @field_validator("hcs_qps")
    @classmethod
    def _qps(cls, value: dict[str, float]) -> dict[str, float]:
        from app.hcs.factory import DEFAULT_QPS

        if any(service not in DEFAULT_QPS for service in value):
            raise ValueError("QPS 配置包含未知云服务")
        if any(not math.isfinite(rate) or not 0.1 <= rate <= 1000 for rate in value.values()):
            raise ValueError("QPS 必须为 0.1 到 1000 之间的有限数值")
        return {**DEFAULT_QPS, **value}


MODELS: dict[str, type[BaseModel]] = {"retention": Retention, "report": ReportSettings,
                                      "platform_alarm": PlatformAlarm, "ledger_dict": LedgerDict,
                                      "collect": CollectionSettings}
LABELS = {"retention": "数据保留", "report": "定时报表", "platform_alarm": "平台自身告警", "ledger_dict": "台账字典",
          "collect": "HCS 采集性能"}


def _defaults(key: str) -> dict:
    s = get_settings()
    if key == "collect":
        from app.hcs.factory import DEFAULT_QPS

        return {"hcs_qps": dict(DEFAULT_QPS)}
    if key == "retention":
        return {"pod_event_days": s.pod_event_retention_days}
    if key == "report":
        return {"cron": dict(s.report_cron), "enabled": {k: bool(s.report_cron.get(k)) for k in REPORT_KINDS},
                "mail_to": list(s.report_mail_to)}
    return {}


def _recipient_values(db: Session, data: dict) -> dict:
    if "recipient_user_ids" in data:
        raw = data["recipient_user_ids"]
        ids = list(dict.fromkeys(uid for uid in raw if type(uid) is int and uid > 0)) if isinstance(raw, list) else []
    else:
        # 包括 REPORT_MAIL_TO 环境默认值：只引用已登记且范围匹配的人员。
        ids = ids_for_emails(db, data.get("mail_to", []), strict=False)
    return {"recipient_user_ids": ids, "mail_to": emails_for_users(db, ids)}


def get(db: Session, key: str) -> BaseModel:
    with db.no_autoflush:
        model = MODELS[key]
        row = db.scalar(select(SysSetting).where(SysSetting.key == key).execution_options(populate_existing=True))
        data = {**_defaults(key), **((row.value if row else None) or {})}
        recipients = _recipient_values(db, data) if key in {"report", "platform_alarm"} else {}
        data.update(recipients)
        if key == "ledger_dict":
            names = sorted(set(db.scalars(select(User.display_name).where(User.enabled.is_(True)))))
            data["owners"] = [{"name": name} for name in names]
        try:
            return model.model_validate(data)
        except ValueError:                     # 其他历史字段非法时仍保留 ID 权威，不能退回任意旧邮箱。
            return model.model_validate({**_defaults(key), **recipients})


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


def get_collect(db: Session) -> CollectionSettings:
    value = get(db, "collect")
    assert isinstance(value, CollectionSettings)
    return value


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
    if key == "ledger_dict" and "owners" in data:
        owners = data["owners"]
        valid = isinstance(owners, list) and all(
            isinstance(item, dict) and isinstance(item.get("name"), str) for item in owners)
        if not valid or sorted(item["name"] for item in owners) != sorted(item["name"] for item in before["owners"]):
            raise BizError(ErrorCode.BAD_REQUEST, "负责人由用户管理统一维护，不能修改负责人字典")
        data = {k: v for k, v in data.items() if k != "owners"}
    if key in {"report", "platform_alarm"}:
        if "recipient_user_ids" in data:
            ids = data["recipient_user_ids"]
        elif "mail_to" in data:
            ids = ids_for_emails(db, data["mail_to"])
        else:
            ids = before["recipient_user_ids"]
        ids = validate_user_ids(db, ids, existing_ids=before["recipient_user_ids"])
        data = {**data, "recipient_user_ids": ids, "mail_to": emails_for_users(db, ids)}
    try:
        value = MODELS[key].model_validate({**before, **data})
    except ValidationError as e:
        raise BizError(ErrorCode.BAD_REQUEST, explain(e, MODELS[key])) from e
    after = value.model_dump(mode="json")
    row = db.get(SysSetting, key)
    stored = after
    if key == "ledger_dict":
        stored = {**after, "owners": (row.value or {}).get("owners", []) if row else []}
    if row is None:
        row = SysSetting(key=key, value=stored, updated_by=operator)
        db.add(row)
    else:
        row.value, row.updated_by = stored, operator
    db.flush()
    return value, {k: [before.get(k), after[k]] for k in after if before.get(k) != after[k]}


def meta(db: Session, key: str) -> dict:
    row = db.get(SysSetting, key)
    return {"updated_by": row.updated_by if row else None, "updated_at": row.updated_at if row else None,
            "customized": row is not None}
