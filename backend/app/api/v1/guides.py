"""静态指南只依赖登录，无租户数据、范围过滤或额外功能权限。"""

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.core.response import ApiResponse, not_found, ok
from app.schemas.guide import ResourceGuide
from app.services.resource_guides import get_resource_guide, list_resource_guides

router = APIRouter(prefix="/guides", tags=["资源与指标指南"], dependencies=[Depends(get_current_user)])


@router.get("", summary="资源与指标指南列表（只需登录）", response_model=ApiResponse[list[ResourceGuide]])
def guide_list():
    return ok(list_resource_guides())


@router.get("/{resource_type}", summary="资源类型指标说明与实践（只需登录）", response_model=ApiResponse[ResourceGuide])
def guide_detail(resource_type: str):
    guide = get_resource_guide(resource_type)
    if guide is None:
        raise not_found("资源类型指南")
    return ok(guide)
