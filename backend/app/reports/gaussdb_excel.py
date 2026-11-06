"""GaussDB 原生查询 XLSX；外部文本不作为公式，数值与原生单位分开。"""
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.metrics.dcs_samples import finite_number
from app.schemas.gaussdb import GaussDiskHistoryOut, GaussInstance, GaussNativeRows, GaussOverviewOut
from app.services.gaussdb_reports import normalize_metric_series

SQL_COLUMNS = [("SQL ID", "sql_id"), ("用户", "user_name"), ("节点 ID", "node_id"), ("节点", "node_name"),
               ("SQL 模板", "sql_text"), ("执行计划", "query_plan"), ("调用次数", "calls"),
               ("平均耗时（微秒）", "avg_exec_time"), ("CPU 耗时（微秒）", "avg_cpu_time"),
               ("IO 耗时（微秒）", "avg_io_time"), ("平均返回行", "avg_returned_rows"),
               ("平均扫描行", "avg_fetched_rows"), ("Buffer 命中率（原值）", "buffer_hit_ratio"),
               ("SQL 命中率（原值）", "sql_hit_ratio")]
SESSION_COLUMNS = [("会话 ID", "session_id"), ("线程 ID", "pid"), ("SQL ID", "unique_sql_id"),
                   ("数据库", "database_name"), ("用户", "user_name"), ("客户端 IP", "client_ip"),
                   ("状态", "state"), ("等待", "wait"), ("等待事件", "wait_event"), ("阻塞会话", "block_session"),
                   ("语句运行时长（原值）", "query_runtime"), ("会话建立时长（秒）", "exec_time"), ("SQL 文本", "query"),
                   ("应用", "application_name")]
SQL_DETAIL_COLUMNS = [("SQL ID", "sql_id"), ("用户", "user_name"), ("节点 ID", "node_id"), ("节点", "node_name"),
                      ("SQL 文本", "sql_text"), ("执行计划", "query_plan"), ("开始时间（原生 UTC）", "start_time"),
                      ("结束时间（原生 UTC）", "finish_time"), ("总耗时（微秒）", "total_time"),
                      ("CPU 耗时（微秒）", "cpu_time"), ("计划耗时（微秒）", "plan_time"), ("IO 耗时（微秒）", "io_time"),
                      ("锁耗时（微秒）", "lock_time"), ("返回行", "returned_rows"), ("扫描行", "fetched_rows"),
                      ("扫描页", "fetched_pages"), ("命中页", "hit_pages"), ("锁次数", "lock_count"),
                      ("客户端 IP", "client_ip"), ("客户端端口", "client_port")]
INSPECTION_COLUMNS = [("记录 ID", "record_id"), ("实例 ID", "instance_id"), ("任务", "task_name"),
                      ("状态", "status"), ("开始时间", "start_time"), ("结束时间", "end_time"),
                      ("巡检项总数", "total_count"), ("正常", "normal_count"), ("异常", "abnormal_count"),
                      ("失败", "failed_count")]
NUMERIC = {"calls", "avg_exec_time", "avg_cpu_time", "avg_io_time", "avg_returned_rows", "avg_fetched_rows",
           "buffer_hit_ratio", "sql_hit_ratio", "total_count", "normal_count", "abnormal_count", "failed_count", "exec_time",
           "total_time", "cpu_time", "plan_time", "io_time", "lock_time", "returned_rows", "fetched_rows", "fetched_pages",
           "hit_pages", "lock_count"}


def _at(value: object) -> datetime | None:
    number = finite_number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, UTC)
    except (OverflowError, ValueError, OSError):
        return None


def report_points(result: GaussNativeRows) -> list[list[Any]]:
    series = result.metric_series if result.metric_series is not None else normalize_metric_series(result.items)
    return [[s.check_name, s.check_item, s.check_status, s.name, s.unit,
             s.node_name, s.node_ip, p.observed_at, p.value, s.min, s.max, s.average] for s in series for p in s.points]


def export_native(instance: GaussInstance, panel: str, result: GaussNativeRows | GaussOverviewOut | GaussDiskHistoryOut,
                  conditions: dict[str, Any]) -> bytes:
    wb = new_workbook()
    condition_rows: list[list[Any]] = [
        ["租户", instance.tenant_name], ["项目", instance.project_name], ["实例", instance.name],
        ["实例 ID", instance.resource_id], ["来源", "GaussDB 原生存储 / 本地定时采样历史" if isinstance(
            result, GaussDiskHistoryOut) else "GaussDB 原生接口"], ["查询", panel],
        ["查询时间", result.queried_at], ["导出时区", get_settings().timezone],
        *[[key, value] for key, value in conditions.items()],
    ]
    if isinstance(result, GaussNativeRows | GaussDiskHistoryOut):
        condition_rows.extend([["状态", result.status], ["说明", result.warning],
                               ["原生结果可能未全部返回", result.truncated if isinstance(result, GaussNativeRows) else False]])
    if isinstance(result, GaussDiskHistoryOut):
        condition_rows.append(["时间口径", "RESPONSE_RECEIVED：本地响应接收时间，源端未提供采样时间"])
    write_table(wb.create_sheet("查询条件"), ["项目", "值"], condition_rows, widths=[30, 80])
    if isinstance(result, GaussDiskHistoryOut):
        write_table(wb.create_sheet("存储历史"),
                    ["时间桶起点", "获取时间（本地响应接收）", "已用（GB）", "总量（GB）", "使用率（%）"],
                    [[p.timestamp, p.collected_at, p.used_gb, p.total_gb, p.usage_percent] for p in result.points])
    elif isinstance(result, GaussOverviewOut):
        disk = result.disk
        time_note = "获取时间为本地响应接收时间，源端未提供采样时间"
        write_table(wb.create_sheet("磁盘与配置"), ["指标", "数值", "单位", "获取/快照时间", "说明"], [
            ["磁盘已用", disk.used_gb, "GB", disk.collected_at, "; ".join(filter(None, [disk.warning, time_note]))],
            ["磁盘总量", disk.total_gb, "GB", disk.collected_at, time_note],
            ["磁盘使用率", disk.usage_percent, "%", disk.collected_at, disk.status],
            ["配置 CPU", instance.vcpus, "vCPU", instance.snapshot_at, "配置量，非使用率"],
            ["配置内存原值", instance.memory_size, instance.memory_unit, instance.snapshot_at,
             "配置量，非使用率" if instance.memory_unit else "单位尚未确认"],
            ["当前 CPU 使用率", None, "%", None, result.performance_note],
            ["当前内存使用率", None, "%", None, result.performance_note],
        ])
        write_table(wb.create_sheet("节点组件"), ["节点 ID", "节点", "组件 ID", "类型", "角色", "状态"],
                    [[c.node_id, c.node_name, c.component_id, c.component_type, c.role, c.status] for c in result.components])
    elif panel == "inspection-detail":
        write_table(wb.create_sheet("巡检结果"), ["检查项", "项目标识", "类别", "状态", "结果", "可能原因", "建议"],
                    [[row.get(key) for key in ("name", "item", "inspection_category", "status", "inspection_result",
                                               "possible_causes", "suggestions")] for row in result.items])
        write_table(wb.create_sheet("巡检指标"), ["检查项", "项目标识", "状态", "指标", "原生单位", "节点", "IP",
                                                 "指标时间", "采样值", "原生最小值", "原生最大值", "原生均值"],
                    report_points(result))
        series = result.metric_series if result.metric_series is not None else normalize_metric_series(result.items)
        write_table(wb.create_sheet("巡检指标概览"), ["类别", "检查项", "指标", "原生单位", "节点", "IP", "最小值", "最大值",
                                                   "均值", "P95", "采样点数", "说明"],
                    [[s.metric, s.check_name, s.name, s.unit, s.node_name, s.node_ip, s.min, s.max, s.average, s.p95,
                      len(s.points), "已有巡检报告指标，不代表当前使用率"] for s in series])
    else:
        columns = SQL_COLUMNS if panel == "slow-sql" else SQL_DETAIL_COLUMNS if panel == "slow-sql-detail" else (
            SESSION_COLUMNS if panel == "sessions" else INSPECTION_COLUMNS)
        def value(row, key):
            raw = row.get(key)
            return finite_number(raw) if key in NUMERIC else _at(raw) if panel == "inspections" and key in (
                "start_time", "end_time") else raw
        write_table(wb.create_sheet("慢SQL" if panel == "slow-sql" else "慢SQL详情" if panel == "slow-sql-detail" else (
            "会话" if panel == "sessions" else "巡检记录")),
                    [label for label, _ in columns], [[value(row, key) for _, key in columns] for row in result.items])
    return to_bytes(wb)
