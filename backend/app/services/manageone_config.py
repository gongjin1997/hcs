"""ManageOne 独立配置：只保存租户配置，不连接云端、不触碰采集开关或任务。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import PREFIX, encrypt
from app.core.response import BizError, ErrorCode, not_found
from app.models import AlarmEvent, AlarmEventLog, HcsAccount
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.manageone import ManageOneConfigUpdate

_PUBLIC_FIELDS = ("enabled", "endpoint", "username", "organization")


def get_config(tenant: HcsAccount) -> dict:
    """内部读取契约，含 secret_cipher；不解密、不读取或回退 IAM 凭据。"""
    raw = tenant.manageone_config if isinstance(tenant.manageone_config, dict) else {}
    try:
        config = ManageOneConfigUpdate.model_validate({key: raw[key] for key in _PUBLIC_FIELDS if key in raw})
    except ValueError:
        config = ManageOneConfigUpdate.model_validate({})  # 损坏配置关闭，不能使用不安全的地址。
        raw = {}
    cipher = raw.get("secret_cipher")
    return {**config.model_dump(include=set(_PUBLIC_FIELDS)),
            "secret_cipher": cipher if isinstance(cipher, str) and cipher.startswith(PREFIX) else ""}


def public_config(tenant: HcsAccount) -> dict:
    config = get_config(tenant)
    secret_set = bool(config["secret_cipher"])
    return {"tenant_id": tenant.id, **{key: config[key] for key in _PUBLIC_FIELDS},
            "configured": bool(config["endpoint"] and config["username"] and secret_set),
            "secret_set": secret_set, "selected": tenant.metrics_source == "MANAGEONE"}


def save_config(db: Session, tenant: HcsAccount, body: ManageOneConfigUpdate, operator: str = "system") -> dict:
    """由路由与审计一并提交；校验全部成功后才写配置及显式来源选择。"""
    locked = db.scalar(select(HcsAccount).where(HcsAccount.id == tenant.id).with_for_update()
                       .execution_options(populate_existing=True))
    if locked is None:
        raise not_found("租户")
    before = get_config(locked)
    values = {**before, **body.model_dump(include=set(_PUBLIC_FIELDS), exclude_unset=True)}
    if body.password and body.clear_password:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时填写和清除 ManageOne 密码")
    if body.clear_password and values["enabled"]:
        raise BizError(ErrorCode.BAD_REQUEST, "清除 ManageOne 密码前须先禁用配置")
    changed_identity = [label for key, label in (("endpoint", "地址"), ("username", "用户名"), ("organization", "组织"))
                        if (values[key] or "") != (before[key] or "")]
    if changed_identity and before["secret_cipher"] and not body.password and not body.clear_password:
        raise BizError(ErrorCode.BAD_REQUEST,
                       f"修改 ManageOne {'、'.join(changed_identity)}后须重新填写密码；或禁用配置并显式清除旧密码")
    if body.password:
        if set(body.password) == {"*"}:
            raise BizError(ErrorCode.BAD_REQUEST, "请填写实际 ManageOne 密码；密码留空保留原密码")
        values["secret_cipher"] = encrypt(body.password)
    elif body.clear_password:
        values["secret_cipher"] = ""
    complete = bool(values["endpoint"] and values["username"] and values["secret_cipher"])
    if values["enabled"] and not complete:
        raise BizError(ErrorCode.BAD_REQUEST, "启用 ManageOne 前须填写完整的地址、用户名和独立密码")
    if body.select_source and not (values["enabled"] and complete):
        raise BizError(ErrorCode.BAD_REQUEST, "用于 ECS 指标采集前，须先启用并完整配置 ManageOne（地址、用户名和独立密码）")
    changes = {key: [before[key], values[key]] for key in _PUBLIC_FIELDS if before[key] != values[key]}
    secret_changed = before["secret_cipher"] != values["secret_cipher"]
    closed_alarms = 0
    if body.select_source and locked.metrics_source != "MANAGEONE":
        changes["metrics_source"] = [locked.metrics_source, "MANAGEONE"]
        now = utcnow()
        alarms = db.scalars(select(AlarmEvent).where(AlarmEvent.tenant_id == locked.id,
            AlarmEvent.resource_type == "ecs_server", AlarmEvent.check_item == "disk",
            AlarmEvent.status.in_(ACTIVE_STATUSES)).order_by(AlarmEvent.id).with_for_update())
        for alarm in alarms:
            previous, alarm.status = alarm.status, "CLOSED"
            alarm.resolved_at = alarm.resolved_at or now
            db.add(AlarmEventLog(alarm_id=alarm.id, action="CLOSE", from_status=previous, to_status="CLOSED",
                operator=operator, note="切换至 ManageOne 云硬盘指标；文件系统分区规则不再适用，行政关闭（非指标恢复）",
                at=now))
            closed_alarms += 1
        locked.metrics_source = "MANAGEONE"
    # 替换整个 JSON，确保 ORM 与主代理的 config_signature 能识别任何配置变更。
    locked.manageone_config = values
    db.flush()
    return {"changes": changes, "password_changed": secret_changed, "closed_disk_alarms": closed_alarms}
