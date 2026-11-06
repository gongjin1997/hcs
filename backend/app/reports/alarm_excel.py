"""按当前权限与筛选导出完整告警表。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizError, ErrorCode
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.models import AlarmEvent, HcsAccount
from app.services.catalog import RESOURCE_TYPES, SERVICES, STATUS_LABEL


def alarm_workbook(db: Session, alarms: list[AlarmEvent]) -> bytes:
    if len(alarms) > 1048575:
        raise BizError(ErrorCode.BAD_REQUEST, "告警数量超过 Excel 工作表行数上限，请缩小筛选范围")
    tenant_ids = {a.tenant_id for a in alarms if a.tenant_id is not None}
    names = dict(db.execute(select(HcsAccount.id, HcsAccount.name).where(HcsAccount.id.in_(tenant_ids))).all())
    wb = new_workbook()
    ws = wb.create_sheet("告警明细")
    write_table(ws, [
        "告警 ID", "租户", "级别", "状态", "告警标题", "规则", "云服务", "资源类型", "资源", "子对象",
        "当前值", "阈值", "首次发生", "最近发生", "恢复时间", "触发次数", "负责人", "认领人",
    ], ([
        a.id, names.get(a.tenant_id, "") if a.tenant_id is not None else "平台", a.severity,
        STATUS_LABEL.get(a.status, a.status), a.title, a.rule_id,
        SERVICES.get(a.service, a.service), RESOURCE_TYPES.get(a.resource_type, {}).get("name", a.resource_type),
        a.resource_name, a.sub_object, a.current_value, a.threshold_text, a.first_seen, a.last_seen, a.resolved_at,
        a.count, a.owner, a.ack_by,
    ] for a in alarms), [14, 24, 10, 12, 50, 22, 22, 20, 30, 26, 18, 26, 22, 22, 22, 12, 16, 16])
    return to_bytes(wb)
