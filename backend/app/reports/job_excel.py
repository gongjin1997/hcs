"""作业报表 Excel：汇总、作业明细、节点和指标。"""

import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import Scope
from app.core.response import BizError, ErrorCode
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.models.base import utcnow
from app.schemas.job_report import JobPanel, JobReportRow
from app.services.job_report_service import DATA_NOTES, JobReportFilters, job_collection_status, list_job_report_rows

PANEL_TITLES = {
    "dgc": "DGC 作业报表", "flink": "Flink 作业报表", "realtime": "DGC 实时作业报表",
    "batch": "DGC 批作业报表", "mrs": "MRS 作业报表",
}
TYPE_NAMES = {
    "dgc_realtime_job": "DGC 实时作业", "dgc_batch_job": "DGC 批作业",
    "mrs_job": "MRS 原生执行", "dgc_mrs_node": "DataArts MRS 节点",
}
HEADERS = [
    "作业名称", "作业类别", "作业状态", "原始状态", "执行状态", "引擎", "作业类型",
    "租户", "项目", "区域", "工作空间", "所属作业", "节点名称", "实例 ID", "节点类型",
    "计划时间", "提交时间", "开始时间", "结束时间", "运行时长（分钟）",
    "节点数", "异常节点数", "活动告警数", "巡检健康度", "最后观测时间", "状态变化观测时间",
    "资源 ID", "资源主键", "租户 ID", "项目 ID", "生命周期",
    "历史 P90（分钟）", "基线有效样本数", "基线最低样本数", "基线窗口开始", "基线窗口结束", "基线状态",
    "采集来源", "对象层级", "执行结果", "集群", "集群 ID", "队列", "执行 ID", "原始耗时", "耗时单位",
    "调度状态", "实例查询状态", "定义已采集", "引擎列表", "实例窗口开始", "实例窗口结束",
    "列表末次实例提示（非当前证据）", "列表末次实例结束提示", "时长依据", "时长观测时间",
]


def _value(value: Any) -> Any:
    if isinstance(value, dict | list):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**15:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sheet(wb: Workbook, title: str, headers: Sequence[str], rows: Iterable[Sequence[Any]],
           widths: Sequence[int] | None = None) -> None:
    ws = wb.create_sheet(title)

    def bounded_rows():
        for index, row in enumerate(rows, 1):
            if index > 1048575:
                raise BizError(ErrorCode.BAD_REQUEST, "导出数据超过 Excel 工作表行数上限，请缩小筛选范围")
            yield [_value(value) for value in row]

    write_table(ws, headers, bounded_rows(), widths)
    if ws.max_row == 2 and all(cell.value is None for cell in ws[2]):
        ws.delete_rows(2)
    for row in ws:
        for cell in row:
            if isinstance(cell.value, str):
                # 显式字符串类型，公式前缀和 Excel 错误代码均按原文保存。
                cell.data_type = "s"
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            cell.alignment = Alignment(vertical="center", wrap_text=True)


def _time(value: Any) -> Any:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return value


def _row(row: JobReportRow) -> list[Any]:
    details, metrics = row.status_details, row.metrics
    baseline = details.get("batch_baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    return [
        row.name, TYPE_NAMES[row.resource_type], row.status_label, row.status_raw, row.execution_state,
        row.engine, row.job_type, row.tenant_name, row.project_name or row.project_id,
        row.region, details.get("workspace"), details.get("job_name"), details.get("node_name"),
        str(details["instance_id"]) if details.get("instance_id") is not None else None, details.get("node_type"),
        *(_time(details.get(key)) if row.resource_type in ("dgc_batch_job", "mrs_job", "dgc_mrs_node") else details.get(key)
          for key in ("plan_time", "submitted_time", "start_time", "end_time")),
        metrics.get("run_minutes"), metrics.get("nodes"), metrics.get("bad_nodes"),
        row.active_alarms, row.health, row.updated_at, row.status_since, row.resource_id, row.id,
        row.tenant_id, row.project_id, row.lifecycle,
        metrics.get("p90_minutes"), baseline.get("sample_count"), baseline.get("min_samples"),
        _time(baseline.get("window_start_ms")), _time(baseline.get("window_end_ms")), baseline.get("status"),
        row.source, row.object_level, row.execution_result, details.get("cluster"), details.get("cluster_id"),
        details.get("queue_name", details.get("queue")),
        str(details["job_id"]) if details.get("job_id") is not None else None,
        details.get("elapsed_time"), details.get("elapsed_time_unit"), details.get("schedule_status"),
        details.get("instance_query_status"), details.get("definition_present"), row.engines,
        _time(details.get("window_start_ms")), _time(details.get("window_end_ms")),
        details.get("listed_last_instance_status"), _time(details.get("listed_last_instance_end_time")),
        details.get("duration_basis"), _time(details.get("duration_observed_at")),
    ]


def job_workbook(db: Session, scope: Scope, panel: JobPanel, filters: JobReportFilters) -> bytes:
    rows = list_job_report_rows(db, scope, panel, filters)
    wb = new_workbook()
    statuses = Counter(row.status_label for row in rows)
    collection = job_collection_status(db, scope, panel, filters, len(rows))
    criteria = {key: value for key, value in vars(filters).items() if value is not None and value != []}
    scope_name = "全部授权租户" if scope.tenant_ids is None else "、".join(map(str, scope.tenant_ids)) or "无授权租户"
    summary: list[list[Any]] = [
        ["报表", PANEL_TITLES[panel]], ["导出时间", utcnow()], ["时区", get_settings().timezone],
        ["租户范围", scope_name], ["筛选条件", criteria], ["作业数", len(rows)],
        ["活动告警数", sum(row.active_alarms for row in rows)], ["数据范围", "当前筛选的最新采集数据"],
        ["数据说明", "\n".join(DATA_NOTES)], ["采集情况", collection.message], ["采集状态", collection.state],
        *[[f"对象层级 {level}", count] for level, count in Counter(row.object_level for row in rows).items()],
        *[[f"采集来源 {source}", count] for source, count in Counter(row.source for row in rows).items()],
        *[[status, count] for status, count in sorted(statuses.items())],
    ]
    _sheet(wb, "汇总", ["项目", "内容"], summary, [24, 80])
    _sheet(wb, "作业明细", HEADERS, (_row(row) for row in rows),
           [30, 20, 20, 20, 20, 20, 14, 20, 24, 28, 16, 24, 24, 18, 24, 38,
            22, 22, 22, 22, 20, 20, 12, 14, 14, 16, 22, 22, 40, 14, 14, 38, 38, 14, 18, 18, 18]
           + [28, 18, 20, 24, 32, 24, 32, 18, 18, 18, 20, 18, 30, 22, 22, 30, 24, 28, 22])
    _sheet(wb, "节点状态", ["资源主键", "作业名称", "租户", "节点", "节点状态"],
           ([row.id, row.name, row.tenant_name, node.get("name"), node.get("status")]
            for row in rows for node in row.status_details.get("nodes") or [] if isinstance(node, dict)),
           [14, 30, 24, 40, 20])
    _sheet(wb, "运行指标", ["资源主键", "作业名称", "租户", "指标", "值"],
           ([row.id, row.name, row.tenant_name, key, value] for row in rows for key, value in row.metrics.items()),
           [14, 30, 24, 34, 40])
    return to_bytes(wb)
