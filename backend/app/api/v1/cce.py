from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, not_found, ok
from app.core.xlsx import new_workbook, to_bytes, write_table, xlsx_response
from app.hcs.cce_inventory import timestamp
from app.models import HcsAccount
from app.schemas.cce import CceClusterOut, CceClustersOut, CceSettings, CceSettingsOut
from app.services import cce_monitoring, cce_settings

router = APIRouter(prefix="/cce", tags=["CCE 集群健康"])
VIEW = Depends(require_perm("podevent:view"))


@router.get("/clusters", response_model=ApiResponse[CceClustersOut], summary="CCE 集群健康快照（不调用云端）")
def clusters(tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
             cluster_id: str | None = Query(None, max_length=255), page: int = Query(1, ge=1),
             page_size: int = Query(50, ge=1, le=100), db: Session = Depends(get_db),
             scope: Scope = Depends(get_scope), _: CurrentUser = VIEW):
    return ok(cce_monitoring.list_clusters(db, scope, tenant_id=tenant_id, project_ref=project_ref,
                                         cluster_id=cluster_id, page=page, page_size=page_size))


@router.get("/clusters/{key}", response_model=ApiResponse[CceClusterOut], summary="CCE 节点、节点池及原生能力快照")
def detail(key: str = Path(pattern=r"^[a-f0-9]{64}$"), db: Session = Depends(get_db),
           scope: Scope = Depends(get_scope), _: CurrentUser = VIEW):
    return ok(cce_monitoring.cluster_detail(db, scope, key))


@router.get("/clusters/{key}/export", summary="导出 CCE 集群只读快照（Excel）")
def export(key: str = Path(pattern=r"^[a-f0-9]{64}$"), db: Session = Depends(get_db),
           scope: Scope = Depends(get_scope), _: CurrentUser = VIEW):
    data = cce_monitoring.cluster_detail(db, scope, key)
    wb = new_workbook()
    write_table(wb.create_sheet("采集信息"), ["租户", "项目", "集群", "采集时间", "时效", "数据源"],
                [[data["tenant_name"], data["project_name"], data["cluster"]["name"], data["last_seen"],
                  data["freshness"], data["sources"]]])
    write_table(wb.create_sheet("节点"), ["节点", "状态", "检查时间", "时效", "健康", "云服务器ID", "私网IP"],
                ([n.get(k) for k in ("name", "phase", "last_probe_at", "freshness", "health", "server_id", "private_ip")]
                 for n in data["nodes"]))
    write_table(wb.create_sheet("节点池"), ["节点池", "当前节点", "创建中", "删除中", "条件"],
                ([n.get(k) for k in ("name", "current_nodes", "creating_nodes", "deleting_nodes", "conditions")]
                 for n in data["nodepools"]))
    for kind, title in (("nodes", "原生节点指标"), ("pods", "原生Pod指标")):
        samples = data["native"].get("metrics", {}).get(kind, {}).get("items", [])
        write_table(wb.create_sheet(title), ["名称", "命名空间", "CPU核数", "内存字节", "上游时间", "窗口秒", "有效样本"],
                    [[(n.get("identity") or {}).get("name"), (n.get("identity") or {}).get("namespace"),
                      n.get("cpu_cores") if n.get("fresh") else None, n.get("memory_bytes") if n.get("fresh") else None,
                      timestamp(n.get("timestamp")), n.get("window_seconds"), n.get("fresh", False)] for n in samples])
    owners = data["native"].get("owners", {}).get("items", [])
    write_table(wb.create_sheet("工作负载归属"), ["Pod", "命名空间", "直接对象", "直接UID", "顶层对象", "顶层UID", "核对状态"],
                [[(n.get("pod") or {}).get("name"), (n.get("pod") or {}).get("namespace"),
                  (n.get("direct_owner") or {}).get("name"), (n.get("direct_owner") or {}).get("uid"),
                  (n.get("top_owner") or {}).get("name"), (n.get("top_owner") or {}).get("uid"), n.get("status")]
                 for n in owners])
    return xlsx_response(to_bytes(wb), "CCE集群快照.xlsx")


def _tenant(db: Session, scope: Scope, tenant_id: int) -> None:
    scope.ensure(tenant_id, "CCE 租户配置")
    if db.get(HcsAccount, tenant_id) is None:
        raise not_found("租户")


@router.get("/settings/{tenant_id}", response_model=ApiResponse[CceSettingsOut], summary="查看租户 CCE 独立配置")
def settings(tenant_id: int = Path(ge=1), db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             _: CurrentUser = VIEW):
    _tenant(db, scope, tenant_id)
    return ok(cce_settings.status(db, tenant_id))


@router.put("/settings/{tenant_id}", response_model=ApiResponse[CceSettingsOut],
            summary="保存租户 CCE 配置（不修改任务或总开关）")
def save_settings(body: CceSettings, tenant_id: int = Path(ge=1), db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
                  user: CurrentUser = Depends(require_perm("collect:manage"))):
    _tenant(db, scope, tenant_id)
    before = cce_settings.effective(db, tenant_id).model_dump()
    cce_settings.save(db, tenant_id, body, user.username)
    audit.log("cce.settings.update", target_type="cce_settings", target_id=tenant_id, tenant_id=tenant_id,
              detail={"before": before, "after": body.model_dump()})
    return ok(cce_settings.status(db, tenant_id))
