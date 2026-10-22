"""容器事件：CCE Pod 重启 / OOM / CrashLoopBackOff / 背压 / 驱逐 / 探针失败等事件的查询、统计、Pod 时间线，
背压探针配置，手动采集。

数据来源见 app/k8s/collector.py；全部接口按租户范围过滤（X-Tenant-Id）。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, not_found, ok
from app.k8s.catalog import EVENT_TYPES, LEVEL_LABEL, RESTART_TYPES, SOURCE_LABEL
from app.k8s.collector import TASK_CODE
from app.models import HcsAccount, PodEvent, PodProbe, PodState
from app.models.base import utcnow
from app.schemas.pod import PodEventOut, PodStateOut, ProbeIn, ProbeOut
from app.services import pipeline
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/pod-events", tags=["容器事件"])
VIEW = Depends(require_perm("podevent:view"))
LEVEL_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def _tname(tm: dict[int, HcsAccount], tid: int | None) -> str | None:
    t = tm.get(tid) if tid is not None else None
    return (t.short_name or t.name) if t else None


def _event_out(e: PodEvent, tm: dict[int, HcsAccount], now: datetime) -> PodEventOut:
    meta = EVENT_TYPES.get(e.event_type, EVENT_TYPES["WARNING"])
    end = e.end_time or (now if e.ongoing else None)
    duration = int((end - e.first_time).total_seconds()) if end and meta.get("ongoing") else None
    return PodEventOut.model_validate(e).model_copy(update={
        "tenant_name": _tname(tm, e.tenant_id), "type_name": meta["name"], "color": meta["color"],
        "duration_sec": duration})


def _events_query(scope: Scope, *, hours: int, cluster_id: str | None = None, namespace: str | None = None,
                  workload: str | None = None, pod: str | None = None, keyword: str | None = None,
                  event_type: list[str] | None = None, level: list[str] | None = None, source: str | None = None,
                  ongoing: bool | None = None):
    since = utcnow() - timedelta(hours=hours)
    q = scope.apply(select(PodEvent), PodEvent.tenant_id).where(
        or_(PodEvent.last_time >= since, PodEvent.ongoing.is_(True)))
    if cluster_id:
        q = q.where(PodEvent.cluster_id == cluster_id)
    if namespace:
        q = q.where(PodEvent.namespace == namespace)
    if workload:
        q = q.where(PodEvent.workload == workload)
    if pod:
        q = q.where(PodEvent.pod == pod)
    if event_type:
        q = q.where(PodEvent.event_type.in_(event_type))
    if level:
        q = q.where(PodEvent.level.in_(level))
    if source:
        q = q.where(PodEvent.source == source)
    if ongoing is not None:
        q = q.where(PodEvent.ongoing.is_(ongoing))
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(or_(PodEvent.pod.ilike(like), PodEvent.workload.ilike(like), PodEvent.message.ilike(like),
                        PodEvent.reason.ilike(like), PodEvent.node.ilike(like), PodEvent.container.ilike(like)))
    return q


@router.get("", response_model=ApiResponse[Page[PodEventOut]], summary="容器事件列表", dependencies=[VIEW])
def list_events(cluster_id: str | None = None, namespace: str | None = None, workload: str | None = None,
                pod: str | None = None, keyword: str | None = None, event_type: list[str] | None = Query(None),
                level: list[str] | None = Query(None), source: str | None = None, ongoing: bool | None = None,
                hours: int = Query(24, ge=1, le=24 * 30), page: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=200), db: Session = Depends(get_db),
                scope: Scope = Depends(get_scope)):
    q = _events_query(scope, hours=hours, cluster_id=cluster_id, namespace=namespace, workload=workload, pod=pod,
                      keyword=keyword, event_type=event_type, level=level, source=source, ongoing=ongoing)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(PodEvent.ongoing.desc(), PodEvent.last_time.desc(), PodEvent.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    tm, now = tenant_map(db), utcnow()
    return ok(Page(items=[_event_out(e, tm, now) for e in rows], total=total, page=page, page_size=page_size))


@router.get("/stats", summary="容器事件统计（按类型 / 趋势 / Top 工作负载 / Pod 概况）", dependencies=[VIEW])
def stats(hours: int = Query(24, ge=1, le=24 * 30), cluster_id: str | None = None, namespace: str | None = None,
          db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    now = utcnow()
    q = _events_query(scope, hours=hours, cluster_id=cluster_id, namespace=namespace)
    rows = db.execute(q.with_only_columns(PodEvent.tenant_id, PodEvent.cluster_name, PodEvent.namespace,
                                          PodEvent.workload, PodEvent.event_type, PodEvent.level, PodEvent.count.label("n"),
                                          PodEvent.first_time, PodEvent.last_time, PodEvent.ongoing)
                      .limit(50000)).all()
    by_type: Counter[str] = Counter()
    by_level: Counter[str] = Counter()
    restarts = 0
    # 趋势：48 小时以内按小时，否则按天；重启按次数（count），其他按事件条数
    step = timedelta(hours=1) if hours <= 48 else timedelta(days=1)
    fmt = "%m-%d %H:00" if hours <= 48 else "%m-%d"
    start = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    if step == timedelta(days=1):
        start = start.replace(hour=0)
    buckets: list[datetime] = []
    t = start
    while t <= now:
        buckets.append(t)
        t += step
    series: dict[str, list[int]] = {k: [0] * len(buckets) for k in ("restart", "backpressure", "other")}
    workloads: dict[tuple, dict] = {}
    for r in rows:
        by_type[r.event_type] += 1
        by_level[r.level] += 1
        n = int(r.n or 1)
        if r.event_type in RESTART_TYPES:
            restarts += n
        at = max(r.first_time, start)
        idx = min(int((at - start) / step), len(buckets) - 1)
        key = "restart" if r.event_type in RESTART_TYPES else "backpressure" if r.event_type == "BACKPRESSURE" else "other"
        series[key][idx] += n if key == "restart" else 1
        if r.workload:
            w = workloads.setdefault((r.tenant_id, r.cluster_name, r.namespace, r.workload), {
                "tenant_id": r.tenant_id, "cluster_name": r.cluster_name, "namespace": r.namespace,
                "workload": r.workload, "events": 0, "restarts": 0, "backpressure": 0, "critical": 0,
                "last_time": r.last_time, "types": Counter()})
            w["events"] += 1
            w["restarts"] += n if r.event_type in RESTART_TYPES else 0
            w["backpressure"] += 1 if r.event_type == "BACKPRESSURE" else 0
            w["critical"] += 1 if r.level == "CRITICAL" else 0
            w["last_time"] = max(w["last_time"], r.last_time)
            w["types"][r.event_type] += 1
    tm = tenant_map(db)
    top = sorted(workloads.values(), key=lambda w: (-w["critical"], -w["restarts"] - w["events"], w["workload"]))[:10]
    for w in top:
        w["tenant_name"] = _tname(tm, w["tenant_id"])
        w["types"] = [{"type": k, "name": EVENT_TYPES[k]["name"], "count": v} for k, v in w["types"].most_common()]
    pq = scope.apply(select(PodState).where(PodState.deleted.is_(False)), PodState.tenant_id)
    if cluster_id:
        pq = pq.where(PodState.cluster_id == cluster_id)
    if namespace:
        pq = pq.where(PodState.namespace == namespace)
    pods = db.scalars(pq).all()
    ongoing = db.scalar(select(func.count()).select_from(
        _events_query(scope, hours=24 * 30, cluster_id=cluster_id, namespace=namespace, ongoing=True).subquery())) or 0
    return ok({
        "hours": hours, "total": len(rows), "restarts": restarts, "ongoing": ongoing,
        "critical": by_level.get("CRITICAL", 0),
        "by_type": [{"type": k, "name": v["name"], "color": v["color"], "level": v["level"], "count": by_type.get(k, 0)}
                    for k, v in EVENT_TYPES.items()],
        "by_level": [{"level": k, "name": v, "count": by_level.get(k, 0)} for k, v in LEVEL_LABEL.items()],
        "trend": {"labels": [b.strftime(fmt) for b in buckets], "series": series, "step": "hour" if hours <= 48 else "day"},
        "top_workloads": top,
        "pods": {"total": len(pods), "ready": sum(1 for p in pods if p.ready),
                 "not_ready": sum(1 for p in pods if not p.ready and p.phase != "Succeeded"),
                 "crashloop": sum(1 for p in pods if p.status_reason == "CrashLoopBackOff"),
                 "restarts": sum(p.restarts for p in pods),
                 "clusters": len({p.cluster_id for p in pods})},
    })


@router.get("/options", summary="筛选项：集群 / 命名空间 / 工作负载 / 事件类型", dependencies=[VIEW])
def options(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    tm = tenant_map(db)
    pods = db.execute(scope.apply(select(PodState.tenant_id, PodState.cluster_id, PodState.cluster_name,
                                         PodState.namespace, PodState.workload_kind, PodState.workload),
                                  PodState.tenant_id).distinct()).all()
    clusters: dict[str, dict] = {}
    namespaces: dict[str, set[str]] = defaultdict(set)
    workloads: dict[tuple, dict] = {}
    for p in pods:
        clusters.setdefault(p.cluster_id, {"cluster_id": p.cluster_id, "cluster_name": p.cluster_name,
                                           "tenant_id": p.tenant_id, "tenant_name": _tname(tm, p.tenant_id)})
        namespaces[p.cluster_id].add(p.namespace)
        if p.workload:
            workloads.setdefault((p.cluster_id, p.namespace, p.workload), {
                "cluster_id": p.cluster_id, "namespace": p.namespace, "kind": p.workload_kind, "name": p.workload})
    return ok({
        "clusters": sorted(clusters.values(), key=lambda c: (c["tenant_id"] or 0, c["cluster_name"])),
        "namespaces": sorted({n for v in namespaces.values() for n in v}),
        "namespaces_by_cluster": {k: sorted(v) for k, v in namespaces.items()},
        "workloads": sorted(workloads.values(), key=lambda w: (w["namespace"], w["name"])),
        "event_types": [{"code": k, **v} for k, v in EVENT_TYPES.items()],
        "levels": [{"code": k, "name": v} for k, v in LEVEL_LABEL.items()],
        "sources": [{"code": k, "name": v} for k, v in SOURCE_LABEL.items()],
    })


@router.get("/pod", summary="Pod 时间线：最新状态 + 事件（key = 集群ID/命名空间/Pod 名）", dependencies=[VIEW])
def pod_timeline(key: str, days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db),
                 scope: Scope = Depends(get_scope)):
    parts = key.split("/", 2)
    if len(parts) != 3:
        raise BizError(ErrorCode.BAD_REQUEST, "key 格式应为 集群ID/命名空间/Pod 名")
    cid, ns, name = parts
    st = db.get(PodState, key)
    tm, now = tenant_map(db), utcnow()
    q = scope.apply(select(PodEvent), PodEvent.tenant_id).where(
        PodEvent.cluster_id == cid, PodEvent.namespace == ns, PodEvent.pod == name,
        or_(PodEvent.last_time >= now - timedelta(days=days), PodEvent.ongoing.is_(True)))
    events = db.scalars(q.order_by(PodEvent.first_time.desc()).limit(500)).all()
    if st is not None:
        scope.ensure(st.tenant_id, "Pod")
    elif not events:
        raise not_found("Pod")
    state = PodStateOut.model_validate(st).model_copy(update={"tenant_name": _tname(tm, st.tenant_id)}) if st else None
    return ok({"state": state, "events": [_event_out(e, tm, now) for e in events],
               "restarts_in_window": sum(e.count for e in events if e.event_type in RESTART_TYPES)})


# ---------------- 背压探针 ----------------
def _probe_out(p: PodProbe, tm: dict[int, HcsAccount]) -> ProbeOut:
    return ProbeOut.model_validate(p).model_copy(update={"tenant_name": _tname(tm, p.tenant_id)})


def _get_probe(db: Session, scope: Scope, pid: int) -> PodProbe:
    p = db.get(PodProbe, pid)
    if p is None:
        raise not_found("背压探针")
    scope.ensure(p.tenant_id, "背压探针")
    return p


def _probe_tenant(db: Session, scope: Scope, tenant_id: int | None) -> int:
    tid = tenant_id if tenant_id is not None else scope.single
    if tid is None:
        ids = scope.tenant_ids if scope.tenant_ids is not None else [t.id for t in tenant_map(db).values()]
        if len(ids) != 1:
            raise BizError(ErrorCode.BAD_REQUEST, "请选择探针所属租户")
        tid = ids[0]
    if db.get(HcsAccount, tid) is None:
        raise not_found("租户")
    if not scope.allows(tid):
        raise BizError(ErrorCode.FORBIDDEN, "无权为该租户配置探针", 403)
    return tid


@router.get("/probes", response_model=ApiResponse[list[ProbeOut]], summary="背压探针列表", dependencies=[VIEW])
def list_probes(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    tm = tenant_map(db)
    rows = db.scalars(scope.apply(select(PodProbe), PodProbe.tenant_id).order_by(PodProbe.tenant_id, PodProbe.id))
    return ok([_probe_out(p, tm) for p in rows])


@router.post("/probes", response_model=ApiResponse[ProbeOut], summary="新增背压探针")
def create_probe(body: ProbeIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 audit: Auditor = Depends(get_auditor), user: CurrentUser = Depends(require_perm("podevent:config"))):
    tid = _probe_tenant(db, scope, body.tenant_id)
    p = PodProbe(**body.model_dump(exclude={"tenant_id"}), tenant_id=tid, updated_by=user.display_name)
    db.add(p)
    db.flush()
    audit.log("probe.save", target_type="probe", target_id=p.id, target_name=p.name, tenant_id=tid,
              detail={"op": "create", **body.model_dump(exclude={"tenant_id"})})
    return ok(_probe_out(p, tenant_map(db)))


@router.put("/probes/{pid}", response_model=ApiResponse[ProbeOut], summary="修改背压探针")
def update_probe(pid: int, body: ProbeIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 audit: Auditor = Depends(get_auditor), user: CurrentUser = Depends(require_perm("podevent:config"))):
    p = _get_probe(db, scope, pid)
    data = body.model_dump(exclude={"tenant_id"})
    if body.tenant_id is not None and body.tenant_id != p.tenant_id:
        p.tenant_id = _probe_tenant(db, scope, body.tenant_id)
    before = {k: getattr(p, k) for k in data}
    for k, v in data.items():
        setattr(p, k, v)
    p.updated_by = user.display_name
    changed = {k: [before[k], v] for k, v in data.items() if before[k] != v}
    audit.log("probe.save", target_type="probe", target_id=p.id, target_name=p.name, tenant_id=p.tenant_id,
              detail={"op": "update", "changed": changed})
    return ok(_probe_out(p, tenant_map(db)))


@router.delete("/probes/{pid}", summary="删除背压探针")
def delete_probe(pid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 audit: Auditor = Depends(get_auditor), _: CurrentUser = Depends(require_perm("podevent:config"))):
    p = _get_probe(db, scope, pid)
    name, tid = p.name, p.tenant_id
    db.delete(p)
    audit.log("probe.delete", target_type="probe", target_id=pid, target_name=name, tenant_id=tid)
    return ok()


@router.post("/collect", summary="立即采集一次容器事件（按当前租户范围）")
def collect_now(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                audit: Auditor = Depends(get_auditor), _: CurrentUser = Depends(require_perm("podevent:config"))):
    runs = pipeline.run_all(db, TASK_CODE, trigger="MANUAL", tenant_ids=scope.tenant_ids)
    tm = tenant_map(db)
    items = [{"run_id": r.id, "tenant_id": r.tenant_id, "tenant_name": _tname(tm, r.tenant_id), "status": r.status,
              "resource_total": r.resource_total, "abnormal_total": r.abnormal_total,
              "events": (r.stats or {}).get("events_created", 0), "error_summary": r.error_summary} for r in runs]
    failed = [i for i in items if i["status"] == "FAILED"]
    all_failed = bool(runs) and len(failed) == len(runs)
    audit.log("podevent.collect", target_type="task", target_id=TASK_CODE, target_name="CCE 容器事件采集",
              tenant_id=scope.single, result="FAILED" if all_failed else "SUCCESS",
              detail={"runs": [{"tenant": i["tenant_name"], "status": i["status"]} for i in items]})
    if all_failed:
        raise BizError(ErrorCode.HCS_UPSTREAM, "采集失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['error_summary']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "status": "PARTIAL" if failed else "SUCCESS"})
