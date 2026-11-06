"""短信默认关闭：全局总开关与租户独立网关配置。"""
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import BizError, ErrorCode, not_found, ok
from app.models import HcsAccount
from app.services import sms_settings


class SmsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                fields = [list(error["loc"]) for error in exc.errors()]
                raise BizError(ErrorCode.BAD_REQUEST, "短信配置参数不正确，请检查地址、账号、密码及级别", 400,
                               {"fields": fields}) from None

        return safe_handler


router = APIRouter(prefix="/system", tags=["短信通知"], route_class=SmsRoute)


class SmsGlobalIn(BaseModel):
    enabled: StrictBool = False


def _tenant(db: Session, scope: Scope, tenant_id: int) -> HcsAccount:
    scope.ensure(tenant_id, "租户")
    tenant = db.get(HcsAccount, tenant_id)
    if tenant is None:
        raise not_found("租户")
    return tenant


@router.get("/sms", summary="短信总开关（默认关闭）")
def get_global(db: Session = Depends(get_db), _: CurrentUser = Depends(require_perm(
        "setting:manage", "tenant:manage", "notify:view", any_of=True))):
    return ok(sms_settings.load_global(db).model_dump(mode="json"))


@router.put("/sms", summary="修改短信总开关（仅全租户系统管理员）")
def put_global(body: SmsGlobalIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
               audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("setting:manage"))):
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "短信总开关为全局设置，需要全部租户的数据权限", 403)
    diff = sms_settings.save_global(db, body.enabled, cu.display_name or cu.username)
    if diff:
        audit.log("sms.global.update", target_type="setting", target_id="sms", target_name="短信总开关",
                  detail={"changes": diff})
    return ok(sms_settings.load_global(db).model_dump(mode="json"))


@router.get("/sms/tenants", summary="各租户短信配置概况（系统配置页使用；不返回网关地址、账号与密码）")
def list_tenants(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 _: CurrentUser = Depends(require_perm("tenant:manage"))):
    rows = db.scalars(scope.apply(select(HcsAccount), HcsAccount.id).order_by(HcsAccount.id)).all()
    global_on = sms_settings.load_global(db).enabled
    items = []
    for tenant in rows:
        config = sms_settings.load_tenant(db, tenant.id)
        public = sms_settings.public_tenant(config)
        items.append({"tenant_id": tenant.id, "tenant_name": tenant.name, "short_name": tenant.short_name,
                      "tenant_enabled": tenant.enabled, "enabled": config.enabled,
                      "configured": bool(config.endpoint and config.username and public["password_set"]),
                      "severities": config.severities, "effective": sms_settings.effective(db, tenant.id)})
    return ok({"global_enabled": global_on, "items": items})


@router.get("/tenants/{tenant_id}/sms", summary="租户短信配置（密码不返回）")
def get_tenant(tenant_id: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
               _: CurrentUser = Depends(require_perm("tenant:manage"))):
    _tenant(db, scope, tenant_id)
    return ok(sms_settings.public_tenant(sms_settings.load_tenant(db, tenant_id)))


@router.put("/tenants/{tenant_id}/sms", summary="保存租户独立短信配置（默认关闭，不发送测试短信）")
def put_tenant(tenant_id: int, body: sms_settings.SmsTenantUpdate, db: Session = Depends(get_db),
               scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
               cu: CurrentUser = Depends(require_perm("tenant:manage"))):
    _tenant(db, scope, tenant_id)
    diff = sms_settings.save_tenant(db, tenant_id, body, cu.display_name or cu.username)
    if diff:
        audit.log("sms.tenant.update", target_type="tenant_sms", target_id=tenant_id, target_name="租户短信配置",
                  tenant_id=tenant_id, detail={"changes": diff})
    return ok(sms_settings.public_tenant(sms_settings.load_tenant(db, tenant_id)))


@router.get("/sms/notifications", summary="短信升级记录（按租户过滤，仅展示掩码号码）")
def list_notifications(tenant_id: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200),
                       db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                       _: CurrentUser = Depends(require_perm("notify:view", "tenant:manage", any_of=True))):
    from app.models.sms import SmsNotification

    query = scope.apply(select(SmsNotification), SmsNotification.tenant_id)
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        query = query.where(SmsNotification.tenant_id == tenant_id)
    rows = db.scalars(query.order_by(SmsNotification.id.desc()).limit(limit))
    fields = ("id", "tenant_id", "alarm_id", "user_id", "status", "phone_mask", "gateway_code",
              "gateway_message_id", "error", "created_at", "claimed_at", "sent_at")
    return ok([{key: getattr(row, key) for key in fields} for row in rows])
