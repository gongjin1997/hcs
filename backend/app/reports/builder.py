"""报表统计口径（全部基于平台自己的数据：告警、巡检运行记录、资源台账，不依赖外部监控系统）。

- 新增告警：首次发生时间落在统计周期内的告警；MTTA / MTTR 也只统计这批告警
- 期末未恢复：周期结束时仍未恢复（含已认领、已抑制）
- 巡检成功率：周期内巡检任务运行记录中 SUCCESS 的占比（PARTIAL 计为部分成功）
- 台账：期末快照 + 周期内同步发现的新增 / 变更 / 释放
"""
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AlarmEvent, AlarmRule, HcsAccount, InspectionRun, InspectionTask, PodEvent, Resource, ResourceChange
from app.models.base import utcnow
from app.services import dashboard_service
from app.services.catalog import LEDGER_TYPES, RESOURCE_TYPES, SERVICES, ledger_categories, type_name

KINDS = {"DAILY": "日报", "WEEKLY": "周报", "MONTHLY": "月报", "LEDGER": "资源台账报表"}
SEVS = ("P1", "P2", "P3", "P4")


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def period_of(kind: str, ref: date | None = None) -> tuple[datetime, datetime]:
    """返回 [start, end)。ref 为空时取上一个完整周期（昨天 / 上周 / 上月）；台账报表为当前时刻快照。"""
    tz, now = _tz(), utcnow()
    today = now.astimezone(tz).date()
    if kind == "LEDGER":
        return now, now
    if kind == "DAILY":
        d = ref or today - timedelta(days=1)
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        return start, start + timedelta(days=1)
    if kind == "WEEKLY":
        d = ref or today - timedelta(days=7)
        monday = d - timedelta(days=d.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
        return start, start + timedelta(days=7)
    if kind == "MONTHLY":
        d = ref or (today.replace(day=1) - timedelta(days=1))
        start = datetime(d.year, d.month, 1, tzinfo=tz)
        nxt = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=tz)
        return start, nxt
    raise ValueError(f"unknown report kind: {kind}")


def title_of(kind: str, start: datetime, end: datetime, tenant: str | None = None) -> str:
    s, e = start.astimezone(_tz()), (end - timedelta(seconds=1)).astimezone(_tz())
    who = f" · {tenant}" if tenant else ""
    if kind == "DAILY":
        return f"HCS 巡检日报{who}（{s:%Y-%m-%d}）"
    if kind == "WEEKLY":
        return f"HCS 巡检周报{who}（{s:%Y-%m-%d} ~ {e:%m-%d}）"
    if kind == "MONTHLY":
        return f"HCS 巡检月报{who}（{s:%Y 年 %m 月}）"
    return f"资源台账报表{who}（{start.astimezone(_tz()):%Y-%m-%d %H:%M}）"


def _in(q, col, tenant_ids: list[int] | None):
    return q if tenant_ids is None else q.where(col.in_(tenant_ids))


def _avg_min(deltas: list[timedelta]) -> float | None:
    return round(sum(d.total_seconds() for d in deltas) / len(deltas) / 60, 1) if deltas else None


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return round((a - b) * 100 / b, 1)


def _alarm_stats(alarms: Sequence[AlarmEvent]) -> dict:
    sev = Counter(a.severity for a in alarms)
    major = [a for a in alarms if a.severity in ("P1", "P2")]
    return {"new_alarms": len(alarms), **{s.lower(): sev.get(s, 0) for s in SEVS},
            "resolved": sum(1 for a in alarms if a.resolved_at is not None),
            "mtta_min": _avg_min([a.ack_at - a.first_seen for a in alarms if a.ack_at]),
            "mttr_min": _avg_min([a.resolved_at - a.first_seen for a in alarms if a.resolved_at]),
            "ack_rate": round(sum(1 for a in major if a.ack_at) * 100 / len(major), 1) if major else None}


def _buckets(kind: str, start: datetime, end: datetime) -> tuple[list[str], list[datetime]]:
    tz = _tz()
    if kind == "DAILY":
        points = [start + timedelta(hours=i) for i in range(24)]
        return [p.astimezone(tz).strftime("%H:00") for p in points], points
    days = (end - start).days
    points = [start + timedelta(days=i) for i in range(days)]
    return [p.astimezone(tz).strftime("%m-%d") for p in points], points


def _ledger(db: Session, start: datetime, end: datetime, tenant_ids: list[int] | None = None) -> dict:
    rows = db.scalars(_in(select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.resource_type.in_(
        LEDGER_TYPES)), Resource.tenant_id, tenant_ids)).all()
    # 台账变更只统计新增 / 变更 / 释放；ECS 状态变更、重启在 ECS 小节单独统计
    ch_q = _in(select(ResourceChange).where(ResourceChange.source == "SYNC", ResourceChange.change_type.in_(
        ("CREATED", "UPDATED", "DELETED"))), ResourceChange.tenant_id, tenant_ids)
    if start < end:
        ch_q = ch_q.where(ResourceChange.detected_at >= start, ResourceChange.detected_at < end)
    else:   # 台账报表：近 30 天变更
        ch_q = ch_q.where(ResourceChange.detected_at >= end - timedelta(days=30))
    changes = db.scalars(ch_q.order_by(ResourceChange.detected_at.desc())).all()
    cnt = Counter(r.resource_type for r in rows)
    ecs = [r for r in rows if r.resource_type == "ecs_server"]
    return {
        "total": len(rows),
        "by_category": [{"code": c["code"], "name": c["name"],
                         "count": sum(n for t, n in cnt.items() if RESOURCE_TYPES[t]["category"] == c["code"])}
                        for c in ledger_categories()],
        "by_type": [{"code": t, "name": type_name(t), "count": cnt.get(t, 0),
                     "unassigned": sum(1 for r in rows if r.resource_type == t and not r.owner)} for t in LEDGER_TYPES],
        "vcpus": sum(r.vcpus or 0 for r in ecs), "memory_gb": round(sum(r.memory_gb or 0 for r in ecs), 1),
        "storage_gb": round(sum(r.storage_gb or 0 for r in rows if r.resource_type == "evs_volume"), 1),
        "unassigned": sum(1 for r in rows if not r.owner),
        "idle": sum(1 for r in rows if (r.resource_type, r.status_raw) in
                    (("evs_volume", "available"), ("eip", "DOWN"), ("ecs_server", "SHUTOFF"))),
        "changes_count": {k: sum(1 for c in changes if c.change_type == k) for k in ("CREATED", "UPDATED", "DELETED")},
        "changes": [{"change_type": c.change_type, "type_name": type_name(c.resource_type), "name": c.name,
                     "detected_at": c.detected_at.isoformat(),
                     "summary": "；".join(f"{x['label']} {x['old']} → {x['new']}" for x in c.changes)[:120]}
                    for c in changes[:50]],
    }


def _capacity(db: Session, tenant_ids: list[int] | None = None) -> list[dict]:
    rows = db.scalars(_in(select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.resource_type.in_(
        ("dcs_instance", "gaussdb_instance", "kafka_instance", "ecs_server"))), Resource.tenant_id, tenant_ids)).all()
    out = []
    for r in rows:
        v, metric = None, ""
        if r.resource_type == "dcs_instance":
            v, metric = r.metrics.get("memory_usage"), "内存使用率"
        elif r.resource_type == "gaussdb_instance":
            v, metric = r.metrics.get("disk_usage"), "磁盘使用率"
        elif r.resource_type == "ecs_server":
            v, metric = r.metrics.get("disk_util"), f"磁盘使用率（{r.metrics.get('disk_mount', '-')}）"
        else:
            v, metric = (r.attrs or {}).get("storage_usage"), "存储使用率"
        if v is not None:
            out.append({"name": r.name, "type_name": type_name(r.resource_type), "metric": metric, "value": v})
    return sorted(out, key=lambda x: x["value"], reverse=True)[:10]


def _conclusion(kind: str, ov: dict, cmp: dict, key_alarms: list[dict], ledger: dict, tasks: list[dict]) -> list[str]:
    word = {"DAILY": "当日", "WEEKLY": "本周", "MONTHLY": "本月"}.get(kind, "")
    lines = []
    if kind != "LEDGER":
        trend = cmp.get("new_alarms")
        tr = "" if trend is None else "，与上期持平" if trend == 0 else \
            f"，较上期{'增加' if trend > 0 else '减少'} {abs(trend):g}%"
        lines.append(f"{word}新增告警 {ov['new_alarms']} 条（P1 {ov['p1']} 条、P2 {ov['p2']} 条）{tr}；"
                     f"期末仍有 {ov['active_at_end']} 条未恢复。")
        eff = []
        if ov["mtta_min"] is not None:
            eff.append(f"平均认领 {ov['mtta_min']:g} 分钟")
        if ov["mttr_min"] is not None:
            eff.append(f"平均恢复 {ov['mttr_min']:g} 分钟")
        if ov["ack_rate"] is not None:
            eff.append(f"P1/P2 认领率 {ov['ack_rate']:g}%")
        if eff:
            lines.append("处置效率：" + "，".join(eff) + "。")
        bad = [t for t in tasks if t["failed"]]
        lines.append(f"巡检共执行 {ov['inspection_runs']} 次，成功率 {ov['run_success_rate']:g}%"
                     + (f"；失败较多的任务：{'、'.join(t['name'] for t in bad[:3])}。" if bad else "，无失败任务。"))
        if key_alarms:
            names = list(dict.fromkeys(a["resource_name"] for a in key_alarms if a["severity"] == "P1"))[:3]
            if names:
                lines.append(f"重点关注：{'、'.join(names)} 出现紧急告警。")
    cc = ledger["changes_count"]
    lines.append(f"资源台账共 {ledger['total']} 个资源，{word or '近 30 天'}新增 {cc['CREATED']}、变更 {cc['UPDATED']}、"
                 f"释放 {cc['DELETED']}；未分配负责人 {ledger['unassigned']} 个，疑似闲置 {ledger['idle']} 个。")
    return lines


def _runtime_lines(ecs: dict, pods: dict) -> list[str]:
    """ECS 与容器的结论（插在台账结论之前）。"""
    lines = []
    if ecs.get("total"):
        bad = []
        if ecs["error"]:
            bad.append(f"故障 {ecs['error']} 台")
        if ecs["shutoff"]:
            bad.append(f"关机 {ecs['shutoff']} 台")
        top = "、".join(f"{x['name']}（{x['count']} 次）" for x in ecs["top_reboot"][:3])
        lines.append(f"ECS 共 {ecs['total']} 台，期末运行中 {ecs['active']} 台" + (f"、{'、'.join(bad)}" if bad else "")
                     + f"；本期状态变更 {ecs['status_changes']} 次、重启 {ecs['reboots']} 次"
                     + (f"，重启较多：{top}。" if top else "。"))
    if pods.get("events"):
        top = "、".join(f"{x['workload'].split('/')[-1]}（{x['count']} 次）" for x in pods["top_restart"][:3])
        lines.append(f"容器事件 {pods['events']} 条：容器重启 {pods['restarts']} 次（OOM {pods['oom']} 次）、"
                     f"CrashLoopBackOff {pods['crashloop']} 次、背压 {pods['backpressure']} 次"
                     f"（累计 {pods['backpressure_min']} 分钟）、驱逐 {pods['evicted']} 次"
                     + (f"；重启最多：{top}。" if top else "。"))
    return lines


def _ecs_section(db: Session, start: datetime, end: datetime, tenant_ids: list[int] | None) -> dict:
    """ECS：期末状态分布 + 本期状态变更 / 重启次数。"""
    ecs = db.scalars(_in(select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.resource_type == "ecs_server"),
                         Resource.tenant_id, tenant_ids)).all()
    ch = db.scalars(_in(select(ResourceChange).where(
        ResourceChange.resource_type == "ecs_server", ResourceChange.change_type.in_(("STATUS", "REBOOT")),
        ResourceChange.detected_at >= start, ResourceChange.detected_at < end), ResourceChange.tenant_id,
        tenant_ids)).all()
    reboots = [c for c in ch if c.change_type == "REBOOT" or any(x.get("new") in ("REBOOT", "HARD_REBOOT")
                                                                for x in c.changes or [])]
    by = Counter(r.status_raw or "UNKNOWN" for r in ecs)
    return {"total": len(ecs), "active": by.get("ACTIVE", 0), "shutoff": by.get("SHUTOFF", 0),
            "error": by.get("ERROR", 0), "status_changes": len(ch), "reboots": len(reboots),
            "top_reboot": [{"name": n, "count": c} for n, c in Counter(x.name for x in reboots).most_common(5)]}


def _pod_section(db: Session, start: datetime, end: datetime, tenant_ids: list[int] | None) -> dict:
    """容器事件：本期重启 / OOM / CrashLoop / 背压 / 驱逐次数与 Top 工作负载。"""
    rows = db.scalars(_in(select(PodEvent).where(PodEvent.first_time < end, PodEvent.last_time >= start),
                          PodEvent.tenant_id, tenant_ids)).all()
    by = Counter(e.event_type for e in rows)
    restarts = Counter(f"{e.cluster_name}/{e.namespace}/{e.workload}" for e in rows
                       if e.event_type in ("RESTART", "OOM_KILLED"))
    bp_min = 0.0
    for e in rows:
        if e.event_type == "BACKPRESSURE":
            s0, e0 = max(e.first_time, start), min(e.end_time or e.last_time if not e.ongoing else end, end)
            bp_min += max(0.0, (e0 - s0).total_seconds() / 60)
    return {"events": len(rows), "restarts": by.get("RESTART", 0) + by.get("OOM_KILLED", 0),
            "oom": by.get("OOM_KILLED", 0), "crashloop": by.get("CRASH_LOOP", 0),
            "backpressure": by.get("BACKPRESSURE", 0), "backpressure_min": round(bp_min),
            "evicted": by.get("EVICTED", 0),
            "top_restart": [{"workload": k, "count": n} for k, n in restarts.most_common(8)]}


def _by_tenant(db: Session, alarms: Sequence[AlarmEvent], tenant_ids: list[int] | None) -> list[dict]:
    out = []
    for t in db.scalars(_in(select(HcsAccount).order_by(HcsAccount.id), HcsAccount.id, tenant_ids)):
        mine = [a for a in alarms if a.tenant_id == t.id]
        out.append({"tenant_id": t.id, "name": t.short_name or t.name, "new_alarms": len(mine),
                    "p1": sum(1 for a in mine if a.severity == "P1"), "p2": sum(1 for a in mine if a.severity == "P2"),
                    "health_score": dashboard_service.summary(db, tenant_ids=[t.id])["health_score"]})
    return out


def build_report(db: Session, kind: str, start: datetime, end: datetime,
                 tenant_id: int | None = None) -> dict[str, Any]:
    tz = _tz()
    tenant = db.get(HcsAccount, tenant_id) if tenant_id else None
    tids = [tenant_id] if tenant_id else None
    ledger = _ledger(db, start, end, tids)
    base: dict[str, Any] = {"kind": kind, "kind_name": KINDS[kind],
                            "title": title_of(kind, start, end, (tenant.short_name or tenant.name) if tenant else None),
                            "tenant": {"id": tenant.id, "name": tenant.name, "short_name": tenant.short_name or tenant.name}
                            if tenant else None,
                            "period": {"start": start.isoformat(), "end": end.isoformat()},
                            "generated_at": utcnow().isoformat(), "ledger": ledger}
    if kind == "LEDGER":
        base["overview"] = {"resources": ledger["total"], "unassigned": ledger["unassigned"], "idle": ledger["idle"]}
        base["conclusion"] = _conclusion(kind, {}, {}, [], ledger, [])
        return base

    alarms = db.scalars(_in(select(AlarmEvent).where(AlarmEvent.first_seen >= start, AlarmEvent.first_seen < end),
                            AlarmEvent.tenant_id, tids)).all()
    prev_start = start - (end - start)
    prev = db.scalars(_in(select(AlarmEvent).where(AlarmEvent.first_seen >= prev_start, AlarmEvent.first_seen < start),
                          AlarmEvent.tenant_id, tids)).all()
    open_at_end = db.scalars(_in(select(AlarmEvent).where(AlarmEvent.first_seen < end), AlarmEvent.tenant_id,
                                 tids)).all()
    active_at_end = sum(1 for a in open_at_end if a.status != "CLOSED" and (a.resolved_at is None or a.resolved_at >= end))
    runs = db.scalars(_in(select(InspectionRun).where(InspectionRun.started_at >= start, InspectionRun.started_at < end),
                          InspectionRun.tenant_id, tids)).all()
    cur, old = _alarm_stats(alarms), _alarm_stats(prev)
    # 平台上线前没有上期数据：不做环比，避免出现“较上期减少 100%”之类的误导结论
    first_run = db.scalar(select(func.min(InspectionRun.started_at)))
    has_prev = first_run is not None and first_run <= prev_start + timedelta(hours=1)

    labels, points = _buckets(kind, start, end)
    step = timedelta(hours=1) if kind == "DAILY" else timedelta(days=1)
    series = {s: [0] * len(points) for s in SEVS}
    for a in alarms:
        idx = int((a.first_seen - start) / step)
        if 0 <= idx < len(points):
            series[a.severity][idx] += 1

    by_service: dict[str, Counter] = defaultdict(Counter)
    for a in alarms:
        by_service[a.service][a.severity] += 1
    res_cnt: dict[tuple[str, str], list[AlarmEvent]] = defaultdict(list)
    for a in alarms:
        res_cnt[(a.resource_name, a.resource_type)].append(a)
    rule_names = dict(db.execute(select(AlarmRule.id, AlarmRule.name)).all())
    rule_cnt = Counter(a.rule_id for a in alarms)
    task_names = dict(db.execute(select(InspectionTask.code, InspectionTask.name)).all())
    task_runs: dict[str, list[InspectionRun]] = defaultdict(list)
    for r in runs:
        task_runs[r.task_code].append(r)
    tasks = []
    for code, rs in sorted(task_runs.items()):
        ok_n = sum(1 for r in rs if r.status == "SUCCESS")
        durs = [(r.finished_at - r.started_at).total_seconds() for r in rs if r.finished_at]
        tasks.append({"code": code, "name": task_names.get(code, code), "runs": len(rs), "success": ok_n,
                      "partial": sum(1 for r in rs if r.status == "PARTIAL"),
                      "failed": sum(1 for r in rs if r.status in ("FAILED", "TIMEOUT")),
                      "success_rate": round(ok_n * 100 / len(rs), 1),
                      "avg_sec": round(sum(durs) / len(durs), 1) if durs else None})
    key = sorted([a for a in alarms if a.severity in ("P1", "P2")], key=lambda a: (a.severity, a.first_seen))[:20]
    key_alarms = [{"severity": a.severity, "title": a.title, "resource_name": a.resource_name,
                   "type_name": type_name(a.resource_type), "first_seen": a.first_seen.isoformat(),
                   "duration_min": round(((a.resolved_at or min(end, utcnow())) - a.first_seen).total_seconds() / 60, 1),
                   "status": a.status, "ack_by": a.ack_by} for a in key]
    total_runs = len(runs)
    overview = {**cur, "active_at_end": active_at_end, "inspection_runs": total_runs,
                "run_success_rate": round(sum(1 for r in runs if r.status == "SUCCESS") * 100 / total_runs, 1)
                if total_runs else 100.0,
                "health_score_now": dashboard_service.summary(db, tenant_ids=tids)["health_score"]}
    compare = {k: _pct(cur[k], old[k]) if has_prev else None for k in ("new_alarms", "p1", "p2", "mtta_min", "mttr_min")}
    base.update({
        "overview": overview, "previous": old if has_prev else None, "compare": compare,
        "trend": {"labels": labels, "series": series},
        "by_service": [{"service": s, "name": SERVICES.get(s, s), "count": sum(c.values()),
                        **{x.lower(): c.get(x, 0) for x in SEVS}}
                       for s, c in sorted(by_service.items(), key=lambda kv: -sum(kv[1].values()))],
        "top_resources": [{"name": n, "type_name": type_name(t), "count": len(v), "max_severity": min(a.severity for a in v)}
                          for (n, t), v in sorted(res_cnt.items(), key=lambda kv: -len(kv[1]))[:10]],
        "top_rules": [{"rule_id": rid, "name": rule_names.get(rid, rid), "count": n} for rid, n in rule_cnt.most_common(10)],
        "key_alarms": key_alarms, "tasks": tasks, "capacity": _capacity(db, tids),
        "ecs": _ecs_section(db, start, end, tids), "pods": _pod_section(db, start, end, tids),
        "by_tenant": [] if tenant_id else _by_tenant(db, alarms, None),
    })
    base["conclusion"] = _conclusion(kind, overview, compare, key_alarms, ledger, tasks)
    base["conclusion"][-1:-1] = _runtime_lines(base["ecs"], base["pods"])
    base["period"]["label"] = f"{start.astimezone(tz):%Y-%m-%d %H:%M} ~ {end.astimezone(tz):%Y-%m-%d %H:%M}"
    return base


def summary_of(data: dict) -> dict:
    ov = data.get("overview", {})
    keys = ("new_alarms", "p1", "p2", "mttr_min", "run_success_rate", "active_at_end", "resources", "unassigned")
    out = {k: ov[k] for k in keys if k in ov}
    out["ledger_total"] = data["ledger"]["total"]
    out["ledger_changes"] = sum(data["ledger"]["changes_count"].values())
    return out
