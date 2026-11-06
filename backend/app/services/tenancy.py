"""租户（= HCS 账号 / VDC，hcs_account）与巡检目标。

一个租户可以有多个项目（region + project_id）；巡检、台账同步、容器事件采集都按“租户 × 项目”逐个执行，
每个租户单独一条运行记录，数据全部打上 tenant_id（资源还带 project_ref），页面按租户范围过滤。
数据库里一个租户都没有时，退回环境变量中的单账号配置（兼容早期版本，tenant_id 为空）。
"""
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.hcs import HcsClient, build_client, client_for
from app.models import HcsAccount, HcsProject


@dataclass
class Target:
    tenant_id: int | None
    code: str
    name: str
    short_name: str
    project_ref: int | None
    project_id: str
    project_name: str
    region: str
    workspaces: list[str] = field(default_factory=list)
    tenant: HcsAccount | None = None
    project: HcsProject | None = None

    def client(self, http: httpx.Client | None = None, mock_url: str | None = None) -> HcsClient:
        if self.tenant is None or self.project is None:
            return build_client(http=http)
        return client_for(self.tenant, self.project, http=http, mock_url=mock_url)

    @property
    def label(self) -> str:
        return self.short_name or self.name


def legacy_target() -> Target:
    s = get_settings()
    return Target(tenant_id=None, code=s.hcs_domain, name=s.hcs_domain, short_name="", project_ref=None,
                  project_id=s.hcs_project_id, project_name=s.hcs_project_id, region=s.hcs_region,
                  workspaces=list(s.hcs_workspaces))


def targets(db: Session, tenant_ids: list[int] | None = None) -> list[Target]:
    """启用的租户 × 启用的项目；tenant_ids 为空表示全部租户。"""
    q = select(HcsAccount, HcsProject).join(HcsProject, HcsProject.account_id == HcsAccount.id).where(
        HcsAccount.enabled.is_(True), HcsProject.enabled.is_(True))
    if tenant_ids is not None:
        q = q.where(HcsAccount.id.in_(tenant_ids))
    out = []
    for t, p in db.execute(q.order_by(HcsAccount.id, HcsProject.id)).all():
        out.append(Target(tenant_id=t.id, code=t.code, name=t.name, short_name=t.short_name or t.name,
                          project_ref=p.id, project_id=p.project_id, project_name=p.project_name or p.project_id,
                          region=p.region_id, workspaces=list(p.workspaces or []), tenant=t, project=p))
    return out


def resolve_targets(db: Session, tenant_ids: list[int] | None = None) -> list[Target]:
    """有租户配置时返回范围内的租户目标；一个租户都没配置时返回环境变量兜底目标。"""
    ts = targets(db, tenant_ids)
    if tenant_ids is not None or ts or db.scalar(select(HcsAccount.id).limit(1)) is not None:
        return ts
    return [legacy_target()]


def tenant_brief(t: HcsAccount) -> dict:
    return {"id": t.id, "code": t.code, "name": t.name, "short_name": t.short_name or t.name, "color": t.color,
            "auth_type": t.auth_type, "enabled": t.enabled}


def tenant_map(db: Session) -> dict[int, HcsAccount]:
    return {t.id: t for t in db.scalars(select(HcsAccount).order_by(HcsAccount.id))}


def tenant_label(db: Session, tenant_id: int | None) -> str:
    if tenant_id is None:
        return "全部租户"
    t = db.get(HcsAccount, tenant_id)
    return (t.short_name or t.name) if t else f"租户#{tenant_id}"
