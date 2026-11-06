"""资源台账导出：汇总页 + 每类资源一页（通用列 + 该类型特有列）。"""
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.xlsx import TITLE_FONT, new_workbook, safe_title, to_bytes, write_table
from app.models import HcsAccount, Resource
from app.models.base import utcnow
from app.schemas.resource import ClassificationConfig
from app.services import resource_classification
from app.services.catalog import CATEGORIES, ENV_TYPES, RESOURCE_TYPES
from app.services.ecs_monitoring import snapshots as ecs_snapshots
from app.services.metric_visibility import current_metrics

COMMON = [("租户", 12), ("名称", 26), ("资源 ID", 38), ("状态", 12), ("可用区", 12), ("IP 地址", 16), ("规格", 26)]
TAIL = [("负责人", 10), ("业务系统", 14), ("环境", 8), ("核心资源", 10), ("云上创建时间", 18), ("最近同步", 18), ("备注", 24),
        ("资源分类", 12)]


def _attr(r: Resource, key: str, metrics: dict | None = None):
    if key.startswith("attrs."):
        return (r.attrs or {}).get(key[6:])
    if key.startswith("metrics."):
        return (metrics if metrics is not None else (r.metrics or {})).get(key[8:])
    return getattr(r, key, None)


def ledger_rows(r: Resource, rtype: str, tenants: dict[int, str] | None = None,
                config: ClassificationConfig | None = None, metrics: dict | None = None) -> list:
    extra = [_attr(r, c["key"], metrics) for c in RESOURCE_TYPES[rtype]["columns"]]
    return [(tenants or {}).get(r.tenant_id or 0, "-"), r.name, r.resource_id, r.status_raw, r.az, r.ip, r.spec, *extra,
            r.owner, r.business, ENV_TYPES.get(r.env_type or "", r.env_type), r.critical, r.created_time, r.last_seen,
            r.remark, resource_classification.classify(r, config or resource_classification.defaults())[0]]


def add_ledger_sheets(wb, db: Session, types: list[str], include_summary: bool = True,
                      tenant_ids: list[int] | None = None, classification: str | None = None) -> None:
    config = resource_classification.load(db)
    q = select(Resource).where(Resource.resource_type.in_(types), Resource.lifecycle == "ACTIVE")
    if tenant_ids is not None:
        q = q.where(Resource.tenant_id.in_(tenant_ids))
    if classification is not None:
        q = q.where(resource_classification.expression(db, config, tenant_ids=tenant_ids,
                                                       resource_types=types) == classification)
    rows = db.scalars(q.order_by(Resource.tenant_id, Resource.resource_type, Resource.name)).all()
    snapshots = ecs_snapshots(db, rows)
    visible = {row.id: current_metrics(row, snapshots.get(row.id)) for row in rows}
    tenants = {t.id: t.short_name or t.name for t in db.scalars(select(HcsAccount))}
    by_type: dict[str, list[Resource]] = {}
    for r in rows:
        by_type.setdefault(r.resource_type, []).append(r)
    if include_summary:
        ws = wb.create_sheet("汇总")
        ws["A1"], ws["A1"].font = "资源台账汇总", TITLE_FONT
        ws["A2"] = f"导出时间：{utcnow().astimezone().strftime('%Y-%m-%d %H:%M')}，共 {len(rows)} 个资源"
        if classification is not None:
            ws["A2"] = f"{ws['A2'].value}，资源分类：{classification}"
        cat_name = {c["code"]: c["name"] for c in CATEGORIES}
        summary = []
        for t in types:
            items = by_type.get(t, [])
            owners = Counter(r.owner for r in items if r.owner)
            summary.append([cat_name[RESOURCE_TYPES[t]["category"]], RESOURCE_TYPES[t]["name"], len(items),
                            sum(1 for r in items if not r.owner), sum(1 for r in items if r.critical),
                            "、".join(o for o, _ in owners.most_common(3))])
        write_table(ws, ["分类", "资源类型", "数量", "未分配负责人", "核心资源", "主要负责人"], summary,
                    [14, 18, 8, 14, 10, 30], start_row=4)
    for t in types:
        cols = RESOURCE_TYPES[t]["columns"]
        headers = [h for h, _ in COMMON] + [c["label"] for c in cols] + [h for h, _ in TAIL]
        widths = [w for _, w in COMMON] + [max(10, c["width"] // 8) for c in cols] + [w for _, w in TAIL]
        ws = wb.create_sheet(safe_title(RESOURCE_TYPES[t]["name"]))
        write_table(ws, headers, [ledger_rows(r, t, tenants, config, visible.get(r.id))
                                 for r in by_type.get(t, [])], widths)


def export_ledger(db: Session, types: list[str], tenant_ids: list[int] | None = None,
                  classification: str | None = None) -> bytes:
    wb = new_workbook()
    add_ledger_sheets(wb, db, types, tenant_ids=tenant_ids, classification=classification)
    return to_bytes(wb)
