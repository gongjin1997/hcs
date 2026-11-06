"""报表导出 Excel：概览（KPI + 结论）+ 各明细页；台账报表附带完整台账明细。"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.xlsx import TITLE_FONT, new_workbook, to_bytes, write_table
from app.ledger.excel import add_ledger_sheets
from app.services.catalog import LEDGER_TYPES, SEVERITY_LABEL, STATUS_LABEL

CHANGE_LABEL = {"CREATED": "新增", "UPDATED": "变更", "DELETED": "释放", "STATUS": "状态变更", "REBOOT": "重启"}
KPI = [("new_alarms", "新增告警", "条"), ("p1", "P1 紧急", "条"), ("p2", "P2 重要", "条"), ("resolved", "已恢复", "条"),
       ("active_at_end", "期末未恢复", "条"), ("mtta_min", "平均认领时长", "分钟"), ("mttr_min", "平均恢复时长", "分钟"),
       ("ack_rate", "P1/P2 认领率", "%"), ("inspection_runs", "巡检执行次数", "次"), ("run_success_rate", "巡检成功率", "%"),
       ("health_score_now", "当前健康分", "分")]


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _overview(wb, data: dict) -> None:
    ws = wb.create_sheet("概览")
    ws["A1"], ws["A1"].font = data["title"], TITLE_FONT
    ws["A2"] = f"统计周期：{data['period'].get('label', '-')}；生成时间：{_dt(data['generated_at']):%Y-%m-%d %H:%M}"
    ws["A3"] = f"统计范围：{(data.get('tenant') or {}).get('name') or '全部租户'}"
    row = 5
    if data["kind"] != "LEDGER":
        ov, prev, cmp = data["overview"], data.get("previous") or {}, data.get("compare") or {}
        rows = [[label, ov.get(k), prev.get(k, "-"), "-" if cmp.get(k) is None else f"{cmp[k]:+g}%", unit]
                for k, label, unit in KPI if k in ov]
        write_table(ws, ["指标", "本期", "上期", "环比", "单位"], rows, [18, 12, 12, 10, 8], start_row=row)
        row += len(rows) + 2
    ws.cell(row=row, column=1, value="结论").font = TITLE_FONT
    for i, line in enumerate(data["conclusion"], 1):
        ws.cell(row=row + i, column=1, value=f"{i}. {line}")
    ws.freeze_panes = None


def _runtime_sheets(wb, data: dict) -> None:
    """ECS 状态、容器事件、分租户对比（v0.3 新增；旧报表没有这些数据时跳过）。"""
    ecs, pods = data.get("ecs"), data.get("pods")
    if ecs:
        ws = wb.create_sheet("ECS 状态")
        write_table(ws, ["指标", "数值"],
                    [["生成时 ECS 总数", ecs["total"]], ["生成时运行中", ecs["active"]],
                     ["生成时关机", ecs["shutoff"]], ["生成时故障", ecs["error"]],
                     ["本期状态变更次数", ecs["status_changes"]], ["本期重启次数", ecs["reboots"]]], [22, 12])
        write_table(ws, ["重启较多的云服务器", "重启次数"], [[x["name"], x["count"]] for x in ecs["top_reboot"]],
                    start_row=10)
        ws.column_dimensions["A"].width = 30
    if pods:
        ws = wb.create_sheet("容器事件")
        write_table(ws, ["指标", "数值"],
                    [["事件总数", pods["events"]], ["容器重启（含 OOM）", pods["restarts"]], ["OOM 终止", pods["oom"]],
                     ["CrashLoopBackOff", pods["crashloop"]], ["背压次数", pods["backpressure"]],
                     ["背压累计时长（分钟）", pods["backpressure_min"]], ["Pod 驱逐", pods["evicted"]]], [24, 12])
        write_table(ws, ["重启最多的工作负载（集群/命名空间/工作负载）", "重启次数"],
                    [[x["workload"], x["count"]] for x in pods["top_restart"]], start_row=11)
        ws.column_dimensions["A"].width = 44
    if data.get("by_tenant"):
        write_table(wb.create_sheet("分租户"), ["租户", "新增告警", "P1", "P2", "当前健康分"],
                    [[t["name"], t["new_alarms"], t["p1"], t["p2"], t["health_score"]] for t in data["by_tenant"]],
                    [24, 10, 8, 8, 12])


def report_workbook(db: Session, data: dict) -> bytes:
    wb = new_workbook()
    _overview(wb, data)
    if data["kind"] != "LEDGER":
        tr = data["trend"]
        write_table(wb.create_sheet("告警趋势"), ["时间", "P1", "P2", "P3", "P4"],
                    [[lb, *(tr["series"][s][i] for s in ("P1", "P2", "P3", "P4"))] for i, lb in enumerate(tr["labels"])],
                    [12, 8, 8, 8, 8])
        write_table(wb.create_sheet("按服务"), ["服务", "告警数", "P1", "P2", "P3", "P4"],
                    [[s["name"], s["count"], s["p1"], s["p2"], s["p3"], s["p4"]] for s in data["by_service"]],
                    [22, 10, 8, 8, 8, 8])
        write_table(wb.create_sheet("重点告警"),
                    ["级别", "告警", "资源", "资源类型", "首次发生", "持续(分钟)", "状态", "认领人"],
                    [[f"{a['severity']}-{SEVERITY_LABEL[a['severity']]}", a["title"], a["resource_name"], a["type_name"],
                      _dt(a["first_seen"]), a["duration_min"], STATUS_LABEL.get(a["status"], a["status"]), a["ack_by"]]
                     for a in data["key_alarms"]], [10, 34, 24, 16, 18, 12, 10, 10])
        write_table(wb.create_sheet("Top 资源与规则"), ["告警最多的资源", "资源类型", "告警数", "最高级别"],
                    [[r["name"], r["type_name"], r["count"], r["max_severity"]] for r in data["top_resources"]],
                    [28, 16, 10, 10])
        ws = wb["Top 资源与规则"]
        write_table(ws, ["触发最多的规则", "规则编号", "触发次数"],
                    [[r["name"], r["rule_id"], r["count"]] for r in data["top_rules"]],
                    start_row=len(data["top_resources"]) + 4)
        ws.column_dimensions["A"].width = 32
        write_table(wb.create_sheet("巡检任务"),
                    ["任务", "运行记录", "成功", "部分成功", "失败", "跳过", "运行中", "已完成成功率(%)", "平均耗时(秒)"],
                    [[t["name"], t["runs"], t["success"], t["partial"], t["failed"], t.get("skipped", 0),
                      t.get("running", 0), t["success_rate"], t["avg_sec"]]
                     for t in data["tasks"]], [22, 10, 8, 10, 8, 8, 8, 18, 14])
        write_table(wb.create_sheet("容量水位"), ["资源", "资源类型", "指标", "当前值(%)"],
                    [[c["name"], c["type_name"], c["metric"], c["value"]] for c in data["capacity"]], [26, 16, 14, 12])
        _runtime_sheets(wb, data)
    lg = data["ledger"]
    write_table(wb.create_sheet("台账变更"), ["类型", "资源类型", "资源", "发现时间", "变更内容"],
                [[CHANGE_LABEL.get(c["change_type"], c["change_type"]), c["type_name"], c["name"], _dt(c["detected_at"]),
                  c["summary"]] for c in lg["changes"]], [8, 16, 28, 18, 60])
    if data["kind"] == "LEDGER":   # 台账报表：附完整明细（导出时刻的最新台账；租户报表只含该租户）
        tenant = data.get("tenant") or {}
        add_ledger_sheets(wb, db, LEDGER_TYPES, tenant_ids=[tenant["id"]] if tenant.get("id") else None)
    return to_bytes(wb)
