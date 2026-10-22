"""CCE 容器事件采集（任务 cce_pod_events，默认每 60 秒）：

1. Pod 状态比对：restartCount 增加 → 容器重启（上次退出原因为 OOMKilled 时记为 OOM 终止）；
   waiting.reason = CrashLoopBackOff → 持续型事件，恢复 Running 后结束；status.reason = Evicted → 驱逐
2. K8s Event：健康检查失败、调度失败、镜像拉取失败、节点压力、扩缩容、其他 Warning；reason / message 命中背压关键字时记为背压
3. 背压探针（可选）：经 apiserver 代理抓取 Pod /metrics 中的一个指标，持续 N 分钟超过阈值记为背压开始，连续 M 分钟恢复记为结束
4. 按工作负载汇总成巡检观测（resource_type=cce_workload），交给规则引擎产生告警（R-CCE-POD-*）

首次看到某个 Pod 时不补记历史重启，只回填 1 小时内的最近一次（避免把几个月前的重启当成新事件）。
"""
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext
from app.core.config import get_settings
from app.hcs import HcsError
from app.ledger.fetchers import parse_time
from app.models import PodEvent, PodProbe, PodState, Resource
from app.models.base import utcnow
from app.services import settings_service

from .catalog import EVENT_TYPES, RESTART_TYPES, classify, match_selector, workload_from_name, workload_of
from .kube import KubeApi, aggregate, parse_metric

log = logging.getLogger(__name__)
TASK_CODE = "cce_pod_events"
RESOURCE_TYPE = "cce_workload"
BACKFILL = timedelta(hours=1)
TOLERANCE = timedelta(seconds=20)            # 采集周期抖动容差
OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.2f}"


def _containers(pod: dict) -> list[dict]:
    limits = {c.get("name"): ((c.get("resources") or {}).get("limits") or {}).get("memory")
              for c in (pod.get("spec") or {}).get("containers") or []}
    out = []
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        state = cs.get("state") or {}
        st_name = next(iter(state), "")
        st = state.get(st_name) or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        out.append({"name": cs.get("name", ""), "image": cs.get("image"), "restart_count": int(cs.get("restartCount") or 0),
                    "ready": bool(cs.get("ready")), "state": st_name, "reason": st.get("reason"),
                    "started_at": st.get("startedAt"), "last_reason": last.get("reason"),
                    "last_exit_code": last.get("exitCode"), "last_started": last.get("startedAt"),
                    "last_finished": last.get("finishedAt"), "memory_limit": limits.get(cs.get("name"))})
    return out


def _container_of(field_path: str | None) -> str:
    m = re.search(r"\{([^}]+)\}", field_path or "")
    return m.group(1) if m else ""


class PodEventCollector:
    code, service, name = TASK_CODE, "cce", "CCE 容器事件采集"

    def collect(self, ctx: RunContext) -> list[Observation]:
        if ctx.db is None:
            raise RuntimeError("容器事件采集需要数据库会话（RunContext.db）")
        run = _Run(ctx, utcnow())
        clusters = ctx.client.get("cce", "/api/v3/projects/{project_id}/clusters").get("items") or []
        obs: list[Observation] = []
        for c in clusters:
            meta = c.get("metadata") or {}
            if (c.get("status") or {}).get("phase", "Available") != "Available":
                continue
            try:
                obs.extend(run.cluster(meta["uid"], meta.get("name", meta["uid"])))
            except HcsError as e:
                ctx.errors.append(f"集群 {meta.get('name')}: {e}")
        run.finish()
        return obs


class _Run:
    def __init__(self, ctx: RunContext, now: datetime):
        s = get_settings()
        self.ctx, self.now = ctx, now
        self.db_: Session = ctx.db
        self.bp = re.compile(s.pod_backpressure_pattern)
        self.recover = timedelta(minutes=s.pod_backpressure_recover_min)
        # 保留天数以“系统设置 → 数据保留”为准（未设置时取环境变量 INSPECTOR_POD_EVENT_RETENTION_DAYS）
        self.retention = timedelta(days=settings_service.get_retention(ctx.db).pod_event_days) if ctx.db is not None \
            else timedelta(days=s.pod_event_retention_days)
        self.exclude = set(ctx.params.get("exclude_namespaces", ["kube-public", "kube-node-lease"]))
        self.probes = list(self.db_.scalars(select(PodProbe).where(
            PodProbe.tenant_id == ctx.tenant_id, PodProbe.enabled.is_(True)))) if ctx.tenant_id is not None else []
        self.probe_results: dict[int, dict] = {}
        self.ongoing: dict[str, PodEvent] = {}
        self.created = 0

    # ---------------- 事件写入 ----------------
    def _new(self, st: PodState | None, *, cluster: tuple[str, str], event_type: str, source: str, dedup: str,
             first: datetime, last: datetime | None = None, **kw) -> PodEvent:
        ev = PodEvent(tenant_id=self.ctx.tenant_id, cluster_id=cluster[0], cluster_name=cluster[1],
                      namespace=st.namespace if st else kw.pop("namespace", ""),
                      workload_kind=st.workload_kind if st else kw.pop("workload_kind", ""),
                      workload=st.workload if st else kw.pop("workload", ""), pod=st.pod if st else kw.pop("pod", ""),
                      node=st.node if st else kw.pop("node", None), event_type=event_type,
                      level=EVENT_TYPES[event_type]["level"], source=source, dedup_key=dedup, first_time=first,
                      last_time=last or first, **kw)
        self.db_.add(ev)
        self.created += 1
        return ev

    def _ongoing(self, st: PodState, cluster: tuple[str, str], dedup: str, event_type: str, *, first: datetime,
                 reason: str, message: str, count: int = 1, container: str = "", source: str = "POD_STATUS",
                 peak: float | None = None, detail: dict | None = None) -> PodEvent:
        ev = self.ongoing.get(dedup)
        if ev is None:
            ev = self._new(st, cluster=cluster, event_type=event_type, source=source, dedup=dedup, first=first,
                           last=self.now, container=container, reason=reason, message=message, count=count,
                           ongoing=True, peak=peak, detail=detail or {})
            self.ongoing[dedup] = ev
            return ev
        ev.last_time, ev.message, ev.count = self.now, message, count
        if peak is not None:
            ev.peak = max(ev.peak or peak, peak)
        if detail:
            ev.detail = {**(ev.detail or {}), **detail}
        return ev

    def _close(self, dedup: str, at: datetime | None = None) -> None:
        ev = self.ongoing.pop(dedup, None)
        if ev is not None:
            ev.ongoing, ev.end_time = False, at or self.now

    # ---------------- 单个集群 ----------------
    def cluster(self, cid: str, cname: str) -> list[Observation]:
        db, now = self.db_, self.now
        kube = KubeApi(self.ctx.client, cid)
        pods = [p for p in kube.pods() if (p.get("metadata") or {}).get("namespace") not in self.exclude]
        events = kube.events()
        cl = (cid, cname)
        states = {s.key: s for s in db.scalars(select(PodState).where(PodState.cluster_id == cid))}
        self.ongoing.update({e.dedup_key: e for e in db.scalars(select(PodEvent).where(
            PodEvent.cluster_id == cid, PodEvent.ongoing.is_(True)))})
        known_restarts = set(db.scalars(select(PodEvent.dedup_key).where(
            PodEvent.cluster_id == cid, PodEvent.source == "POD_STATUS", PodEvent.last_time >= now - timedelta(days=2))))

        seen: set[str] = set()
        for p in pods:
            meta, status, spec = p.get("metadata") or {}, p.get("status") or {}, p.get("spec") or {}
            ns, pname = meta.get("namespace", ""), meta.get("name", "")
            key = f"{cid}/{ns}/{pname}"
            seen.add(key)
            st = states.get(key)
            first_seen = st is None
            if st is None:
                st = PodState(key=key, cluster_id=cid, namespace=ns, pod=pname, first_seen=now, data={}, containers=[])
                db.add(st)
                states[key] = st
            prev = {c["name"]: int(c.get("restart_count") or 0) for c in (st.containers or [])}
            kind, wl = workload_of(p)
            st.tenant_id, st.cluster_name, st.workload_kind, st.workload = self.ctx.tenant_id, cname, kind, wl
            st.node, st.pod_ip = spec.get("nodeName"), status.get("podIP")
            containers = _containers(p)
            for c in containers:
                rc = c["restart_count"]
                fin = parse_time(c["last_finished"])
                if first_seen:
                    if rc > 0 and fin and fin >= now - BACKFILL:
                        self._restart(st, cl, c, rc, 1, fin, known_restarts)
                elif c["name"] in prev and rc > prev[c["name"]]:
                    self._restart(st, cl, c, rc, rc - prev[c["name"]], fin or now, known_restarts)
                crash = f"crash:{key}/{c['name']}"
                if c["state"] == "waiting" and c["reason"] == "CrashLoopBackOff":
                    self._ongoing(st, cl, crash, "CRASH_LOOP", first=fin or now, reason="CrashLoopBackOff",
                                  container=c["name"], count=rc,
                                  message=f"容器 {c['name']} 处于 CrashLoopBackOff（累计重启 {rc} 次，上次退出："
                                          f"{c['last_reason'] or '未知'}，exitCode={c['last_exit_code']}）")
                elif c["state"] == "running":
                    self._close(crash)
            if status.get("reason") == "Evicted" and f"evicted:{key}" not in known_restarts:
                self._new(st, cluster=cl, event_type="EVICTED", source="POD_STATUS", dedup=f"evicted:{key}",
                          first=now, reason="Evicted", message=status.get("message") or "Pod 被驱逐")
                known_restarts.add(f"evicted:{key}")
            waiting = next((c["reason"] for c in containers if c["state"] == "waiting" and c["reason"]), None)
            st.phase, st.status_reason = status.get("phase", ""), status.get("reason") or waiting
            st.ready = bool(containers) and all(c["ready"] for c in containers)
            st.restarts = sum(c["restart_count"] for c in containers)
            st.started_at = parse_time(status.get("startTime"))
            st.labels, st.containers, st.deleted, st.last_seen = meta.get("labels") or {}, containers, False, now
        for key, st in states.items():
            if key not in seen and not st.deleted:
                st.deleted = True
                for dedup in [d for d, e in self.ongoing.items() if e.cluster_id == cid and e.namespace == st.namespace
                              and e.pod == st.pod]:
                    self._close(dedup)

        self._k8s_events(cl, events, states)
        self._probes(kube, cl, pods, states)
        db.flush()
        return self._observations(cl, states)

    def _restart(self, st: PodState, cl: tuple[str, str], c: dict, rc: int, delta: int, at: datetime,
                 known: set[str]) -> None:
        dedup = f"restart:{st.key}/{c['name']}/{rc}"
        if dedup in known:
            return
        known.add(dedup)
        oom = c["last_reason"] == "OOMKilled"
        msg = (f"容器 {c['name']} 内存超限被 OOM Kill（limit {c['memory_limit'] or '-'}），已自动重启（累计 {rc} 次）" if oom
               else f"容器 {c['name']} 重启（累计 {rc} 次），上次退出：{c['last_reason'] or '未知'}，"
                    f"exitCode={c['last_exit_code']}")
        self._new(st, cluster=cl, event_type="OOM_KILLED" if oom else "RESTART", source="POD_STATUS", dedup=dedup,
                  first=at, container=c["name"], reason=c["last_reason"] or "Restarted", message=msg, count=delta,
                  detail={"restart_count": rc, "exit_code": c["last_exit_code"], "image": c["image"],
                          "last_started": c["last_started"], "last_finished": c["last_finished"]})

    def _k8s_events(self, cl: tuple[str, str], events: list[dict], states: dict[str, PodState]) -> None:
        db, now, cid = self.db_, self.now, cl[0]
        wanted = []
        for e in events:
            et = classify(e.get("reason") or "", e.get("type") or "Normal", e.get("message") or "", self.bp)
            ns = (e.get("involvedObject") or {}).get("namespace") or (e.get("metadata") or {}).get("namespace") or ""
            if et and ns not in self.exclude and (e.get("metadata") or {}).get("uid"):
                wanted.append((e, et, ns))
        keys = [f"k8s:{e['metadata']['uid']}" for e, _, _ in wanted]
        existing: dict[str, PodEvent] = {}
        for i in range(0, len(keys), 500):
            existing.update({x.dedup_key: x for x in db.scalars(select(PodEvent).where(
                PodEvent.dedup_key.in_(keys[i:i + 500])))})
        for e, et, ns in wanted:
            io = e.get("involvedObject") or {}
            first = parse_time(e.get("firstTimestamp") or e.get("eventTime")
                               or (e.get("metadata") or {}).get("creationTimestamp")) or now
            last = parse_time(e.get("lastTimestamp") or e.get("eventTime")) or first
            if last < now - self.retention:
                continue
            dedup = f"k8s:{e['metadata']['uid']}"
            ev = existing.get(dedup)
            if ev is None:
                ikind, iname = io.get("kind", ""), io.get("name", "")
                st = states.get(f"{cid}/{ns}/{iname}") if ikind == "Pod" else None
                extra: dict = {"namespace": ns, "pod": "", "workload_kind": "", "workload": "", "node": None}
                if ikind == "Pod" and st is None:
                    kind, wl = workload_from_name(iname)
                    extra.update(pod=iname, workload_kind=kind, workload=wl)
                elif ikind == "Node":
                    extra.update(node=iname)
                elif ikind != "Pod":
                    extra.update(workload_kind=ikind, workload=iname)
                ev = self._new(st, cluster=cl, event_type=et, source="K8S_EVENT", dedup=dedup, first=first, last=last,
                               container=_container_of(io.get("fieldPath")), **({} if st else extra))
                existing[dedup] = ev
            ongoing = et == "BACKPRESSURE" and last >= now - self.recover
            ev.event_type, ev.level, ev.reason = et, EVENT_TYPES[et]["level"], (e.get("reason") or "")[:64]
            ev.message, ev.last_time = (e.get("message") or "")[:2000], last
            ev.count = int(e.get("count") or (e.get("series") or {}).get("count") or 1)
            ev.ongoing, ev.end_time = ongoing, (None if ongoing or et != "BACKPRESSURE" else last)
            ev.detail = {"k8s_type": e.get("type"), "involved_kind": io.get("kind"),
                         "component": (e.get("source") or {}).get("component")}
        # 事件已从 K8s 过期（默认保留 1 小时）但仍标记为持续中的背压：按最后出现时间结束
        current = set(keys)
        for dedup, ev in list(self.ongoing.items()):
            if ev.cluster_id == cid and ev.source == "K8S_EVENT" and dedup not in current:
                self._close(dedup, at=ev.last_time)
        for dedup in keys:
            ev = existing[dedup]
            if ev.ongoing:
                self.ongoing[dedup] = ev
            else:
                self.ongoing.pop(dedup, None)

    def _probes(self, kube: KubeApi, cl: tuple[str, str], pods: list[dict], states: dict[str, PodState]) -> None:
        for probe in self.probes:
            if probe.cluster_id not in ("*", "", cl[0]):
                continue
            res = self.probe_results.setdefault(probe.id, {"at": self.now.isoformat(), "pods": 0, "hit": 0,
                                                           "max": None, "errors": []})
            for p in pods:
                meta = p.get("metadata") or {}
                if meta.get("namespace") != probe.namespace or (p.get("status") or {}).get("phase") != "Running" \
                        or not match_selector(meta.get("labels") or {}, probe.selector):
                    continue
                st = states[f"{cl[0]}/{meta['namespace']}/{meta['name']}"]
                res["pods"] += 1
                try:
                    text = kube.metrics_text(probe.namespace, meta["name"], probe.port, probe.path)
                except HcsError as e:
                    res["errors"].append(f"{meta['name']}: {e.message[:80]}")
                    continue
                v = aggregate(parse_metric(text, probe.metric, probe.label_filter), probe.agg)
                if v is None:
                    res["errors"].append(f"{meta['name']}: 未找到指标 {probe.metric}")
                    continue
                res["max"] = v if res["max"] is None else max(res["max"], v)
                hit = OPS.get(probe.op, OPS[">="])(v, probe.threshold)
                res["hit"] += int(hit)
                self._probe_state(probe, st, cl, v, hit)

    def _probe_state(self, probe: PodProbe, st: PodState, cl: tuple[str, str], v: float, hit: bool) -> None:
        now = self.now
        data = dict(st.data or {})
        k = f"bp{probe.id}"
        d = dict(data.get(k) or {})
        dedup = f"bp:{probe.id}:{st.key}"
        ev = self.ongoing.get(dedup)
        unit = probe.unit or ""
        msg = f"{probe.name}：{probe.metric} 当前 {_fmt(v)}{unit}（阈值 {probe.op} {_fmt(probe.threshold)}{unit}）"
        detail = {"probe_id": probe.id, "probe_name": probe.name, "metric": probe.metric, "op": probe.op,
                  "threshold": probe.threshold, "unit": unit, "last": v}
        if hit:
            d["over_since"] = d.get("over_since") or now.isoformat()
            d.pop("under_since", None)
            over = datetime.fromisoformat(d["over_since"])
            if ev is not None or now - over >= timedelta(minutes=probe.for_minutes) - TOLERANCE:
                self._ongoing(st, cl, dedup, "BACKPRESSURE", first=over, reason="BackPressure", source="PROBE",
                              message=msg, count=(ev.count + 1) if ev else 1, peak=v, detail=detail)
        else:
            d.pop("over_since", None)
            if ev is not None:
                d["under_since"] = d.get("under_since") or now.isoformat()
                under = datetime.fromisoformat(d["under_since"])
                ev.detail = {**(ev.detail or {}), "last": v}
                if now - under >= timedelta(minutes=probe.recover_minutes) - TOLERANCE:
                    self._close(dedup, at=under)
                    d.pop("under_since", None)
        d.update(last=v, at=now.isoformat())
        data[k] = d
        st.data = data

    # ---------------- 汇总为巡检观测 ----------------
    def _observations(self, cl: tuple[str, str], states: dict[str, PodState]) -> list[Observation]:
        cid, cname = cl
        now = self.now
        wl: dict[tuple[str, str, str], dict] = {}
        for st in states.values():
            if st.deleted or not st.workload:
                continue
            g = wl.setdefault((st.namespace, st.workload_kind, st.workload), {"pods": 0, "ready": 0, "restarts": 0})
            g["pods"] += 1
            g["ready"] += int(st.ready)
            g["restarts"] += st.restarts
        evs: dict[tuple[str, str], list[PodEvent]] = defaultdict(list)
        for e in self.db_.scalars(select(PodEvent).where(PodEvent.cluster_id == cid, or_(
                PodEvent.last_time >= now - timedelta(hours=24), PodEvent.ongoing.is_(True)))):
            evs[(e.namespace, e.workload)].append(e)

        def cnt(items: list[PodEvent], types: tuple[str, ...], within: timedelta) -> int:
            return sum(e.count for e in items if e.event_type in types and e.last_time >= now - within)

        out: list[Observation] = []
        current: set[str] = set()
        for (ns, kind, name), g in sorted(wl.items()):
            items = evs.get((ns, name), [])
            rid = f"{cid}/{ns}/{kind}/{name}"
            current.add(rid)
            bp = [e for e in items if e.event_type == "BACKPRESSURE" and e.ongoing]
            bp_min = max((int((now - e.first_time).total_seconds() // 60) for e in bp), default=0)
            crash = len({e.pod for e in items if e.event_type == "CRASH_LOOP" and e.ongoing})
            status = f"{g['ready']}/{g['pods']} Ready"
            attrs = {"cluster": cname, "cluster_id": cid, "namespace": ns, "kind": kind, "workload": name,
                     "pods": g["pods"], "ready_pods": g["ready"]}
            metrics = {"pods": g["pods"], "ready_pods": g["ready"], "restarts_24h": cnt(items, RESTART_TYPES,
                                                                                        timedelta(hours=24)),
                       "backpressure_min": bp_min, "crashloop_pods": crash}
            display = f"{ns}/{name}"
            out += [
                Observation(RESOURCE_TYPE, rid, display, "pod_restart",
                            {"restarts_10m": cnt(items, RESTART_TYPES, timedelta(minutes=10)),
                             "restarts_1h": cnt(items, RESTART_TYPES, timedelta(hours=1))},
                            status_raw=status, attrs=attrs, metrics=metrics),
                Observation(RESOURCE_TYPE, rid, display, "pod_oom",
                            {"oom_1h": cnt(items, ("OOM_KILLED",), timedelta(hours=1))}),
                Observation(RESOURCE_TYPE, rid, display, "pod_crashloop", {"crashloop_pods": crash, "status": status}),
                Observation(RESOURCE_TYPE, rid, display, "pod_backpressure",
                            {"backpressure_min": bp_min, "backpressure_pods": len({e.pod for e in bp})}),
                Observation(RESOURCE_TYPE, rid, display, "pod_evicted",
                            {"evicted_1h": cnt(items, ("EVICTED",), timedelta(hours=1))}),
            ]
        # 已不存在的工作负载：从巡检对象中下线（历史事件保留）
        for r in self.db_.scalars(select(Resource).where(Resource.resource_type == RESOURCE_TYPE,
                                                         Resource.resource_id.like(f"{cid}/%"),
                                                         Resource.lifecycle == "ACTIVE")):
            if r.resource_id not in current:
                r.lifecycle, r.deleted_at = "DELETED", now
        return out

    def finish(self) -> None:
        db, now = self.db_, self.now
        for probe in self.probes:
            if probe.id in self.probe_results:
                r = self.probe_results[probe.id]
                r["errors"] = r["errors"][:10]
                probe.last_result = r
        tenant = PodEvent.tenant_id == self.ctx.tenant_id if self.ctx.tenant_id is not None \
            else PodEvent.tenant_id.is_(None)
        db.execute(delete(PodEvent).where(tenant, PodEvent.ongoing.is_(False), PodEvent.last_time < now - self.retention))
        st_tenant = PodState.tenant_id == self.ctx.tenant_id if self.ctx.tenant_id is not None \
            else PodState.tenant_id.is_(None)
        db.execute(delete(PodState).where(st_tenant, PodState.deleted.is_(True), PodState.last_seen < now - self.retention))
        db.flush()
        self.ctx.stats.update(events_created=self.created, probes=len(self.probe_results))
        log.info("pod events [%s]: %d new", self.ctx.account, self.created)
