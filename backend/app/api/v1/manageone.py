"""租户 ManageOne 独立配置 API；请求校验错误也不得回显密码。"""
from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.models import HcsAccount
from app.schemas.manageone import ENDPOINT_ERROR, ManageOneConfigOut, ManageOneConfigUpdate
from app.services import manageone_config

_FIELD_ERRORS = {
    "auth_type": "ManageOne 认证方式须为 PASSWORD 或 AKSK",
    "access_key": "ManageOne AK 须为不含空白、控制字符和脱敏占位符的字符串，最长 128 字符",
    "secret_key": "ManageOne SK 须为字符串，最长 512 字符；留空保留原 SK",
    "clear_secret_key": "ManageOne 清除 SK 选项须为布尔值",
    "endpoint": ENDPOINT_ERROR,
    "username": "ManageOne 用户名须为不含控制字符的字符串，最长 128 字符",
    "organization": "ManageOne 组织须为空或不含控制字符的字符串，最长 128 字符",
    "password": "ManageOne 密码须为字符串，最长 512 字符；留空保留原密码",
    "enabled": "ManageOne 启用状态须为布尔值",
    "clear_password": "ManageOne 清除密码选项须为布尔值",
    "select_source": "ManageOne 用于 ECS 指标采集选项须为布尔值",
}


class ManageOneRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                fields = list(dict.fromkeys(error["loc"][1] for error in exc.errors()
                                            if len(error["loc"]) > 1 and error["loc"][1] in _FIELD_ERRORS))
                message = _FIELD_ERRORS[fields[0]] if fields else "ManageOne 配置参数不正确，请检查字段类型和长度"
                raise BizError(ErrorCode.BAD_REQUEST, message, 400, {"fields": fields}) from None

        return safe_handler


router = APIRouter(prefix="/system/tenants", tags=["ManageOne 配置"], route_class=ManageOneRoute)


def _tenant(db: Session, scope: Scope, tenant_id: int) -> HcsAccount:
    scope.ensure(tenant_id, "租户")
    tenant = db.get(HcsAccount, tenant_id)
    if tenant is None:
        raise not_found("租户")
    return tenant


@router.get("/{tenant_id}/manageone", summary="查看租户 ManageOne 独立配置（密码不返回）",
            response_model=ApiResponse[ManageOneConfigOut])
def get_manageone(tenant_id: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  _: CurrentUser = Depends(require_perm("ledger:view", "task:view", "tenant:view", any_of=True))):
    return ok(manageone_config.public_config(_tenant(db, scope, tenant_id)))


@router.put("/{tenant_id}/manageone", summary="保存租户 ManageOne 配置（默认关闭，显式选择指标来源）",
            response_model=ApiResponse[ManageOneConfigOut])
def put_manageone(tenant_id: int, body: ManageOneConfigUpdate, db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
                  user: CurrentUser = Depends(require_perm("tenant:manage"))):
    tenant = _tenant(db, scope, tenant_id)
    detail = manageone_config.save_config(db, tenant, body, user.display_name or user.username)
    audit.log("tenant.manageone.update", target_type="tenant_manageone", target_id=tenant.id,
              target_name=tenant.name, tenant_id=tenant.id, detail=detail)
    return ok(manageone_config.public_config(tenant))
