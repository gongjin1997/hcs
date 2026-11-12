"""台账管理字段 Excel 批量导入：负责人 / 业务系统 / 环境 / 核心资源 / 允许关机 / 备注。

流程：下载模板（按当前筛选导出在用资源，已带当前取值）→ 在 Excel 中修改 → 上传预览（dry_run，逐行校验并列出变更）→ 确认导入。
- 按“资源 ID”匹配（同一 ID 出现在多个资源类型时再按“资源类型”区分），只能修改当前数据范围内的在用资源
- 单元格留空 = 不修改；填写“-”= 清空；同步不会覆盖这些管理字段
- 负责人必须来自启用且具有资源租户范围的用户；负责人账号列优先，同名人员用账号区分
- 开启“台账字典 → 只能从字典选择”时，业务系统必须是字典中的取值
- 每个变更写一条台账变更记录（来源 = Excel 导入），整批写一条操作审计
"""
import io
from dataclasses import dataclass, field

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizError, ErrorCode
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.models import HcsAccount, Resource, User
from app.models.base import utcnow
from app.services import collect_scope
from app.services.catalog import ENV_TYPES, LEDGER_TYPES, RESOURCE_TYPES, type_name
from app.services.identity import resolve_owner
from app.services.settings_service import LedgerDict

from .sync import MANUAL_FIELDS, _change, propagate_owner

HEADERS = ["租户", "资源类型", "资源 ID", "名称", "负责人", "业务系统", "环境", "核心资源", "允许关机", "备注", "台账 ID",
           "负责人账号"]
WIDTHS = [12, 16, 40, 28, 12, 18, 8, 10, 10, 36, 12, 20]
EDITABLE = {"负责人": "owner", "业务系统": "business", "环境": "env_type", "核心资源": "critical", "允许关机": "allow_stop",
            "备注": "remark", "负责人账号": "owner_user_id"}
CLEAR = {"-", "－", "（清空）", "(清空)"}
YES, NO = {"是", "y", "yes", "true", "1", "√", "核心"}, {"否", "n", "no", "false", "0", "×"}
ENV_BY_TEXT = {**{v: k for k, v in ENV_TYPES.items()}, **{k: k for k in ENV_TYPES}}
MAX_ROWS = 5000
LABEL = {**MANUAL_FIELDS, "allow_stop": "预期状态"}


@dataclass
class ImportResult:
    total: int = 0
    matched: int = 0
    unchanged: int = 0
    items: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self, limit: int = 500) -> dict:
        return {"total": self.total, "matched": self.matched, "changed": len(self.items), "unchanged": self.unchanged,
                "error_count": len(self.errors), "errors": self.errors[:limit], "items": self.items[:limit]}


def template(db: Session, types: list[str] | None, tenant_ids: list[int] | None) -> bytes:
    types = [t for t in (types or LEDGER_TYPES) if RESOURCE_TYPES[t]["ledger"]]
    q = select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.resource_type.in_(types))
    if tenant_ids is not None:
        q = q.where(Resource.tenant_id.in_(tenant_ids))
    tenants = {t.id: t.short_name or t.name for t in db.scalars(select(HcsAccount))}
    usernames = dict(db.execute(select(User.id, User.username).where(
        User.id.in_(q.with_only_columns(Resource.owner_user_id)))).all())
    rows = []
    for r in db.scalars(q.order_by(Resource.tenant_id, Resource.resource_type, Resource.name)):
        rows.append([tenants.get(r.tenant_id or 0, "-"), type_name(r.resource_type), r.resource_id, r.name, r.owner,
                     r.business, ENV_TYPES.get(r.env_type or "", r.env_type), "是" if r.critical else "否",
                     "否" if (r.expected_state or {}).get("running", True) else "是", r.remark, r.id,
                     usernames.get(r.owner_user_id) if r.owner_user_id is not None else None])
    wb = new_workbook()
    ws = wb.create_sheet("台账管理字段")
    write_table(ws, HEADERS, rows, WIDTHS)
    help_ws = wb.create_sheet("填写说明")
    lines = [["说明"], ["1. 保留平台导出的台账 ID；同云资源 ID 跨项目重复时按它准确匹配，仍受当前租户权限限制"],
             ["2. 可修改：负责人、业务系统、环境、核心资源、允许关机、备注、负责人账号"],
             ["3. 单元格留空 = 不修改；填写 - = 清空该字段（任一负责人列填 - 会同时清空负责人及账号）"],
             [f"4. 环境取值：{' / '.join(ENV_TYPES.values())}（或 {' / '.join(ENV_TYPES)}）"],
             ["5. 核心资源、允许关机：是 / 否（允许关机 = 是 时，关机不再告警）"],
             ["6. 负责人必须在用户管理登记并启用，且有资源租户权限；可填账号或唯一显示名，同名时填写负责人账号"],
             ["7. 负责人账号优先于负责人显示名；开启台账字典“只能从字典选择”时，业务系统必须是字典中的取值"],
             [f"8. 单次最多 {MAX_ROWS} 行；上传后先预览变更，确认后才会写入"]]
    for i, line in enumerate(lines, 1):
        help_ws.cell(row=i, column=1, value=line[0])
    help_ws.column_dimensions["A"].width = 90
    return to_bytes(wb)


def _text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _bool(v: str, label: str) -> bool:
    low = v.lower()
    if low in YES:
        return True
    if low in NO:
        return False
    raise ValueError(f"{label}只能填“是”或“否”：{v}")


def _owner_changes(db: Session, resource: Resource, name: str | None, account: str | None) -> dict:
    values: dict[str, str | int | None]
    if name in CLEAR or account in CLEAR:
        values = {"owner": None, "owner_user_id": None}
    elif account is not None:
        current = db.get(User, resource.owner_user_id) if resource.owner_user_id is not None else None
        if current is not None and current.username == account and name in (None, resource.owner, account):
            return {}
        user = resolve_owner(db, tenant_id=resource.tenant_id, owner=account)
        if user is None or user.username != account:
            raise ValueError(f"负责人账号“{account}”不存在，请到用户管理确认账号")
        values = {"owner": user.display_name, "owner_user_id": user.id}
    elif name is None or name == resource.owner:
        # 导出回传的未匹配历史值不应妨碍修改其他字段，也不会据此新建人员。
        return {}
    else:
        user = resolve_owner(db, tenant_id=resource.tenant_id, owner=name)
        values = {"owner": user.display_name if user else None, "owner_user_id": user.id if user else None}
    return {key: value for key, value in values.items() if getattr(resource, key) != value}


def parse(db: Session, content: bytes, tenant_ids: list[int] | None, dictionary: LedgerDict) -> tuple[ImportResult, list]:  # noqa: C901  # T10
    """解析并校验，返回（结果, 待写入的变更 [(resource, {field: new})]）。不修改数据库。"""
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise BizError(ErrorCode.BAD_REQUEST, "无法读取文件，请上传 .xlsx 格式的 Excel（可先下载模板）") from e
    ws = wb["台账管理字段"] if "台账管理字段" in wb.sheetnames else wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [(_text(h) or "").replace("资源ID", "资源 ID") for h in next(rows, [])]
    if "资源 ID" not in header:
        raise BizError(ErrorCode.BAD_REQUEST, "第 1 行缺少“资源 ID”列，请使用平台导出的模板")
    col = {h: i for i, h in enumerate(header) if h}
    editable = [h for h in EDITABLE if h in col]
    if not editable:
        raise BizError(ErrorCode.BAD_REQUEST, f"没有可导入的列（{'、'.join(EDITABLE)}）")
    q = select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.resource_type.in_(LEDGER_TYPES))
    if tenant_ids is not None:
        q = q.where(Resource.tenant_id.in_(tenant_ids))
    by_id: dict[str, list[Resource]] = {}
    for r in db.scalars(q):
        by_id.setdefault(r.resource_id, []).append(r)
    tenant_rows = list(db.scalars(select(HcsAccount)))
    tenants = {t.id: t.short_name or t.name for t in tenant_rows}
    tenant_labels = {t.id: {t.name, t.short_name, t.code, str(t.id)} for t in tenant_rows}
    business_names = {i.name for i in dictionary.business}
    res, todo, seen = ImportResult(), [], set()
    for n, row in enumerate(rows, 2):
        cells = list(row) + [None] * (len(header) - len(row))
        rid = _text(cells[col["资源 ID"]])
        if rid is None and not any(_text(c) for c in cells):
            continue
        res.total += 1
        if res.total > MAX_ROWS:
            raise BizError(ErrorCode.BAD_REQUEST, f"单次最多导入 {MAX_ROWS} 行，请拆分文件")
        if rid is None:
            res.errors.append({"row": n, "resource_id": None, "message": "资源 ID 为空"})
            continue
        cands = by_id.get(rid, [])
        internal_id = _text(cells[col["台账 ID"]]) if "台账 ID" in col else None
        if internal_id:
            if not internal_id.isdecimal() or int(internal_id) <= 0:
                res.errors.append({"row": n, "resource_id": rid, "message": "台账 ID 必须为平台导出的正整数"})
                continue
            cands = [r for r in cands if r.id == int(internal_id)]
        tenant_text = _text(cells[col["租户"]]) if "租户" in col else None
        if tenant_text == "-":
            cands = [r for r in cands if r.tenant_id is None]
        elif tenant_text:
            cands = [r for r in cands if tenant_text in
                     tenant_labels.get(r.tenant_id if r.tenant_id is not None else -1, set())]
        type_text = _text(cells[col["资源类型"]]) if "资源类型" in col else None
        if type_text:
            cands = [r for r in cands if type_text in (type_name(r.resource_type), r.resource_type)]
        if not cands:
            res.errors.append({"row": n, "resource_id": rid, "message": "资源不存在、已释放或不在当前数据范围内"})
            continue
        if len(cands) > 1:
            res.errors.append({"row": n, "resource_id": rid,
                               "message": "资源 ID 在多个租户 / 项目下重复，请重新下载模板并保留台账 ID 列"})
            continue
        r = cands[0]
        if r.id in seen:
            res.errors.append({"row": n, "resource_id": rid, "message": "资源 ID 在文件中重复出现"})
            continue
        seen.add(r.id)
        res.matched += 1
        new: dict = {}
        try:
            new.update(_owner_changes(db, r, _text(cells[col["负责人"]]) if "负责人" in col else None,
                                      _text(cells[col["负责人账号"]]) if "负责人账号" in col else None))
            for h in editable:
                raw = _text(cells[col[h]])
                if raw is None:
                    continue
                key = EDITABLE[h]
                if key in ("owner", "owner_user_id"):
                    continue
                txt = None if raw in CLEAR else raw
                value: str | bool | None = txt
                if key == "env_type" and txt is not None:
                    if txt.upper() not in ENV_BY_TEXT and txt not in ENV_BY_TEXT:
                        raise ValueError(f"环境只能填 {'/'.join(ENV_TYPES.values())}：{txt}")
                    value = ENV_BY_TEXT.get(txt) or ENV_BY_TEXT[txt.upper()]
                elif key in ("critical", "allow_stop"):
                    value = False if txt is None else _bool(txt, h)
                elif key == "business" and txt is not None:
                    if len(txt) > 64:
                        raise ValueError(f"{h}不能超过 64 个字符")
                    if dictionary.enforce and txt not in business_names:
                        raise ValueError(f"{h}“{txt}”不在台账字典中（已开启只能从字典选择）")
                elif key == "remark" and txt is not None and len(txt) > 500:
                    raise ValueError("备注不能超过 500 个字符")
                old = (not (r.expected_state or {}).get("running", True)) if key == "allow_stop" else getattr(r, key)
                if old != value:
                    new[key] = value
        except (ValueError, BizError) as e:
            res.errors.append({"row": n, "resource_id": rid, "name": r.name, "message": str(e)})
            continue
        if not new:
            res.unchanged += 1
            continue
        changes = []
        for k, v in new.items():
            old = (not (r.expected_state or {}).get("running", True)) if k == "allow_stop" else getattr(r, k)
            changes.append({"field": k, "label": LABEL[k], "old": _show(k, old), "new": _show(k, v)})
        res.items.append({"row": n, "id": r.id, "resource_id": rid, "name": r.name, "type_name": type_name(r.resource_type),
                          "tenant": tenants.get(r.tenant_id or 0, "-"), "changes": changes})
        todo.append((r, new))
    return res, todo


def _show(key: str, v):
    if key in ("critical", "allow_stop"):
        return "是" if v else "否"
    if key == "env_type":
        return ENV_TYPES.get(v or "", v)
    return v


def apply(db: Session, todo: list, operator: str) -> int:
    now = utcnow()
    scopes = collect_scope.load(db)
    for r, new in todo:
        changes = []
        for k, v in new.items():
            if k == "allow_stop":
                old_run = (r.expected_state or {}).get("running", True)
                r.expected_state = {**(r.expected_state or {}), "running": not v}
                text = {True: "应保持运行", False: "允许关机"}
                changes.append({"field": "expected_state.running", "label": "预期状态", "old": text[old_run],
                                "new": text[not v]})
            else:
                changes.append({"field": k, "label": MANUAL_FIELDS[k], "old": getattr(r, k), "new": v})
                setattr(r, k, v)
        if "owner" in new or "owner_user_id" in new:
            propagate_owner(db, r)
        _change(db, r, "UPDATED", now, changes, source="IMPORT", operator=operator)
        if {"owner", "owner_user_id", "business", "env_type", "critical"} & new.keys():
            collect_scope.refresh_resource(db, r, operator=operator, scopes=scopes)   # 采集范围可能按这些字段匹配
    db.flush()
    return len(todo)
