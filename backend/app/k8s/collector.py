"""CCE 容器事件采集（任务 cce_pod_events，默认每 60 秒）：

1. Pod 状态比对：restartCount 增加 → 容器重启（上次退出原因为 OOMKilled 时记为 OOM 终止）；
   waiting.reason = CrashLoopBackOff → 持续型事件，恢复 Running 后结束；status.reason = Evicted → 驱逐
2. K8s Event：健康检查失败、调度失败、镜像拉取失败、节点压力、扩缩容、其他 Warning；reason / message 命中背压关键字时记为背压
3. 背压探针（可选）：经 apiserver 代理抓取 Pod /metrics 中的一个指标，持续 N 分钟超过阈值记为背压开始，连续 M 分钟恢复记为结束
4. 按工作负载汇总成巡检观测（resource_type=cce_workload），交给规则引擎产生告警（R-CCE-POD-*）

首次看到某个 Pod 时不补记历史重启，只回填 1 小时内的最近一次（避免把几个月前的重启当成新事件）。
"""
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext, log_collection_failure
from app.core.config import get_settings
from app.hcs import HcsError
from app.hcs.pager import response_items
from app.ledger.fetchers import parse_time
from app.models import InspectionTask, PodEvent, PodProbe, PodState, Resource
from app.models.base import utcnow
from app.models.pod import SCOPE_META, pod_event_key, pod_state_key
from app.services import settings_service

from .catalog import EVENT_TYPES, RESTART_TYPES, classify, match_selector
from .kube import KubeApi, aggregate, parse_metric
from .semantics import event_occurrences, occurrence_interval, window_count, workload_identity

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
              for key in ("containers", "initContainers") for c in (pod.get("spec") or {}).get(key) or []}
    out = []
    statuses = [(cs, key == "initContainerStatuses") for key in ("containerStatuses", "initContainerStatuses")
                for cs in (pod.get("status") or {}).get(key) or []]
    for cs, init in statuses:
        state = cs.get("state") or {}
        st_name = next(iter(state), "")
        st = state.get(st_name) or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        out.append({"name": cs.get("name", ""), "image": cs.get("image"), "restart_count": int(cs.get("restartCount") or 0),
                    "ready": bool(cs.get("ready")), "state": st_name, "reason": st.get("reason"),
                    "started_at": st.get("startedAt"), "last_reason": last.get("reason"),
                    "last_exit_code": last.get("exitCode"), "last_started": last.get("startedAt"),
                    "last_finished": last.get("finishedAt"), "memory_limit": limits.get(cs.get("name")),
                    "init": init, "exit_code": st.get("exitCode"), "finished_at": st.get("finishedAt")})
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
        data = ctx.client.get("cce", "/api/v3/projects/{project_id}/clusters")
        metadata = data.get("metadata")
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get("continue") not in (None, "")):
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "CCE 控制面返回未核实的分页清单，未使用不完整集群列表")
        clusters = response_items(data, "items")
        obs: list[Observation] = []
        for c in clusters:
            meta, status = c.get("metadata"), c.get("status")
            if not isinstance(meta, dict) or not isinstance(meta.get("uid"), str) or not meta["uid"] \
                    or not isinstance(status, dict) or not isinstance(status.get("phase"), str) or not status["phase"]:
                ctx.errors.append("CCE 集群标识或状态缺失，未按可用集群采集")
                continue
            if not run.scope.keep({"cluster": (meta["uid"], meta.get("name") or meta["uid"])}):
                continue                        # 采集范围：整个集群在范围外，不请求 Pod 与事件
            if status["phase"] != "Available":
                ctx.errors.append(f"集群 {meta.get('name', meta['uid'])}: 状态 {status['phase']}，容器事件未采集")
                continue
            cname = meta.get("name") or meta["uid"]
            try:
                obs.extend(run.cluster(meta["uid"], cname))
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
        # 任务参数 exclude_namespaces 是系统命名空间的默认屏蔽；“采集范围”规则（集群 / 命名空间 / 工作负载）在此之上叠加
        self.exclude = set(ctx.params.get("exclude_namespaces", ["kube-public", "kube-node-lease"]))
        self.scope = ctx.scope(RESOURCE_TYPE)
        self.out_pods: set[tuple[str, str, str]] = set()   # 本轮因工作负载在范围外而跳过的 Pod（集群 ID, 命名空间, 名称）
        task = self.db_.get(InspectionTask, TASK_CODE)
        self.sample_gap = timedelta(seconds=max(1, task.interval_sec if task else 60)) + TOLERANCE
        self.probes = list(self.db_.scalars(select(PodProbe).where(
            PodProbe.tenant_id == ctx.tenant_id, PodProbe.enabled.is_(True)))) if ctx.tenant_id is not None else []
        self.probe_results: dict[int, dict] = {}
        self.ongoing: dict[str, PodEvent] = {}
        self.created = 0
        self.clusters: set[str] = set()
        self.new_events: dict[str, PodEvent] = {}
        # Tenant/project are fixed for this run; empty scope components are explicit wildcards.
        self.unknown_events: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
        self.unavailable_events: set[str] = set()
        self.event_diagnostics: dict[str, int] = defaultdict(int)
        self.window_unknown_checks: dict[int, int] = defaultdict(int)
        self.window_unknown_events: dict[int, int] = defaultdict(int)

    def _scope(self, model) -> tuple:
        return (model.tenant_id == self.ctx.tenant_id, model.project_ref == self.ctx.project_ref)

    def _out(self, cluster: tuple[str, str], namespace: str, workload: str | None = None) -> bool:
        """确定在采集范围之外：系统命名空间，或命中范围规则。拿不准（例如还不知道工作负载）时不排除。
        集群级对象（Node 事件等）没有命名空间，命名空间规则对它们不适用。"""
        if namespace and namespace in self.exclude:
            return True
        subject: dict = {"cluster": cluster}
        if namespace:
            subject["namespace"] = namespace
        if workload is not None:
            subject["workload"] = workload
        return not self.scope.keep(subject)

    def _event_key(self, cid: str, dedup: str) -> str:
        return pod_event_key(self.ctx.tenant_id, self.ctx.project_ref, cid, dedup)

    # ---------------- 事件写入 ----------------
    def _new(self, st: PodState | None, *, cluster: tuple[str, str], event_type: str, source: str, dedup: str,
             first: datetime, last: datetime | None = None, **kw) -> PodEvent:
        detail = dict(kw.pop("detail", None) or {})
        detail[SCOPE_META] = {"dedup": dedup}
        if st is not None and (st.data or {}).get("pod_uid"):
            detail.setdefault("pod_uid", st.data["pod_uid"])
        ev = PodEvent(tenant_id=self.ctx.tenant_id, project_ref=self.ctx.project_ref,
                      cluster_id=cluster[0], cluster_name=cluster[1],
                      namespace=st.namespace if st else kw.pop("namespace", ""),
                      workload_kind=st.workload_kind if st else kw.pop("workload_kind", ""),
                      workload=st.workload if st else kw.pop("workload", ""), pod=st.pod if st else kw.pop("pod", ""),
                      node=st.node if st else kw.pop("node", None), event_type=event_type,
                      level=EVENT_TYPES[event_type]["level"], source=source,
                      dedup_key=self._event_key(cluster[0], dedup), first_time=first,
                      last_time=last or first, detail=detail, **kw)
        self.db_.add(ev)
        self.new_events[ev.dedup_key] = ev
        self.created += 1
        return ev

    def _ongoing(self, st: PodState, cluster: tuple[str, str], dedup: str, event_type: str, *, first: datetime,
                 reason: str, message: str, count: int = 1, container: str = "", source: str = "POD_STATUS",
                 peak: float | None = None, detail: dict | None = None) -> PodEvent:
        key = self._event_key(cluster[0], dedup)
        ev = self.ongoing.get(key)
        if ev is None:
            ev = self._new(st, cluster=cluster, event_type=event_type, source=source, dedup=dedup, first=first,
                           last=self.now, container=container, reason=reason, message=message, count=count,
                           ongoing=True, peak=peak, detail=detail or {})
            self.ongoing[key] = ev
            return ev
        ev.last_time, ev.message, ev.count = self.now, message, count
        if peak is not None:
            ev.peak = max(ev.peak or peak, peak)
        if detail:
            ev.detail = {**(ev.detail or {}), **detail}
        return ev

    def _close(self, dedup: str, at: datetime | None = None, *, cid: str | None = None,
               reason: str = "RECOVERED") -> None:
        if cid is not None:
            dedup = self._event_key(cid, dedup)
        ev = self.ongoing.pop(dedup, None)
        if ev is not None:
            ev.ongoing, ev.end_time = False, at or self.now
            ev.detail = {**(ev.detail or {}), "closure_reason": reason, "recovery_confirmed": reason == "RECOVERED"}

    # ---------------- 单个集群 ----------------
    def cluster(self, cid: str, cname: str) -> list[Observation]:
        db, now = self.db_, self.now
        kube = KubeApi(self.ctx.client, cid)
        cl = (cid, cname)
        pods = []
        for p in kube.pods():
            ns = (p.get("metadata") or {}).get("namespace") or ""
            if self._out(cl, ns):
                continue
            if not self.scope.open and self._out(cl, ns, workload_identity(p)[1]):
                self.out_pods.add((cid, ns, (p.get("metadata") or {}).get("name") or ""))
                continue
            pods.append(p)
        # Only a complete Pod snapshot may drive status changes or deletion. Events
        # are supplemental: a failed/incomplete Event list must not discard that snapshot.
        events = None
        try:
            events = kube.events()
        except HcsError as exc:
            self.unavailable_events.add(cid)
            self.ctx.errors.append(f"集群 {cname}: K8s Event 查询失败（HTTP {exc.status}）")
            log_collection_failure(log, self.ctx, exc, cloud_service="cce_k8s", phase="event_fetch")
        states = {f"{s.cluster_id}/{s.namespace}/{s.pod}": s for s in db.scalars(select(PodState).where(
            *self._scope(PodState), PodState.cluster_id == cid, PodState.namespace.not_in(self.exclude)))}
        if not self.scope.open:     # 范围外的 Pod 记录原样保留（既不更新也不标记删除），重新纳入范围后继续比对
            states = {key: s for key, s in states.items() if not self._out(cl, s.namespace, s.workload or None)}
        self.ongoing.update({e.dedup_key: e for e in db.scalars(select(PodEvent).where(
            *self._scope(PodEvent), PodEvent.cluster_id == cid, PodEvent.ongoing.is_(True)))})
        known_restarts = set(db.scalars(select(PodEvent.dedup_key).where(
            *self._scope(PodEvent), PodEvent.cluster_id == cid, PodEvent.source == "POD_STATUS",
            PodEvent.last_time >= now - timedelta(days=2))))

        seen: set[str] = set()
        for p in pods:
            meta, status, spec = p.get("metadata") or {}, p.get("status") or {}, p.get("spec") or {}
            ns, pname = meta.get("namespace", ""), meta.get("name", "")
            key = f"{cid}/{ns}/{pname}"
            seen.add(key)
            st = states.get(key)
            first_seen = st is None
            if st is None:
                st = PodState(key=pod_state_key(self.ctx.tenant_id, self.ctx.project_ref, cid, ns, pname),
                              tenant_id=self.ctx.tenant_id, project_ref=self.ctx.project_ref,
                              cluster_id=cid, namespace=ns, pod=pname, first_seen=now, data={}, containers=[])
                db.add(st)
                states[key] = st
            old_uid, uid = (st.data or {}).get("pod_uid"), meta.get("uid")
            if uid and old_uid and uid != old_uid:
                for dedup, ev in list(self.ongoing.items()):
                    if ev.cluster_id == cid and ev.namespace == ns and ev.pod == pname:
                        self._close(dedup, reason="POD_REPLACED")
                st.data, st.containers, st.first_seen = {}, [], now
                first_seen = True
            if uid:
                st.data = {**(st.data or {}), "pod_uid": uid}
            st.data = {**(st.data or {}), "status_observed": bool(status.get("phase"))}
            prev = {c["name"]: int(c.get("restart_count") or 0) for c in (st.containers or [])}
            kind, wl = workload_identity(p)
            st.tenant_id, st.cluster_name, st.workload_kind, st.workload = self.ctx.tenant_id, cname, kind, wl
            st.node, st.pod_ip = spec.get("nodeName"), status.get("podIP")
            containers = _containers(p)
            ready_condition = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
            # Old snapshots without conditions retain container evidence; readiness gates may never be inferred.
            regular = [c for c in containers if not c["init"]]
            pod_ready = (status.get("phase") == "Running" and bool(regular) and all(c["ready"] for c in regular)
                         and (ready_condition.get("status") == "True" if ready_condition else
                              not spec.get("readinessGates")))
            for c in containers:
                rc = c["restart_count"]
                fin = parse_time(c["last_finished"])
                if first_seen:
                    if rc > 0 and fin and fin >= now - BACKFILL:
                        self._restart(st, cl, c, rc, 1, fin, known_restarts)
                elif c["name"] in prev and rc > prev[c["name"]]:
                    self._restart(st, cl, c, rc, rc - prev[c["name"]], fin or now, known_restarts)
                if c["state"] == "terminated" and c["reason"] == "OOMKilled":
                    finished = parse_time(c["finished_at"])
                    if finished is not None and now - BACKFILL <= finished <= now + TOLERANCE:
                        self._oom(st, cl, c, finished, known_restarts, restarted=False)
                crash = f"crash:{key}/{uid or ''}/{c['name']}"
                if c["state"] == "waiting" and c["reason"] == "CrashLoopBackOff":
                    self._ongoing(st, cl, crash, "CRASH_LOOP", first=fin or now, reason="CrashLoopBackOff",
                                  container=c["name"], count=rc,
                                  message=f"容器 {c['name']} 处于 CrashLoopBackOff（累计重启 {rc} 次，上次退出："
                                          f"{c['last_reason'] or '未知'}，exitCode={c['last_exit_code']}）")
                elif c["state"] == "running" and c["ready"] and (pod_ready or c["init"]):
                    self._close(crash, cid=cid)
                elif c["state"] == "terminated":
                    self._close(crash, cid=cid, reason="CONTAINER_TERMINATED")
            evicted = f"evicted:{key}/{uid or ''}"
            if status.get("reason") == "Evicted" and not (st.data or {}).get("eviction_recorded") \
                    and self._event_key(cid, evicted) not in known_restarts:
                self._new(st, cluster=cl, event_type="EVICTED", source="POD_STATUS", dedup=evicted,
                          first=now, reason="Evicted", message=status.get("message") or "Pod 被驱逐")
                known_restarts.add(self._event_key(cid, evicted))
            if status.get("reason") == "Evicted":
                st.data = {**(st.data or {}), "eviction_recorded": True}
            waiting = next((c["reason"] for c in containers if c["state"] == "waiting" and c["reason"]), None)
            ended = next((c["reason"] for c in containers if c["state"] == "terminated" and c["reason"]), None)
            st.phase, st.status_reason = status.get("phase", ""), status.get("reason") or waiting or ended
            st.ready = pod_ready
            st.restarts = sum(c["restart_count"] for c in containers)
            st.started_at = parse_time(status.get("startTime"))
            st.labels, st.containers, st.deleted, st.last_seen = meta.get("labels") or {}, containers, False, now
        for key, st in states.items():
            if key not in seen and not st.deleted:
                st.deleted = True
                for dedup in [d for d, e in self.ongoing.items() if e.cluster_id == cid and e.namespace == st.namespace
                              and e.pod == st.pod]:
                    self._close(dedup, reason="POD_DELETED")

        if events is not None:
            self._k8s_events(cl, events, states)
        self._probes(kube, cl, pods, states)
        db.flush()
        observations = self._observations(cl, states)
        self.clusters.add(cid)
        return observations

    def _restart(self, st: PodState, cl: tuple[str, str], c: dict, rc: int, delta: int, at: datetime,
                 known: set[str]) -> None:
        public_key = f"{st.cluster_id}/{st.namespace}/{st.pod}"
        uid = (st.data or {}).get("pod_uid", "")
        dedup = f"restart:{public_key}/{uid}/{c['name']}/{rc}/{at.isoformat()}"
        key = self._event_key(cl[0], dedup)
        if key in known:
            return
        known.add(key)
        oom = c["last_reason"] == "OOMKilled"
        if oom and self._oom(st, cl, c, at, known, restarted=True):
            delta -= 1
        if delta <= 0:
            return
        msg = (f"容器 {c['name']} 重启（累计 {rc} 次），上次退出：{c['last_reason'] or '未知'}，"
               f"exitCode={c['last_exit_code']}" if not oom else f"容器 {c['name']} 另有 {delta} 次重启，退出原因未留存")
        finished = at
        if st.last_seen is not None and at < st.last_seen:
            at = self.now
        interval_start = min(st.last_seen, at) if st.last_seen and (delta > 1 or finished != at) else at
        self._new(st, cluster=cl, event_type="RESTART", source="POD_STATUS", dedup=dedup,
                  first=at, container=c["name"], reason="Restarted" if oom else c["last_reason"] or "Restarted",
                  message=msg, count=delta,
                  detail={"restart_count": rc, "exit_code": c["last_exit_code"], "image": c["image"],
                          "last_started": c["last_started"], "last_finished": c["last_finished"],
                          "count_intervals": [occurrence_interval(interval_start, at, delta)]})

    def _oom(self, st: PodState, cl: tuple[str, str], c: dict, at: datetime, known: set[str], *, restarted: bool) -> bool:
        dedup = f"oom:{st.namespace}/{st.pod}/{(st.data or {}).get('pod_uid', '')}/{c['name']}/{at.isoformat()}"
        key = self._event_key(cl[0], dedup)
        if key in known:
            if restarted:
                ev = self.db_.scalar(select(PodEvent).where(*self._scope(PodEvent), PodEvent.dedup_key == key))
                if ev is not None and (ev.detail or {}).get("restart_counted") is False:
                    ev.detail = {**(ev.detail or {}), "restart_counted": True}
                    ev.message = ev.message.replace("尚未观测到重启", "已观测到重启")
                    return True
            return False
        known.add(key)
        self._new(st, cluster=cl, event_type="OOM_KILLED", source="POD_STATUS", dedup=dedup,
                  first=at, container=c["name"], reason="OOMKilled", count=1,
                  message=f"容器 {c['name']} 被 OOM 终止（limit {c['memory_limit'] or '-'}）" +
                          ("，已观测到重启" if restarted else "，尚未观测到重启"),
                  detail={"restart_counted": restarted, "exit_code": c.get("exit_code") if not restarted
                          else c["last_exit_code"], "count_intervals": [occurrence_interval(at, at, 1)]})
        return restarted

    def _unknown_event(self, cid: str, ns: str, io: dict, event_type: str,
                       pod_uids: dict[tuple[str, str], PodState | None],
                       workloads: set[tuple[str, str, str]], previous: PodEvent | None) -> None:
        """Limit precision gaps to proven identities; never guess an owner from a Pod name."""
        kind, name, uid = (value if isinstance(value, str) else ""
                           for value in (io.get("kind"), io.get("name"), io.get("uid")))
        scope = (cid, ns, "", "")  # Unknown object: all workloads in the known namespace.
        if kind == "Node":
            scope = (cid, "", "", "")  # A Node's Event namespace does not limit its impact.
        elif kind == "Pod":
            st = pod_uids.get((ns, uid)) if uid else None
            if st is not None and (not name or name == st.pod) and st.workload_kind and st.workload:
                scope = (cid, ns, st.workload_kind, st.workload)
            elif (previous is not None and uid and (previous.detail or {}).get("pod_uid") == uid
                  and previous.namespace == ns and (not name or previous.pod == name)
                  and previous.workload_kind and previous.workload):
                # An earlier observation of this same Event can retain the old Pod's
                # verified owner after a Pod UID replacement removed it from PodState.
                scope = (cid, ns, previous.workload_kind, previous.workload)
        elif (ns, kind, name) in workloads:
            scope = (cid, ns, kind, name)
        self.unknown_events[scope].add(event_type)
        if previous is not None:
            # A malformed/regressed update cannot make the last verified Event disappear,
            # even if the incoming object identity or classification has changed.
            old_scope = ((cid, previous.namespace, previous.workload_kind, previous.workload)
                         if previous.workload_kind and previous.workload else
                         (cid, "" if (previous.detail or {}).get("involved_kind") == "Node"
                          else previous.namespace, "", ""))
            self.unknown_events[old_scope].add(previous.event_type)

    def _event_out(self, cl: tuple[str, str], ns: str, involved: dict) -> bool:
        """K8s Event 是否属于范围外的命名空间 / Pod / 工作负载；Node 等集群级对象不受命名空间规则影响。"""
        if self._out(cl, ns):
            return True
        kind, name = involved.get("kind"), involved.get("name")
        if not ns or not isinstance(name, str) or not name:
            return False
        if kind == "Pod":
            return (cl[0], ns, name) in self.out_pods
        return self._out(cl, ns, name)

    def _k8s_events(self, cl: tuple[str, str], events: list[dict], states: dict[str, PodState]) -> None:
        db, now, cid = self.db_, self.now, cl[0]
        pod_uids: dict[tuple[str, str], PodState | None] = {}
        workloads = {(st.namespace, st.workload_kind, st.workload) for st in states.values()
                     if st.workload_kind and st.workload}
        for state in states.values():
            uid = (state.data or {}).get("pod_uid")
            if isinstance(uid, str) and uid:
                key = (state.namespace, uid)
                pod_uids[key] = None if key in pod_uids else state
        wanted = []
        for e in events:
            et = classify(e.get("reason") or "", e.get("type") or "Normal", e.get("message") or "", self.bp)
            io = e.get("involvedObject") or {}
            ns = next((v for v in (io.get("namespace"), (e.get("metadata") or {}).get("namespace"))
                       if isinstance(v, str) and v), "")
            if et and (e.get("metadata") or {}).get("uid") and not self._event_out(cl, ns, io):
                wanted.append((e, et, ns))
        keys = [self._event_key(cid, f"k8s:{e['metadata']['uid']}") for e, _, _ in wanted]
        existing: dict[str, PodEvent] = {}
        for i in range(0, len(keys), 500):
            existing.update({x.dedup_key: x for x in db.scalars(select(PodEvent).where(
                *self._scope(PodEvent), PodEvent.cluster_id == cid, PodEvent.dedup_key.in_(keys[i:i + 500])))})
        for e, et, ns in wanted:
            io = e.get("involvedObject") or {}
            dedup = f"k8s:{e['metadata']['uid']}"
            scoped_key = self._event_key(cid, dedup)
            ev = existing.get(scoped_key)
            try:
                first, last, count = event_occurrences(e, now, tolerance=TOLERANCE)
            except ValueError as exc:
                self._unknown_event(cid, ns, io, et, pod_uids, workloads, ev)
                self.event_diagnostics[str(exc)] += 1
                continue
            if ev is not None and (count < ev.count or last < ev.last_time):
                self._unknown_event(cid, ns, io, et, pod_uids, workloads, ev)
                reason = "event_counter_regressed" if count < ev.count else "event_time_regressed"
                self.event_diagnostics[reason] += 1
                continue
            if last < now - self.retention:
                continue
            if ev is None:
                ikind, iname = io.get("kind", ""), io.get("name", "")
                st = states.get(f"{cid}/{ns}/{iname}") if ikind == "Pod" else None
                if st is not None and (st.data or {}).get("pod_uid") and io.get("uid") != st.data["pod_uid"]:
                    st = None
                extra: dict = {"namespace": ns, "pod": "", "workload_kind": "", "workload": "", "node": None}
                if ikind == "Pod" and st is None:
                    extra.update(pod=iname, workload_kind="Pod", workload=iname)
                elif ikind == "Node":
                    extra.update(node=iname)
                elif ikind != "Pod":
                    extra.update(workload_kind=ikind, workload=iname)
                detail = {"pod_uid": io.get("uid") if ikind == "Pod" else None,
                          "identity_precision": "UID" if io.get("uid") else "NAME_ONLY",
                          "restart_counted": et != "OOM_KILLED",
                          "count_intervals": [occurrence_interval(first, last, count)]}
                status_key = self._event_key(cid, f"evicted:{cid}/{ns}/{iname}/{io.get('uid') or ''}")
                status_event = None
                if et == "EVICTED" and ikind == "Pod":
                    status_event = self.new_events.get(status_key) or db.scalar(select(PodEvent).where(
                        *self._scope(PodEvent), PodEvent.cluster_id == cid, PodEvent.dedup_key == status_key))
                if status_event is not None:
                    ev = status_event
                    ev.source, ev.dedup_key, ev.first_time = "K8S_EVENT", scoped_key, first
                    ev.detail = {**(ev.detail or {}), **detail, SCOPE_META: {"dedup": dedup}}
                else:
                    ev = self._new(st, cluster=cl, event_type=et, source="K8S_EVENT", dedup=dedup, first=first, last=last,
                                   container=_container_of(io.get("fieldPath")), count=count, detail=detail,
                                   **({} if st else extra))
                existing[scoped_key] = ev
            elif count > ev.count:
                intervals = list((ev.detail or {}).get("count_intervals") or
                                 [occurrence_interval(ev.first_time, ev.last_time, ev.count)])
                intervals.append(occurrence_interval(ev.last_time, last, count - ev.count))
                # Keep payload bounded; coalescing loses only boundary precision and never fabricates counts.
                if len(intervals) > 512:
                    prefix = intervals[:256]
                    intervals = [{"first": prefix[0]["first"], "last": prefix[-1]["last"],
                                  "count": sum(item["count"] for item in prefix)}, *intervals[256:]]
                ev.detail = {**(ev.detail or {}), "count_intervals": intervals}
            ongoing = et == "BACKPRESSURE" and last >= now - self.recover
            ev.event_type, ev.level, ev.reason = et, EVENT_TYPES[et]["level"], (e.get("reason") or "")[:64]
            ev.message, ev.last_time = (e.get("message") or "")[:2000], last
            ev.count = count
            ev.ongoing, ev.end_time = ongoing, (None if ongoing or et != "BACKPRESSURE" else last)
            ev.detail = {**(ev.detail or {}), "k8s_type": e.get("type"), "involved_kind": io.get("kind"),
                         "component": (e.get("source") or {}).get("component")}
            if et == "BACKPRESSURE" and not ongoing:
                ev.detail = {**ev.detail, "closure_reason": "EVENT_EXPIRED", "recovery_confirmed": False}
        # 事件已从 K8s 过期（默认保留 1 小时）但仍标记为持续中的背压：按最后出现时间结束
        current = set(keys)
        for dedup, ev in list(self.ongoing.items()):
            if ev.cluster_id == cid and ev.source == "K8S_EVENT" and dedup not in current:
                self._close(dedup, at=ev.last_time, reason="EVENT_EXPIRED")
        for dedup in keys:
            ev = existing.get(dedup)
            if ev is None:
                continue
            if ev.ongoing:
                self.ongoing[dedup] = ev
            else:
                self.ongoing.pop(dedup, None)

    def _probes(self, kube: KubeApi, cl: tuple[str, str], pods: list[dict], states: dict[str, PodState]) -> None:
        for probe in self.probes:
            if probe.cluster_id not in ("*", "", cl[0]):
                continue
            res = self.probe_results.setdefault(probe.id, {"at": self.now.isoformat(), "pods": 0, "hit": 0,
                                                           "observed": 0, "max": None, "errors": []})
            for p in pods:
                meta = p.get("metadata") or {}
                if meta.get("namespace") != probe.namespace \
                        or not match_selector(meta.get("labels") or {}, probe.selector):
                    continue
                st = states[f"{cl[0]}/{meta['namespace']}/{meta['name']}"]
                res["pods"] += 1
                if (p.get("status") or {}).get("phase") != "Running":
                    message = f"{meta['name']}: Pod 非 Running，背压状态未知"
                    res["errors"].append(message)
                    self._probe_gap(probe, st, cl, message)
                    continue
                try:
                    text = kube.metrics_text(probe.namespace, meta["name"], probe.port, probe.path)
                except HcsError as e:
                    message = f"{meta['name']}: 指标查询失败（HTTP {e.status}）"
                    res["errors"].append(message)
                    self._probe_gap(probe, st, cl, message)
                    continue
                v = aggregate(parse_metric(text, probe.metric, probe.label_filter), probe.agg)
                if v is None:
                    message = f"{meta['name']}: 未找到有效指标 {probe.metric}"
                    res["errors"].append(message)
                    self._probe_gap(probe, st, cl, message)
                    continue
                res["max"] = v if res["max"] is None else max(res["max"], v)
                res["observed"] += 1
                hit = OPS.get(probe.op, OPS[">="])(v, probe.threshold)
                res["hit"] += int(hit)
                self._probe_state(probe, st, cl, v, hit)

    def _probe_gap(self, probe: PodProbe, st: PodState, cl: tuple[str, str], message: str) -> None:
        data = dict(st.data or {})
        data[f"bp{probe.id}"] = {"at": self.now.isoformat(), "unknown": True}
        st.data = data
        key = self._event_key(cl[0], f"bp:{probe.id}:{st.cluster_id}/{st.namespace}/{st.pod}")
        ev = self.ongoing.get(key)
        if ev is not None:
            ev.detail = {**(ev.detail or {}), "signal_status": "UNKNOWN", "last_error_at": self.now.isoformat()}
        self.ctx.errors.append(f"背压探针 {probe.id}: {message}")

    def _probe_state(self, probe: PodProbe, st: PodState, cl: tuple[str, str], v: float, hit: bool) -> None:
        now = self.now
        data = dict(st.data or {})
        k = f"bp{probe.id}"
        d = dict(data.get(k) or {})
        signature = hashlib.sha256(json.dumps([probe.cluster_id, probe.namespace, probe.selector, probe.port, probe.path,
                                   probe.metric, probe.label_filter, probe.agg, probe.op, probe.threshold, probe.unit,
                                   probe.for_minutes, probe.recover_minutes], ensure_ascii=True).encode()).hexdigest()
        public_key = f"{st.cluster_id}/{st.namespace}/{st.pod}"
        dedup = f"bp:{probe.id}:{public_key}"
        if d.get("signature") is not None and d["signature"] != signature:
            self._close(dedup, cid=cl[0], reason="PROBE_CHANGED")
            d = {}
        sampled = parse_time(d.get("at"))
        # An unobserved interval cannot satisfy a continuous trigger/recovery duration.
        if sampled is not None and now - sampled > self.sample_gap:
            d.pop("over_since", None)
            d.pop("under_since", None)
        ev = self.ongoing.get(self._event_key(cl[0], dedup))
        unit = probe.unit or ""
        msg = f"{probe.name}：{probe.metric} 当前 {_fmt(v)}{unit}（阈值 {probe.op} {_fmt(probe.threshold)}{unit}）"
        detail = {"probe_id": probe.id, "probe_name": probe.name, "metric": probe.metric, "op": probe.op,
                  "threshold": probe.threshold, "unit": unit, "last": v, "signal_status": "OBSERVED"}
        if hit:
            d["over_since"] = d.get("over_since") or now.isoformat()
            d.pop("under_since", None)
            over = datetime.fromisoformat(d["over_since"])
            detail["confirmed_since"] = over.isoformat()
            if ev is not None or now - over >= timedelta(minutes=probe.for_minutes) - TOLERANCE:
                self._ongoing(st, cl, dedup, "BACKPRESSURE", first=over, reason="BackPressure", source="PROBE",
                              message=msg, count=(ev.count + 1) if ev else 1, peak=v, detail=detail)
        else:
            d.pop("over_since", None)
            if ev is not None:
                d["under_since"] = d.get("under_since") or now.isoformat()
                under = datetime.fromisoformat(d["under_since"])
                ev.detail = {**(ev.detail or {}), "last": v, "signal_status": "OBSERVED"}
                if now - under >= timedelta(minutes=probe.recover_minutes) - TOLERANCE:
                    self._close(dedup, at=under, cid=cl[0])
                    d.pop("under_since", None)
        d.update(last=v, at=now.isoformat(), unknown=False, signature=signature)
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
            g = wl.setdefault((st.namespace, st.workload_kind, st.workload), {
                "pods": 0, "ready": 0, "restarts": 0, "unknown": False,
                "probe_observed": True, "probe_expected": False})
            g["pods"] += 1
            g["ready"] += int(st.ready)
            g["restarts"] += st.restarts
            g["unknown"] |= (st.data or {}).get("status_observed") is False
            expected = [probe for probe in self.probes if probe.cluster_id in ("*", "", cid)
                        and probe.namespace == st.namespace and match_selector(st.labels or {}, probe.selector)]
            g["probe_expected"] |= bool(expected)
            g["probe_observed"] &= bool(expected) and all(
                (st.data or {}).get(f"bp{probe.id}", {}).get("at") == now.isoformat()
                and (st.data or {}).get(f"bp{probe.id}", {}).get("unknown") is False for probe in expected)
        evs: dict[tuple[str, str, str], list[PodEvent]] = defaultdict(list)
        for e in self.db_.scalars(select(PodEvent).where(*self._scope(PodEvent), PodEvent.cluster_id == cid, or_(
                PodEvent.last_time >= now - timedelta(hours=24), PodEvent.ongoing.is_(True)))):
            evs[(e.namespace, e.workload_kind, e.workload)].append(e)

        def cnt(items: list[PodEvent], types: tuple[str, ...], within: timedelta,
                unknown: set[str]) -> int | None:
            # K8s OOM Events explicitly have restart_counted=False. Restart counters
            # therefore remain independently observable from complete PodStatus data.
            if types != RESTART_TYPES and (cid in self.unavailable_events or unknown.intersection(types)):
                return None
            counts = [window_count(e, now - within, restarts=types == RESTART_TYPES)
                      for e in items if e.event_type in types]
            if any(n is None for n in counts):
                window_seconds = int(within.total_seconds())
                self.window_unknown_checks[window_seconds] += 1
                self.window_unknown_events[window_seconds] += sum(n is None for n in counts)
                return None
            return sum(n for n in counts if n is not None)

        out: list[Observation] = []
        current: set[str] = set()
        for (ns, kind, name), g in sorted(wl.items()):
            items = evs.get((ns, kind, name), [])
            unknown = set().union(*(self.unknown_events.get(scope, set()) for scope in
                                   ((cid, "", "", ""), (cid, ns, "", ""), (cid, ns, kind, name))))
            rid = f"{cid}/{ns}/{kind}/{name}"
            current.add(rid)
            if g["unknown"]:
                self.ctx.errors.append("Pod 状态尚未就绪，未输出工作负载正常观测")
                continue
            bp = [e for e in items if e.event_type == "BACKPRESSURE" and e.ongoing]
            bp_known = (cid not in self.unavailable_events and "BACKPRESSURE" not in unknown
                        and not any((e.detail or {}).get("signal_status") == "UNKNOWN" for e in bp)
                        and not (g["probe_expected"] and not g["probe_observed"]))
            bp_min = max((int((now - (parse_time((e.detail or {}).get("confirmed_since"))
                                     or e.first_time)).total_seconds() // 60) for e in bp), default=0)
            crash = len({e.pod for e in items if e.event_type == "CRASH_LOOP" and e.ongoing})
            crash_known = (crash > 0 or g["ready"] == g["pods"] or not any(
                e.event_type == "CRASH_LOOP" and (e.detail or {}).get("recovery_confirmed") is False for e in items))
            status = f"{g['ready']}/{g['pods']} Ready"
            attrs = {"cluster": cname, "cluster_id": cid, "namespace": ns, "kind": kind, "workload": name,
                     "pods": g["pods"], "ready_pods": g["ready"]}
            metrics = {"pods": g["pods"], "ready_pods": g["ready"], "restarts_24h": cnt(items, RESTART_TYPES,
                                                                                        timedelta(hours=24), unknown),
                       "backpressure_min": bp_min if bp_known and (bp or g["probe_observed"]) else None,
                       "crashloop_pods": crash if crash_known else None}
            display = f"{ns}/{name}"
            observations = [
                Observation(RESOURCE_TYPE, rid, display, "pod_restart",
                            {"restarts_10m": cnt(items, RESTART_TYPES, timedelta(minutes=10), unknown),
                             "restarts_1h": cnt(items, RESTART_TYPES, timedelta(hours=1), unknown)},
                            status_raw=status, attrs=attrs, metrics=metrics),
                Observation(RESOURCE_TYPE, rid, display, "pod_oom",
                            {"oom_1h": cnt(items, ("OOM_KILLED",), timedelta(hours=1), unknown)}),
                Observation(RESOURCE_TYPE, rid, display, "pod_crashloop", {"crashloop_pods": crash, "status": status}),
                Observation(RESOURCE_TYPE, rid, display, "pod_backpressure",
                            {"backpressure_min": bp_min, "backpressure_pods": len({e.pod for e in bp})}),
                Observation(RESOURCE_TYPE, rid, display, "pod_evicted",
                            {"evicted_1h": cnt(items, ("EVICTED",), timedelta(hours=1), unknown)}),
            ]
            for observation in observations:
                observation.facts = {key: value for key, value in observation.facts.items() if value is not None}
                observation.metrics = {key: value for key, value in observation.metrics.items() if value is not None}
                if observation.check_item == "pod_crashloop" and not crash_known:
                    self.ctx.errors.append("CrashLoop 目标已终止或替换，尚未观测到 Ready 恢复")
                    continue
                if observation.check_item == "pod_backpressure":
                    if not bp_known:
                        continue
                    if not bp and not g["probe_observed"]:
                        if any(e.event_type == "BACKPRESSURE" and (e.detail or {}).get("recovery_confirmed") is False
                               for e in items):
                            self.ctx.errors.append("背压事件已过期或目标消失，没有恢复采样证据")
                        continue
                if observation.facts:
                    out.append(observation)
        # 已不存在的工作负载：从巡检对象中下线（历史事件保留）
        for r in self.db_.scalars(select(Resource).where(Resource.resource_type == RESOURCE_TYPE,
                                                         Resource.tenant_id == self.ctx.tenant_id,
                                                         Resource.project_ref == self.ctx.project_ref,
                                                         Resource.resource_id.startswith(f"{cid}/", autoescape=True),
                                                         Resource.lifecycle == "ACTIVE")):
            parts = r.resource_id.split("/")           # 集群ID/命名空间/类型/名称
            if (r.resource_id in current or r.scope_excluded or parts[1] in self.exclude
                    or self._out(cl, parts[1], parts[3] if len(parts) == 4 else None)):
                continue                                # 范围外的工作负载不再采集，不等于已删除
            r.lifecycle, r.deleted_at = "DELETED", now
        return out

    def finish(self) -> None:
        db, now = self.db_, self.now
        for probe in self.probes:
            if probe.id in self.probe_results:
                r = self.probe_results[probe.id]
                r["status"] = "UNKNOWN" if not r["observed"] else "PARTIAL" if r["errors"] else "OBSERVED"
                r["errors"] = r["errors"][:10]
                probe.last_result = r
        db.execute(delete(PodEvent).where(*self._scope(PodEvent), PodEvent.cluster_id.in_(self.clusters),
                                         PodEvent.ongoing.is_(False), PodEvent.last_time < now - self.retention))
        db.execute(delete(PodState).where(*self._scope(PodState), PodState.cluster_id.in_(self.clusters),
                                         PodState.deleted.is_(True), PodState.last_seen < now - self.retention))
        db.flush()
        self.ctx.stats.update(events_created=self.created, probes=len(self.probe_results))
        # Event history/precision limitations are diagnostics, not collection failures.
        # Keep unknown facts absent so they cannot fire or recover workload alarms.
        if self.event_diagnostics:
            self.ctx.stats["event_data_diagnostics"] = dict(self.event_diagnostics)
        if self.window_unknown_checks:
            self.ctx.stats.update(event_window_unknown_checks=sum(self.window_unknown_checks.values()),
                                  event_window_unknown_events=sum(self.window_unknown_events.values()))
        for reason, affected_count in sorted(self.event_diagnostics.items()):
            log.info("collection_data_unavailable", extra={"event": "collection_data_unavailable",
                     "cloud_service": "cce_k8s", "phase": "event_validation", "reason": reason,
                     "affected_count": affected_count,
                     "tenant_id": self.ctx.tenant_id, "project_ref": self.ctx.project_ref})
        for window_seconds, affected_count in sorted(self.window_unknown_checks.items()):
            log.info("collection_data_unavailable", extra={"event": "collection_data_unavailable",
                        "cloud_service": "cce_k8s", "phase": "event_window_count",
                        "reason": "cumulative_count_crosses_window", "affected_count": affected_count,
                        "unknown_event_count": self.window_unknown_events[window_seconds],
                        "window_seconds": window_seconds,
                        "tenant_id": self.ctx.tenant_id, "project_ref": self.ctx.project_ref})
        log.info("pod events [%s]: %d new", self.ctx.account, self.created)
