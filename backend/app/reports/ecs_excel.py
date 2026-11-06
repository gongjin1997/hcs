"""ECS 趋势导出：与图表共用查询，缺失留空，来源和采样数量可核对。"""
from datetime import timedelta

from app.core.config import get_settings
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.schemas.ecs import EcsHistoryOut


def _source(source: str | None) -> str | None:
    return "AOM（已停用，仅历史）" if source == "AOM" else source


def _time_basis(value: str) -> str:
    return {"evaluation": "PromQL 求值时间", "source_sample": "上游点或配置的源时间"}.get(value, "时间语义未记录")


def export_metrics(data: EcsHistoryOut) -> bytes:
    wb = new_workbook()
    instance = data.instance
    write_table(wb.create_sheet("查询条件"), ["项目", "内容"], [
        ["租户", instance.tenant_name], ["项目", instance.project_name], ["实例名称", instance.name],
        ["实例 ID", instance.resource_id], ["IP", instance.ip], ["起始时间（含）", data.start_at],
        ["结束时间（不含）", data.end_at], ["时间粒度（秒）", data.interval_sec],
        ["导出时间时区", get_settings().timezone], ["样本保留天数", data.retention_days],
        ["统计口径", "均值按实际结果点计算；无结果留空；不同来源和时间语义分开，不以求值时间证明物理采样新鲜度"],
        ["说明", "；".join(data.warnings)], ["生成时间", data.generated_at],
    ])
    write_table(wb.create_sheet("指标趋势"), ["实例名称", "实例 ID", "指标", "来源", "单位", "时段开始", "时段结束（不含）",
                                           "平均值", "最大值", "最小值", "实际结果数", "时间语义"], (
        [instance.name, instance.resource_id, series.label, _source(series.source), series.unit, point.at,
         min(point.at+timedelta(seconds=data.interval_sec), data.end_at),
         point.average, point.maximum, point.minimum, point.sample_count, _time_basis(series.timestamp_semantics)]
        for series in data.series for point in series.points))
    write_table(wb.create_sheet("最近采样"), ["指标", "最近值（%）", "结果时间", "本地采集时间", "来源", "状态", "时间语义"], (
        [key, value.value, value.observed_at, value.collected_at, _source(value.source), value.state,
         _time_basis(value.timestamp_semantics)]
        for key, value in instance.latest.items()))
    return to_bytes(wb)
