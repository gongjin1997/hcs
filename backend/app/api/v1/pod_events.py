"""容器事件：CCE Pod 重启 / OOM / CrashLoopBackOff / 背压 / 驱逐 / 探针失败等事件的查询、统计、Pod 时间线，
背压探针配置，手动采集。

数据来源见 app/k8s/collector.py；全部接口按租户范围过滤（X-Tenant-Id）。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, not_found, ok
from app.k8s.catalog import EVENT_TYPES, LEVEL_LABEL, RESTART_TYPES, SOURCE_LABEL
from app.k8s.collector import TASK_CODE
from app.k8s.semantics import occurrence_interval, window_count
from app.ledger.fetchers import parse_time
from app.models import HcsAccount, PodEvent, PodProbe, PodState
from app.models.base import utcnow
from app.models.pod import SCOPE_META
from app.schemas.pod import PodEventOut, PodStateOut, ProbeIn, ProbeOut
from app.services import pipeline, platform_watch
from app.services.pod_freshness import freshness_ttl, snapshot_freshness, summarize_pods
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
        "duration_sec": duration, "detail": {k: v for k, v in (e.detail or {}).items() if k != SCOPE_META}})


def _identity_filters(q, model, tenant_id: int | None, project_ref: int | None, unknown_project: bool = False):
    if project_ref is not None and unknown_project:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时选择已知项目和历史未知项目")
    if tenant_id is not None:
        q = q.where(model.tenant_id == tenant_id)
    if project_ref is not None:
        q = q.where(model.project_ref == project_ref)
    if unknown_project:
        q = q.where(model.project_ref.is_(None))
    return q


def _events_query(scope: Scope, *, hours: int, cluster_id: str | None = None, namespace: str | None = None,
                  workload: str | None = None, pod: str | None = None, keyword: str | None = None,
                  event_type: list[str] | None = None, level: list[str] | None = None, source: str | None = None,
                  ongoing: bool | None = None, tenant_id: int | None = None, project_ref: int | None = None,
                  workload_kind: str | None = None, unknown_project: bool = False):
    since = utcnow() - timedelta(hours=hours)
    q = scope.apply(select(PodEvent), PodEvent.tenant_id).where(
        or_(PodEvent.last_time >= since, PodEvent.ongoing.is_(True)))
    if cluster_id:
        q = q.where(PodEvent.cluster_id == cluster_id)
    if namespace:
        q = q.where(PodEvent.namespace == namespace)
    if workload:
        q = q.where(PodEvent.workload == workload)
    if workload_kind:
        q = q.where(PodEvent.workload_kind == workload_kind)
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
    return _identity_filters(q, PodEvent, tenant_id, project_ref, unknown_project)


@router.get("", response_model=ApiResponse[Page[PodEventOut]], summary="容器事件列表", dependencies=[VIEW])
def list_events(cluster_id: str | None = None, namespace: str | None = None, workload: str | None = None,
                pod: str | None = None, keyword: str | None = None, event_type: list[str] | None = Query(None),
                level: list[str] | None = Query(None), source: str | None = None, ongoing: bool | None = None,
                hours: int = Query(24, ge=1, le=24 * 30), page: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=200), db: Session = Depends(get_db),
                scope: Scope = Depends(get_scope), tenant_id: int | None = Query(None, ge=1),
                project_ref: int | None = Query(None, ge=1),
                workload_kind: Annotated[str | None, Query(max_length=32)] = None, unknown_project: bool = False):
    q = _events_query(scope, hours=hours, cluster_id=cluster_id, namespace=namespace, workload=workload, pod=pod,
                      keyword=keyword, event_type=event_type, level=level, source=source, ongoing=ongoing,
                      tenant_id=tenant_id, project_ref=project_ref, workload_kind=workload_kind,
                      unknown_project=unknown_project)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(PodEvent.ongoing.desc(), PodEvent.last_time.desc(), PodEvent.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    tm, now = tenant_map(db), utcnow()
    return ok(Page(items=[_event_out(e, tm, now) for e in rows], total=total, page=page, page_size=page_size))


@router.get("/stats", summary="容器事件统计（按类型 / 趋势 / Top 工作负载 / Pod 概况）", dependencies=[VIEW])
def stats(hours: int = Query(24, ge=1, le=24 * 30), cluster_id: str | None = None, namespace: str | None = None,  # noqa: C901  # T10
          db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
          tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
          workload: str | None = None, workload_kind: Annotated[str | None, Query(max_length=32)] = None,
          unknown_project: bool = False):
    now = utcnow()
    q = _events_query(scope, hours=hours, cluster_id=cluster_id, namespace=namespace,
                      tenant_id=tenant_id, project_ref=project_ref, workload=workload, workload_kind=workload_kind,
                      unknown_project=unknown_project)
    rows = db.execute(q.with_only_columns(PodEvent.tenant_id, PodEvent.project_ref, PodEvent.cluster_id,
                                          PodEvent.cluster_name, PodEvent.namespace, PodEvent.workload_kind,
                                          PodEvent.workload, PodEvent.event_type, PodEvent.level, PodEvent.count.label("n"),
                                          PodEvent.first_time, PodEvent.last_time, PodEvent.ongoing,
                                          PodEvent.count, PodEvent.detail)
                      .execution_options(yield_per=500))
    by_type: Counter[str] = Counter()
    by_level: Counter[str] = Counter()
    restarts = 0
    restart_complete = True
    total = 0
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
    series: dict[str, list[int | None]] = {k: [0] * len(buckets) for k in ("restart", "backpressure", "other")}
    workloads: dict[tuple, dict] = {}
    for r in rows:
        total += 1
        by_type[r.event_type] += 1
        by_level[r.level] += 1
        n = window_count(r, now - timedelta(hours=hours), restarts=True) if r.event_type in RESTART_TYPES else 0
        if r.event_type in RESTART_TYPES:
            restart_complete &= n is not None
            restarts += n or 0
        key = "restart" if r.event_type in RESTART_TYPES else "backpressure" if r.event_type == "BACKPRESSURE" else "other"
        if key == "restart" and (r.detail or {}).get("restart_counted") is not False:
            intervals = (r.detail or {}).get("count_intervals") or [occurrence_interval(r.first_time, r.last_time, r.n)]
            for interval in intervals:
                first, last = parse_time(interval.get("first")), parse_time(interval.get("last"))
                if first is None or last is None or last < start or first > now:
                    continue
                lo = max(0, int((first - start) // step))
                hi = min(len(buckets) - 1, int((last - start) // step))
                for idx in range(lo, hi + 1):
                    if lo != hi or first < start:
                        series[key][idx] = None
                    elif series[key][idx] is not None:
                        series[key][idx] += interval["count"]
        elif key != "restart" and start <= r.first_time <= now:
            idx = min(int((r.first_time - start) / step), len(buckets) - 1)
            series[key][idx] = (series[key][idx] or 0) + 1
        if r.workload:
            w = workloads.setdefault((r.tenant_id, r.project_ref, r.cluster_id, r.namespace, r.workload_kind, r.workload), {
                "tenant_id": r.tenant_id, "project_ref": r.project_ref, "cluster_id": r.cluster_id,
                "cluster_name": r.cluster_name, "namespace": r.namespace,
                "workload": r.workload, "workload_kind": r.workload_kind,
                "events": 0, "restarts": 0, "restart_complete": True,
                "backpressure": 0, "critical": 0,
                "last_time": r.last_time, "types": Counter()})
            w["events"] += 1
            w["restarts"] += n or 0
            if r.event_type in RESTART_TYPES and n is None:
                w["restart_complete"] = False
            w["backpressure"] += 1 if r.event_type == "BACKPRESSURE" else 0
            w["critical"] += 1 if r.level == "CRITICAL" else 0
            w["last_time"] = max(w["last_time"], r.last_time)
            w["types"][r.event_type] += 1
    tm = tenant_map(db)
    top = sorted(workloads.values(), key=lambda w: (-w["critical"], -w["restarts"] - w["events"], w["workload"]))[:10]
    for w in top:
        if not w.pop("restart_complete"):
            w["restarts"] = None
        w["tenant_name"] = _tname(tm, w["tenant_id"])
        w["types"] = [{"type": k, "name": EVENT_TYPES[k]["name"], "count": v} for k, v in w["types"].most_common()]
    pq = scope.apply(select(PodState).where(PodState.deleted.is_(False)), PodState.tenant_id)
    pq = _identity_filters(pq, PodState, tenant_id, project_ref, unknown_project)
    if cluster_id:
        pq = pq.where(PodState.cluster_id == cluster_id)
    if namespace:
        pq = pq.where(PodState.namespace == namespace)
    if workload:
        pq = pq.where(PodState.workload == workload)
    if workload_kind:
        pq = pq.where(PodState.workload_kind == workload_kind)
    pods = db.scalars(pq).all()
    pod_summary = summarize_pods(db, list(pods), now)
    for group in pod_summary["scopes"]:
        group["tenant_name"] = _tname(tm, group["tenant_id"])
    ongoing = db.scalar(select(func.count()).select_from(
        _events_query(scope, hours=24 * 30, cluster_id=cluster_id, namespace=namespace, ongoing=True,
                      tenant_id=tenant_id, project_ref=project_ref, workload=workload, workload_kind=workload_kind,
                      unknown_project=unknown_project).subquery())) or 0
    return ok({
        "hours": hours, "total": total, "restarts": restarts if restart_complete else None,
        "restart_count_complete": restart_complete, "ongoing": ongoing,
        "critical": by_level.get("CRITICAL", 0),
        "by_type": [{"type": k, "name": v["name"], "color": v["color"], "level": v["level"], "count": by_type.get(k, 0)}
                    for k, v in EVENT_TYPES.items()],
        "by_level": [{"level": k, "name": v, "count": by_level.get(k, 0)} for k, v in LEVEL_LABEL.items()],
        "trend": {"labels": [b.strftime(fmt) for b in buckets], "series": series, "step": "hour" if hours <= 48 else "day"},
        "top_workloads": top,
        "pods": pod_summary,
    })


@router.get("/options", summary="筛选项：集群 / 命名空间 / 工作负载 / 事件类型", dependencies=[VIEW])
def options(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
            tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
            unknown_project: bool = False):
    tm = tenant_map(db)
    q = scope.apply(select(PodState.tenant_id, PodState.project_ref, PodState.cluster_id, PodState.cluster_name,
                                         PodState.namespace, PodState.workload_kind, PodState.workload),
                    PodState.tenant_id)
    pods = db.execute(_identity_filters(q, PodState, tenant_id, project_ref, unknown_project).distinct()).all()
    clusters: dict[tuple, dict] = {}
    namespaces: dict[str, set[str]] = defaultdict(set)
    workloads: dict[tuple, dict] = {}
    for p in pods:
        cluster = clusters.setdefault((p.tenant_id, p.project_ref, p.cluster_id), {
                                           "cluster_id": p.cluster_id, "cluster_name": p.cluster_name,
                                           "project_ref": p.project_ref,
                                           "tenant_id": p.tenant_id, "tenant_name": _tname(tm, p.tenant_id),
                                           "namespaces": set()})
        cluster["namespaces"].add(p.namespace)
        namespaces[p.cluster_id].add(p.namespace)
        if p.workload:
            workloads.setdefault((p.tenant_id, p.project_ref, p.cluster_id, p.namespace, p.workload_kind, p.workload), {
                "tenant_id": p.tenant_id, "project_ref": p.project_ref, "cluster_id": p.cluster_id,
                "namespace": p.namespace, "kind": p.workload_kind, "name": p.workload})
    for cluster in clusters.values():
        cluster["namespaces"] = sorted(cluster["namespaces"])
    return ok({
        "clusters": sorted(clusters.values(), key=lambda c: (c["tenant_id"] or 0, c["cluster_name"])),
        "namespaces": sorted({n for v in namespaces.values() for n in v}),
        "namespaces_by_cluster": {k: sorted(v) for k, v in namespaces.items()},
        "workloads": sorted(workloads.values(), key=lambda w: (w["namespace"], w["name"])),
        "event_types": [{"code": k, **v} for k, v in EVENT_TYPES.items()],
        "levels": [{"code": k, "name": v} for k, v in LEVEL_LABEL.items()],
        "sources": [{"code": k, "name": v} for k, v in SOURCE_LABEL.items()],
    })


@router.get("/pod", summary="Pod 时间线（event_id 精确定位；旧 key 歧义返回 409）", dependencies=[VIEW])
def pod_timeline(key: str, days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db),
                 scope: Scope = Depends(get_scope), event_id: int | None = Query(None, ge=1),
                 tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1)):
    anchor = None
    if event_id is not None:
        anchor = db.scalar(scope.apply(select(PodEvent).where(PodEvent.id == event_id), PodEvent.tenant_id))
        if (anchor is None or not anchor.pod or key != f"{anchor.cluster_id}/{anchor.namespace}/{anchor.pod}"
                or (tenant_id is not None and tenant_id != anchor.tenant_id)
                or (project_ref is not None and project_ref != anchor.project_ref)):
            raise not_found("Pod")
        cid, ns, name = anchor.cluster_id, anchor.namespace, anchor.pod
        identity = (anchor.tenant_id, anchor.project_ref)
    else:
        parts = key.split("/")
        if len(parts) != 3 or not all(parts):
            raise BizError(ErrorCode.BAD_REQUEST, "key 格式应为 集群ID/命名空间/Pod 名")
        cid, ns, name = parts
        identities: set[tuple[int | None, int | None]] = set()
        for model in (PodState, PodEvent):
            candidates = scope.apply(select(model.tenant_id, model.project_ref).where(
                model.cluster_id == cid, model.namespace == ns, model.pod == name), model.tenant_id)
            candidates = _identity_filters(candidates, model, tenant_id, project_ref)
            identities.update((row[0], row[1]) for row in db.execute(candidates.distinct().limit(2)))
            if len(identities) > 1:
                raise BizError(ErrorCode.CONFLICT, "同名 Pod 存在多个租户或项目，请从事件记录打开或指定精确范围", 409)
        if not identities:
            raise not_found("Pod")
        identity = next(iter(identities))

    state_query = select(PodState).where(PodState.tenant_id == identity[0], PodState.project_ref == identity[1],
                                         PodState.cluster_id == cid, PodState.namespace == ns, PodState.pod == name)
    states = db.scalars(state_query.limit(2)).all()
    if len(states) > 1:
        raise BizError(ErrorCode.CONFLICT, "同一 Pod 存在重复状态记录，请先检查历史数据", 409)
    st = states[0] if states else None
    anchor_uid = (anchor.detail or {}).get("pod_uid") if anchor is not None else None
    if st is not None and anchor_uid and (st.data or {}).get("pod_uid") != anchor_uid:
        st = None
    tm, now = tenant_map(db), utcnow()
    q = select(PodEvent).where(PodEvent.tenant_id == identity[0], PodEvent.project_ref == identity[1],
        PodEvent.cluster_id == cid, PodEvent.namespace == ns, PodEvent.pod == name,
        or_(PodEvent.last_time >= now - timedelta(days=days), PodEvent.ongoing.is_(True)))
    if anchor_uid:
        q = q.where(PodEvent.detail["pod_uid"].as_string() == anchor_uid)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    events = db.scalars(q.order_by(PodEvent.first_time.desc(), PodEvent.id.desc()).limit(500)).all()
    if st is None and not events and anchor is None:
        raise not_found("Pod")
    state = PodStateOut.model_validate(st).model_copy(update={
        "tenant_name": _tname(tm, st.tenant_id), "key": key,
        "freshness": snapshot_freshness(st, now, freshness_ttl(db, now))}) if st else None
    restart_counts = [window_count(e, now - timedelta(days=days), restarts=True) for e in db.scalars(
        q.where(PodEvent.event_type.in_(RESTART_TYPES)).execution_options(yield_per=500))]
    restarts = None if any(n is None for n in restart_counts) else sum(n for n in restart_counts if n is not None)
    return ok({"state": state, "events": [_event_out(e, tm, now) for e in events], "total": total,
               "truncated": total > len(events), "restarts_in_window": restarts})


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
    items = [{"run_id": r.id, "tenant_id": r.tenant_id, "project_ref": r.project_ref,
              "tenant_name": _tname(tm, r.tenant_id), "status": r.status,
              "resource_total": r.resource_total, "abnormal_total": r.abnormal_total,
              "events": (r.stats or {}).get("events_created", 0), "error_summary": r.error_summary} for r in runs]
    failed = [i for i in items if i["status"] in ("FAILED", "TIMEOUT")]
    outcome = platform_watch.run_outcome(runs)
    audit.log("podevent.collect", target_type="task", target_id=TASK_CODE, target_name="CCE 容器事件采集",
              tenant_id=scope.single, result="SUCCESS" if outcome == "SUCCESS" else "FAILED",
              detail={"outcome": outcome, "runs": [{"tenant": i["tenant_name"], "project_ref": i["project_ref"],
                      "status": i["status"]} for i in items]})
    if outcome == "FAILED":
        raise BizError(ErrorCode.HCS_UPSTREAM, "采集失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['error_summary']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "status": outcome})
