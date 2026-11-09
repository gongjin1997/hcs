"""ManageOne 独立配置：只保存租户配置，不连接云端、不触碰采集开关或任务。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import PREFIX, encrypt, mask
from app.core.response import BizError, ErrorCode, not_found
from app.models import AlarmEvent, AlarmEventLog, HcsAccount
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.manageone import ManageOneConfigUpdate

_PUBLIC_FIELDS = ("enabled", "endpoint", "auth_type", "username", "organization")


def get_config(tenant: HcsAccount) -> dict:
    """内部读取契约，含 secret_cipher；不解密、不读取或回退 IAM 凭据。"""
    raw = tenant.manageone_config if isinstance(tenant.manageone_config, dict) else {}
    try:
        config = ManageOneConfigUpdate.model_validate({key: raw[key] for key in (*_PUBLIC_FIELDS, "access_key") if key in raw})
    except ValueError:
        config = ManageOneConfigUpdate.model_validate({})  # 损坏配置关闭，不能使用不安全的地址。
        raw = {}
    result = {**config.model_dump(include=set(_PUBLIC_FIELDS)), "access_key": config.access_key or ""}
    for key in ("secret_cipher", "sk_cipher"):
        cipher = raw.get(key)
        result[key] = cipher if isinstance(cipher, str) and cipher.startswith(PREFIX) else ""
    return result


def configured(config: dict) -> bool:
    identity, secret = ("access_key", "sk_cipher") if config.get("auth_type") == "AKSK" else ("username", "secret_cipher")
    return all(isinstance(config.get(key), str) and bool(config[key].strip()) for key in ("endpoint", identity, secret))


def public_config(tenant: HcsAccount) -> dict:
    config = get_config(tenant)
    secret_set = bool(config["secret_cipher"])
    return {"tenant_id": tenant.id, **{key: config[key] for key in _PUBLIC_FIELDS},
            "configured": configured(config), "access_key_mask": mask(config["access_key"]) or None,
            "secret_key_set": bool(config["sk_cipher"]),
            "secret_set": secret_set, "selected": tenant.metrics_source == "MANAGEONE"}


def save_config(db: Session, tenant: HcsAccount, body: ManageOneConfigUpdate, operator: str = "system") -> dict:
    """由路由与审计一并提交；校验全部成功后才写配置及显式来源选择。"""
    locked = db.scalar(select(HcsAccount).where(HcsAccount.id == tenant.id).with_for_update()
                       .execution_options(populate_existing=True))
    if locked is None:
        raise not_found("租户")
    before = get_config(locked)
    values = {**before, **body.model_dump(include=set(_PUBLIC_FIELDS), exclude_unset=True)}
    if body.access_key:
        values["access_key"] = body.access_key
    if values["auth_type"] == "PASSWORD" and (body.access_key or body.secret_key):
        raise BizError(ErrorCode.BAD_REQUEST, "请先选择 AK/SK 认证方式")
    if values["auth_type"] == "AKSK" and body.password:
        raise BizError(ErrorCode.BAD_REQUEST, "AK/SK 认证方式不能填写账号密码")
    if before["auth_type"] != values["auth_type"]:
        # 切换认证不沿用另一方式的凭据；事务校验失败时不写入。
        values["secret_cipher"] = values["sk_cipher"] = ""
        if values["auth_type"] == "AKSK":
            values["username"], values["organization"] = "", None
        else:
            values["access_key"] = ""
    if body.password and body.clear_password:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时填写和清除 ManageOne 密码")
    if body.clear_password and values["enabled"] and values["auth_type"] == "PASSWORD":
        raise BizError(ErrorCode.BAD_REQUEST, "清除 ManageOne 密码前须先禁用配置")
    changed_identity = [label for key, label in (("endpoint", "地址"), ("username", "用户名"), ("organization", "组织"))
                        if (values[key] or "") != (before[key] or "")]
    if (values["auth_type"] == "PASSWORD" and changed_identity and values["secret_cipher"]
            and not body.password and not body.clear_password):
        raise BizError(ErrorCode.BAD_REQUEST,
                       f"修改 ManageOne {'、'.join(changed_identity)}后须重新填写密码；或禁用配置并显式清除旧密码")
    if body.password:
        if set(body.password) == {"*"}:
            raise BizError(ErrorCode.BAD_REQUEST, "请填写实际 ManageOne 密码；密码留空保留原密码")
        values["secret_cipher"] = encrypt(body.password)
    elif body.clear_password:
        values["secret_cipher"] = ""
    if body.secret_key and body.clear_secret_key:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时填写和清除 ManageOne SK")
    if body.clear_secret_key and values["enabled"] and values["auth_type"] == "AKSK":
        raise BizError(ErrorCode.BAD_REQUEST, "清除 ManageOne SK 前须先禁用配置")
    changed_ak = any(values[key] != before[key] for key in ("endpoint", "access_key"))
    if (values["auth_type"] == "AKSK" and changed_ak and values["sk_cipher"]
            and not body.secret_key and not body.clear_secret_key):
        raise BizError(ErrorCode.BAD_REQUEST, "修改 ManageOne 地址或 AK 后须重新填写 SK；或禁用配置并显式清除旧 SK")
    if body.secret_key:
        if not body.secret_key.strip() or set(body.secret_key) == {"*"}:
            raise BizError(ErrorCode.BAD_REQUEST, "请填写实际 ManageOne SK；留空保留原 SK")
        values["sk_cipher"] = encrypt(body.secret_key)
    elif body.clear_secret_key:
        values["sk_cipher"] = ""
    complete = configured(values)
    credentials = "地址、AK 和 SK" if values["auth_type"] == "AKSK" else "地址、用户名和独立密码"
    if values["enabled"] and not complete:
        raise BizError(ErrorCode.BAD_REQUEST, f"启用 ManageOne 前须填写完整的{credentials}")
    if body.select_source and not (values["enabled"] and complete):
        raise BizError(ErrorCode.BAD_REQUEST, f"用于 ECS 指标采集前，须先启用并完整配置 ManageOne（{credentials}）")
    changes = {key: [before[key], values[key]] for key in _PUBLIC_FIELDS if before[key] != values[key]}
    if before["access_key"] != values["access_key"]:
        changes["access_key"] = [mask(before["access_key"]), mask(values["access_key"])]
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
    return {"changes": changes, "password_changed": secret_changed,
            "secret_key_changed": before["sk_cipher"] != values["sk_cipher"], "closed_disk_alarms": closed_alarms}
