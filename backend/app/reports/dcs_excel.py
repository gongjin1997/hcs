"""DCS 指标完整导出；流式工作簿避免大范围查询占用逐格内存。"""
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils import get_column_letter

from app.core.xlsx import HEADER_FILL, HEADER_FONT, cell_value, to_bytes
from app.schemas.dcs import DcsHistoryOut, DcsMetricsHistoryOut

SOURCE = {"AOM": "AOM（已停用，仅历史）", "CES": "旧版 CES", "LOCAL": "DCS 接口采样", "NONE": "暂无数据"}
STATUS = {"AVAILABLE": "完整", "PARTIAL": "部分时段缺失", "NO_DATA": "无数据"}


def _sheet(wb: Workbook, title: str, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    ws = wb.create_sheet(title)
    ws.freeze_panes = "A2"
    for i, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = 24 if "时间" in header else max(16, min(48, len(header) * 2 + 4))
    head = []
    for header in headers:
        cell = WriteOnlyCell(ws, header)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        head.append(cell)
    ws.append(head)
    count = 1
    for row in rows:
        cells = []
        for value in row:
            cell = WriteOnlyCell(ws, cell_value(value))
            if isinstance(cell.value, str):
                cell.data_type = "s"
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            elif isinstance(cell.value, float):
                cell.number_format = "0.00"
            cells.append(cell)
        ws.append(cells)
        count += 1
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{count}"


def export_memory(data: DcsHistoryOut) -> bytes:
    wb = Workbook(write_only=True)
    _sheet(wb, "查询条件", ["项目", "内容"], [
        ["起始时间（含）", data.start_at], ["结束时间（不含）", data.end_at],
        ["时间粒度（分钟）", data.interval_minutes], ["时区", data.timezone],
        ["实例数", len(data.series)], ["生成时间", data.generated_at],
        ["数据环境", "演示" if data.demo else "实际采集"],
        ["聚合口径", "平均值按有效原始采样点加权；峰值取原始最大值；缺失留空，非 0"],
        ["数据来源", "仅使用已保存的 DCS 原生实际采样；查询和导出不调用云监控"],
        ["本地记录", "从部署此功能后开始采集，按 90 天保留；未采集的历史为空"],
        ["内存单位", "使用率与峰值为百分数，已用内存与容量为 MB；缺失字段留空"],
    ])
    _sheet(wb, "实例汇总", ["租户", "项目", "区域", "实例名称", "实例 ID", "来源", "状态", "有效时段", "总时段",
                          "平均使用率（%）", "峰值使用率（%）", "最近使用率（%）", "最近本地记录时间", "说明"], (
        [s.instance.tenant_name, s.instance.project_name, s.instance.region, s.instance.name, s.instance.resource_id,
         SOURCE[s.source], STATUS[s.status], s.valid_points, s.total_points, s.summary.average_usage,
         s.summary.peak_usage, s.summary.latest_usage, s.summary.latest_at, s.warning] for s in data.series))
    _sheet(wb, "内存指标", ["租户", "项目", "区域", "实例名称", "实例 ID", "时段起始时间", "时段结束时间（不含）",
                          "平均使用率（%）", "峰值使用率（%）", "平均已用内存（MB）", "末次容量（MB）", "有效采样数", "来源"], (
        [s.instance.tenant_name, s.instance.project_name, s.instance.region, s.instance.name, s.instance.resource_id,
         p.at, min(p.at + timedelta(minutes=data.interval_minutes), data.end_at),
         p.memory_usage, p.memory_max, p.used_memory_mb, p.max_memory_mb, p.sample_count, SOURCE[s.source]]
        for s in data.series for p in s.points))
    return to_bytes(wb)


def export_metrics(data: DcsMetricsHistoryOut) -> bytes:
    wb = Workbook(write_only=True)
    _sheet(wb, "查询条件", ["项目", "内容"], [
        ["起始时间（含）", data.start_at], ["结束时间（不含）", data.end_at],
        ["时间粒度（分钟）", data.interval_minutes], ["时区", data.timezone], ["生成时间", data.generated_at],
        ["数据环境", "演示" if data.demo else "实际采集"], ["API版本", "DCS V2"],
        ["数据来源", "DCS 原生接口真实本地采样；不查询 AOM/CES，不从资源快照回填历史"],
        ["时间口径", "源端未提供采样时间；所列时间为本地接收响应的时刻（UTC存储），新鲜度按获取时间计算"],
        ["聚合口径", "内存使用率、已用内存和网络速率取均值；"
         "CPU累计秒、keys、命令计数、容量取末值"],
        ["缺失处理", "缺失留空；0为有效值；计数重置保留原值，不推算CPU使用率或QPS"],
    ])
    _sheet(wb, "指标口径", ["字段", "指标", "单位", "窗口聚合"], (
        [metric.key, metric.label, metric.unit, "均值" if metric.aggregation == "mean" else "末值"] for metric in data.metrics))
    identity = ["租户", "项目", "实例名称", "实例 ID"]
    current_headers = ["来源", "API版本", "来源接口", "新鲜度", "本地获取时间", "样本年龄（秒）", "过期阈值（秒）", "说明"]
    _sheet(wb, "当前统计", identity + current_headers + [f"{metric.label}（{metric.unit}）" for metric in data.metrics], (
        [series.instance.tenant_name, series.instance.project_name, series.instance.name, series.instance.resource_id,
         series.instance.metrics_source, series.instance.api_version, series.instance.source_endpoint,
         series.instance.freshness, series.instance.metrics_observed_at, series.instance.sample_age_sec,
         series.instance.stale_after_sec, series.instance.warning,
         *(getattr(series.instance, metric.key) for metric in data.metrics)] for series in data.series))
    headers = identity + ["时段起始时间", "时段结束时间（不含）", "末次本地获取时间", "采样数", "来源", "API版本"]
    headers += [f"{metric.label}（{metric.unit}，{'均值' if metric.aggregation == 'mean' else '末值'}）"
                for metric in data.metrics]
    headers += [f"{metric.label}有效采样数" for metric in data.metrics]
    _sheet(wb, "原生指标", headers, (
        [series.instance.tenant_name, series.instance.project_name, series.instance.name, series.instance.resource_id,
         point.at, min(point.at + timedelta(minutes=data.interval_minutes), data.end_at), point.last_observed_at,
         point.sample_count, SOURCE[series.source], series.api_version,
         *(getattr(point, metric.key) for metric in data.metrics),
         *(point.metric_sample_counts.get(metric.key, 0) for metric in data.metrics)]
        for series in data.series for point in series.points))
    return to_bytes(wb)
