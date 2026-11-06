"""总览页 / 大屏数据：健康分、租户卡片、服务健康、ECS 状态分布、容器事件、24h 趋势、Top 告警、容量 Top5、巡检任务状态。

tenant_ids 为数据范围（用户可见租户 ∩ 顶部租户切换），None 表示全部。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.k8s.catalog import EVENT_TYPES
from app.k8s.semantics import window_count
from app.metrics import CAPABILITIES, effective, mode
from app.models import AlarmEvent, HcsAccount, HcsProject, InspectionRun, InspectionTask, PodEvent, Resource, ResourceChange
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.alarm import AlarmOut
from app.services.ecs_monitoring import snapshots as ecs_snapshots
from app.services.inspection_health import inspection_state
from app.services.metric_visibility import current_metrics

from .catalog import CRITICAL_WEIGHT, ECS_STATUS, HEALTH_GROUPS, LEDGER_TYPES, RESOURCE_SCORE, RESOURCE_TYPES, ledger_categories
from .platform_watch import NEUTRAL, scope_execution_status, task_last_status, task_scopes

SEVS = ("P1", "P2", "P3", "P4")


def _score(resources: list[Resource], worst: dict[int, str], now: datetime | None = None) -> float | None:
    num = den = 0.0
    for r in resources:
        if inspection_state(r, now) != "FRESH" and r.id not in worst:
            continue
        w = CRITICAL_WEIGHT if r.critical else 1
        num += RESOURCE_SCORE[worst.get(r.id)] * w
        den += w
    return round(num / den, 1) if den else None


def _minutes(deltas: list[timedelta]) -> float | None:
    return round(sum(d.total_seconds() for d in deltas) / len(deltas) / 60, 1) if deltas else None


def _in(q, col, tenant_ids: list[int] | None):
    return q if tenant_ids is None else q.where(col.in_(tenant_ids))


def ledger_overview(db: Session, now: datetime, tenant_ids: list[int] | None = None) -> dict:
    rows = db.execute(_in(select(Resource.resource_type, Resource.owner).where(
        Resource.lifecycle == "ACTIVE", Resource.resource_type.in_(LEDGER_TYPES)), Resource.tenant_id,
        tenant_ids)).all()
    changes = Counter(db.scalars(_in(select(ResourceChange.change_type).where(
        ResourceChange.source == "SYNC", ResourceChange.detected_at >= now - timedelta(days=7)),
        ResourceChange.tenant_id, tenant_ids)).all())
    cnt = Counter(t for t, _ in rows)
    return {"total": len(rows), "unassigned": sum(1 for _, o in rows if not o),
            "by_category": [{"code": c["code"], "name": c["name"],
                             "count": sum(n for t, n in cnt.items() if RESOURCE_TYPES[t]["category"] == c["code"])}
                            for c in ledger_categories()],
            "changes_7d": {k: changes.get(k, 0) for k in ("CREATED", "UPDATED", "DELETED")}}


def _ecs(resources: list[Resource], metrics: dict[int, dict] | None = None) -> dict:
    rs = [r for r in resources if r.resource_type == "ecs_server"]
    c = Counter(r.status_raw or "UNKNOWN" for r in rs)
    main = ("ACTIVE", "SHUTOFF", "ERROR")
    return {"total": len(rs), "by_status": [{"status": k, "name": ECS_STATUS.get(k, k), "count": c.get(k, 0)}
                                            for k in main] +
            [{"status": "OTHER", "name": "其他（变更中 / 未知）", "count": sum(n for k, n in c.items() if k not in main)}],
            "with_metrics": sum(1 for r in rs if (metrics or {}).get(r.id, {}).get("cpu_util") is not None)}


def _pods(db: Session, now: datetime, tenant_ids: list[int] | None) -> dict:
    rows = db.scalars(_in(select(PodEvent).where(PodEvent.last_time >= now - timedelta(hours=24)),
                          PodEvent.tenant_id, tenant_ids)).all()
    ongoing = db.scalars(_in(select(PodEvent).where(PodEvent.ongoing.is_(True)), PodEvent.tenant_id,
                             tenant_ids)).all()
    since = now - timedelta(hours=24)

    def total(values: list[int | None]) -> int | None:
        return None if any(value is None for value in values) else sum(value for value in values if value is not None)

    restarts: dict[tuple, list[int | None]] = defaultdict(list)
    for event in rows:
        if event.event_type in ("RESTART", "OOM_KILLED"):
            key = (event.tenant_id, event.project_ref, event.cluster_id, event.namespace,
                   event.workload_kind, event.workload)
            restarts[key].append(window_count(event, since, restarts=True))
    ranked = [{"tenant_id": key[0], "project_ref": key[1], "cluster_id": key[2], "namespace": key[3],
               "workload_kind": key[4], "workload_name": key[5], "workload": f"{key[3]}/{key[5]}",
               "scope_key": f"{key[0]}:{key[1]}:{key[2]}:{key[3]}:{key[4]}:{key[5]}", "count": total(values)}
              for key, values in restarts.items()]
    ranked.sort(key=lambda row: (row["count"] is not None, -(row["count"] or 0), row["scope_key"]))
    return {"restarts_24h": total([value for values in restarts.values() for value in values]),
            "oom_24h": total([window_count(e, since) for e in rows if e.event_type == "OOM_KILLED"]),
            "events_24h": len(rows),
            "crashloop": sum(1 for e in ongoing if e.event_type == "CRASH_LOOP"),
            "backpressure": sum(1 for e in ongoing if e.event_type == "BACKPRESSURE"),
            "top_restart": ranked[:5],
            # 进行中的持续型事件（CrashLoop / 背压 …）：严重优先、最近优先，首页展示前 4 条
            "ongoing": [{"id": e.id, "event_type": e.event_type,
                         "type_name": EVENT_TYPES.get(e.event_type, {}).get("name", e.event_type),
                         "color": EVENT_TYPES.get(e.event_type, {}).get("color", "#8c8c8c"), "level": e.level,
                         "workload": f"{e.namespace}/{e.workload}", "cluster_name": e.cluster_name, "tenant_id": e.tenant_id,
                         "project_ref": e.project_ref, "cluster_id": e.cluster_id, "namespace": e.namespace,
                         "workload_kind": e.workload_kind,
                         "first_time": e.first_time.isoformat()}
                        for e in sorted(ongoing, key=lambda e: (e.level != "CRITICAL", -e.last_time.timestamp()))[:4]],
            "ongoing_total": len(ongoing)}


def summary(db: Session, now: datetime | None = None, tenant_ids: list[int] | None = None) -> dict:
    now = now or utcnow()
    tz = ZoneInfo(get_settings().timezone)
    inspected = [k for k, v in RESOURCE_TYPES.items() if v.get("group")]     # 健康分只统计纳入巡检的资源类型
    resources = db.scalars(_in(select(Resource).where(Resource.lifecycle == "ACTIVE", Resource.monitored.is_(True),
                                                      Resource.resource_type.in_(inspected)), Resource.tenant_id,
                               tenant_ids)).all()
    snapshots = ecs_snapshots(db, resources)
    visible = {r.id: current_metrics(r, snapshots.get(r.id), now=now) for r in resources}
    active = db.scalars(_in(select(AlarmEvent).where(AlarmEvent.status.in_(ACTIVE_STATUSES)), AlarmEvent.tenant_id,
                            tenant_ids)).all()
    recent = db.scalars(_in(select(AlarmEvent).where(AlarmEvent.first_seen >= now - timedelta(hours=24)),
                            AlarmEvent.tenant_id, tenant_ids)).all()
    resource_ids = {r.id for r in resources}
    worst: dict[int, str] = {}
    for a in active:
        if a.resource_ref is not None and a.resource_ref in resource_ids and a.status != "SUPPRESSED":
            worst[a.resource_ref] = min(worst.get(a.resource_ref, "P9"), a.severity)

    groups: list[dict[str, Any]] = []
    for g in HEALTH_GROUPS:
        rs = [r for r in resources if RESOURCE_TYPES[r.resource_type].get("group") == g["code"]]
        al = [a for a in active if RESOURCE_TYPES.get(a.resource_type, {}).get("group") == g["code"]]
        c = Counter(a.severity for a in al)
        known = sum(inspection_state(r, now) == "FRESH" or r.id in worst for r in rs)
        groups.append({**g, "score": _score(rs, worst, now), "resources": len(rs), "assessed_resources": known,
                       "unknown_resources": len(rs) - known,
                       "abnormal": sum(1 for r in rs if r.id in worst), "alarms": {s: c.get(s, 0) for s in SEVS}})
    total = sum(g["assessed_resources"] for g in groups)
    platform = (round(sum(g["score"] * g["assessed_resources"] for g in groups if g["score"] is not None) / total, 1)
                if total else None)

    start = now.astimezone(tz).replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    hours = [start + timedelta(hours=i) for i in range(24)]
    series = {s: [0] * 24 for s in SEVS}
    for a in recent:
        idx = int((a.first_seen.astimezone(tz) - start).total_seconds() // 3600)
        if 0 <= idx < 24:
            series[a.severity][idx] += 1

    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    top = sorted(active, key=lambda a: (a.severity, a.status != "FIRING", -a.first_seen.timestamp()))[:10]

    def top5(rtype: str, key: str) -> list[dict]:
        rows = [r for r in resources if r.resource_type == rtype and visible[r.id].get(key) is not None]
        rows.sort(key=lambda r: visible[r.id][key], reverse=True)
        return [{"name": r.name, "value": visible[r.id][key], "resource_id": r.id} for r in rows[:5]]

    tenants = []
    tq = select(HcsAccount).where(HcsAccount.enabled.is_(True)).order_by(HcsAccount.id)
    for t in db.scalars(_in(tq, HcsAccount.id, tenant_ids)):
        rs = [r for r in resources if r.tenant_id == t.id]
        sev = Counter(a.severity for a in active if a.tenant_id == t.id)
        caps = t.capabilities or {}
        tenants.append({"id": t.id, "code": t.code, "name": t.name, "short_name": t.short_name or t.name,
                        "color": t.color, "auth_type": t.auth_type, "score": _score(rs, worst, now), "resources": len(rs),
                        "unknown_resources": sum(inspection_state(r, now) != "FRESH" and r.id not in worst for r in rs),
                        "abnormal": sum(1 for r in rs if r.id in worst), "alarms": {s: sev.get(s, 0) for s in SEVS},
                        "metrics_source": mode(t), "metrics_effective": effective(t), "ecs": _ecs(rs)["by_status"],
                        "capabilities": {k: (caps.get(k) or {}).get("available") for k in CAPABILITIES},
                        "last_check_ok": (t.last_check or {}).get("ok"), "last_check_at": t.last_check_at})

    tasks = []
    scopes = task_scopes(db, tenant_ids=tenant_ids)
    configured = set(db.execute(_in(select(HcsProject.account_id, HcsProject.id)
        .join(HcsAccount, HcsAccount.id == HcsProject.account_id)
        .where(HcsAccount.enabled.is_(True), HcsProject.enabled.is_(True)), HcsAccount.id, tenant_ids)).all())
    planned = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        selected = [s for s in scopes if s["task_code"] == t.code]
        project_runs = [s for s in selected if s["project_scope"] == "PROJECT"]
        global_scopes = [s for s in selected if s["project_scope"] == "GLOBAL"]
        current_scopes = project_runs or global_scopes or selected
        status = task_last_status(current_scopes)
        matching = [s for s in current_scopes if scope_execution_status(s) == status]
        representative = max(matching, key=lambda s: (s["last_at"], s["run"].id)) if matching else None
        rid = (representative["effective_run_id"] if representative["last_status"] in NEUTRAL
               and representative["effective_status"] == status else representative["run"].id) if representative else None
        planned.append((t, selected, status, representative, rid))
    ids = [rid for _, _, _, _, rid in planned if rid is not None]
    runs = {r.id: r for r in db.scalars(select(InspectionRun).where(InspectionRun.id.in_(ids)))} if ids else {}
    for t, selected, status, representative, rid in planned:
        run = runs.get(rid) if rid is not None else None
        project_runs = [s for s in selected if s["project_scope"] == "PROJECT"
                        and (s["tenant_id"], s["project_ref"]) in configured]
        global_scopes = [s for s in selected if s["project_scope"] == "GLOBAL"]
        counted = project_runs if t.code != "data_cleanup" else global_scopes
        covered = {(s["tenant_id"], s["project_ref"]) for s in project_runs}
        complete = bool(configured) and covered == configured if t.code != "data_cleanup" else bool(global_scopes)
        complete = complete and all(s["effective_status"] == "SUCCESS" and s["effective_finished_at"] is not None
                                    and s["effective_resource_total"] is not None
                                    and s["effective_abnormal_total"] is not None for s in counted)
        resource_total = sum(int(s["effective_resource_total"]) for s in counted) if complete else None
        abnormal_total = sum(int(s["effective_abnormal_total"]) for s in counted) if complete else None
        tasks.append({"code": t.code, "name": t.name, "service": t.service, "interval_sec": t.interval_sec,
                      "cron": t.cron,
                      "enabled": t.enabled, "last_status": status,
                      "last_run_at": run.finished_at or run.started_at if run else None,
                      "duration_sec": round((run.finished_at - run.started_at).total_seconds(), 1)
                      if run and run.finished_at else None,
                      "resource_total": resource_total, "abnormal_total": abnormal_total,
                      "scope_verified": bool(project_runs or global_scopes),
                      "count_basis": "LATEST_COMPLETE_KNOWN_PROJECTS" if complete and project_runs else
                                     "GLOBAL_RUN" if complete else "UNVERIFIED_PROJECT_COVERAGE",
                      "counts_complete": complete,
                      "history_unknown_scope_count": sum(s["project_scope"] == "UNKNOWN" for s in selected),
                      "expected_scope_count": len(configured) if t.code != "data_cleanup" else 1,
                      "counted_scopes": [{"tenant_id": s["tenant_id"], "project_ref": s["project_ref"],
                                          "run_id": s["effective_run_id"], "finished_at": s["effective_finished_at"]}
                                         for s in counted if s["effective_status"] == "SUCCESS"
                                         and s["effective_finished_at"] is not None],
                      "representative_scope": {"run_id": run.id, "tenant_id": run.tenant_id,
                                               "project_ref": run.project_ref,
                                               "project_scope": representative["project_scope"]}
                      if run and representative is not None else None,
                      "scopes": [{k: v for k, v in s.items() if k != "run"} for s in selected]})

    sev_count = Counter(a.severity for a in active)
    return {
        "health_score": platform,
        "kpis": {
            "resources": len(resources), "abnormal_resources": len(worst), "active_alarms": len(active),
            "assessed_resources": total, "unknown_resources": len(resources) - total,
            "firing": sum(1 for a in active if a.status == "FIRING"),
            **{s.lower(): sev_count.get(s, 0) for s in SEVS},
            "today_new": sum(1 for a in recent if a.first_seen >= day_start),
            "resolved_24h": sum(1 for a in recent if a.resolved_at is not None),
            "mtta_min": _minutes([a.ack_at - a.first_seen for a in recent if a.ack_at is not None]),
            "mttr_min": _minutes([a.resolved_at - a.first_seen for a in recent if a.resolved_at is not None]),
        },
        "groups": groups,
        "tenants": tenants,
        "ecs": _ecs(list(resources), visible),
        "pods": _pods(db, now, tenant_ids),
        "trend": {"hours": [h.strftime("%H:00") for h in hours], "series": series},
        "top_alarms": [AlarmOut.model_validate(a).model_dump(mode="json") for a in top],
        "capacity": {"gaussdb_disk": top5("gaussdb_instance", "disk_usage"),
                     "dcs_memory": top5("dcs_instance", "memory_usage"),
                     "ecs_disk": top5("ecs_server", "disk_util"), "ecs_cpu": top5("ecs_server", "cpu_util")},
        "tasks": tasks,
        "ledger": ledger_overview(db, now, tenant_ids),
        "updated_at": now,
    }
