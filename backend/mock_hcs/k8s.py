"""Mock CCE 集群的 Kubernetes API 数据：按数据集里的工作负载定义（WORKLOADS / K8S_EVENTS / POD_METRICS）生成
Pod、Event 列表与 Pod /metrics 文本，字段格式与 kube-apiserver v1 一致。"""
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pod_name(w: dict, p: dict, idx: int) -> str:
    kind, name = w["kind"], w["workload"]
    if kind == "Deployment":
        return f"{name}-{w['hash']}-{p['suffix']}"
    if kind == "StatefulSet":
        return f"{name}-{idx}"
    if kind == "CronJob":
        return f"{name}-{w['job']}-{p['suffix']}"
    return f"{name}-{p['suffix']}"


def _owner(w: dict) -> dict:
    kind, name = w["kind"], w["workload"]
    if kind == "Deployment":
        return {"kind": "ReplicaSet", "name": f"{name}-{w['hash']}", "controller": True}
    if kind == "CronJob":
        return {"kind": "Job", "name": f"{name}-{w['job']}", "controller": True}
    return {"kind": kind, "name": name, "controller": True}


def _container_status(ds: ModuleType, w: dict, p: dict, now: datetime) -> dict:
    started = now - timedelta(minutes=p.get("up_min", w.get("age_min", 6 * 1440)))
    rc = p.get("restarts", 0)
    cs: dict[str, Any] = {"name": w["container"], "image": w["image"], "imageID": "", "restartCount": rc,
                          "ready": True, "started": True, "state": {"running": {"startedAt": _ts(started)}}}
    if p.get("last"):
        reason, code, ago_min = p["last"]
        fin = now - timedelta(minutes=ago_min)
        cs["lastState"] = {"terminated": {"reason": reason, "exitCode": code, "startedAt": _ts(fin - timedelta(hours=5)),
                                          "finishedAt": _ts(fin)}}
        if not p.get("waiting"):
            cs["state"] = {"running": {"startedAt": _ts(fin + timedelta(seconds=12))}}
    if p.get("waiting"):
        cs["state"] = {"waiting": {"reason": p["waiting"], "message": p.get("waiting_msg", "")}}
        cs["ready"] = cs["started"] = False
    if p.get("unready"):
        cs["ready"] = False
    if p.get("completed"):
        cs["state"] = {"terminated": {"reason": "Completed", "exitCode": 0, "startedAt": _ts(started),
                                      "finishedAt": _ts(started + timedelta(minutes=7))}}
        cs["ready"] = False
    return cs


def pods(ds: ModuleType, cluster: str, now: datetime | None = None) -> list[dict]:
    now = now or ds.NOW
    out = []
    for w in ds.WORKLOADS.get(cluster, []):
        for idx, p in enumerate(w["pods"]):
            name = pod_name(w, p, idx)
            labels = {**w.get("labels", {}), **({"pod-template-hash": w["hash"]} if w["kind"] == "Deployment" else {})}
            created = now - timedelta(minutes=p.get("age_min", w.get("age_min", 6 * 1440)))
            meta = {"name": name, "namespace": w["ns"], "uid": ds.rid("pod", f"{cluster}/{w['ns']}/{name}"),
                    "labels": labels, "creationTimestamp": _ts(created), "ownerReferences": [_owner(w)]}
            spec = {"nodeName": p.get("node"), "containers": [{"name": w["container"], "image": w["image"],
                                                               "resources": {"limits": {"memory": w.get("limit", "1Gi"),
                                                                                        "cpu": w.get("cpu", "1")}}}]}
            status: dict[str, Any] = {"phase": "Running", "podIP": p.get("ip"), "hostIP": None, "startTime": _ts(created)}
            if p.get("pending"):
                status = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False",
                                                              "reason": "Unschedulable"}]}
                spec.pop("nodeName")
            elif p.get("evicted"):
                status = {"phase": "Failed", "reason": "Evicted", "message": p["evicted"], "startTime": _ts(created)}
            else:
                status["containerStatuses"] = [_container_status(ds, w, p, now)]
                if p.get("completed"):
                    status["phase"] = "Succeeded"
            out.append({"metadata": meta, "spec": spec, "status": status})
    return out


def events(ds: ModuleType, cluster: str, now: datetime | None = None) -> list[dict]:
    now = now or ds.NOW
    out = []
    for i, e in enumerate(ds.K8S_EVENTS.get(cluster, [])):
        first, last = now - timedelta(minutes=e["first_min"]), now - timedelta(minutes=e.get("last_min", e["first_min"]))
        obj = {"kind": e["kind"], "name": e["name"], "namespace": e.get("ns", "")}
        if e["kind"] == "Pod" and e.get("container"):
            obj["fieldPath"] = f"spec.containers{{{e['container']}}}"
        out.append({"metadata": {"name": f"{e['name']}.{17_000_000 + i:x}", "namespace": e.get("ns", "default"),
                                 "uid": ds.rid("k8s-event", f"{cluster}/{i}/{e['reason']}/{e['name']}"),
                                 "creationTimestamp": _ts(first)},
                    "involvedObject": obj, "reason": e["reason"], "message": e["message"], "type": e.get("type", "Warning"),
                    "count": e.get("count", 1), "firstTimestamp": _ts(first), "lastTimestamp": _ts(last),
                    "source": {"component": e.get("component", "kubelet"), "host": e.get("host")}})
    return out


def namespaces(ds: ModuleType, cluster: str) -> list[dict]:
    names = sorted({w["ns"] for w in ds.WORKLOADS.get(cluster, [])} | {"default", "kube-system", "kube-public"})
    return [{"metadata": {"name": n, "uid": ds.rid("ns", f"{cluster}/{n}")}, "status": {"phase": "Active"}} for n in names]


def metrics_text(ds: ModuleType, cluster: str, ns: str, pod: str) -> str | None:
    """Pod 自身暴露的 Prometheus 文本（背压探针抓取）。返回 None 表示该 Pod 没有暴露指标端口。"""
    for w in ds.WORKLOADS.get(cluster, []):
        if w["ns"] != ns or "metrics" not in w:
            continue
        for idx, p in enumerate(w["pods"]):
            if pod_name(w, p, idx) != pod:
                continue
            m = w["metrics"]
            lines = [f"# HELP {m['name']} {m.get('help', '')}", f"# TYPE {m['name']} gauge"]
            for task, base in m["series"]:
                v = p.get("bp", base) if task == m.get("hot_task") else base
                lines.append(f'{m["name"]}{{job_name="{m["job"]}",task_name="{task}",subtask_index="{idx}"}} {float(v)}')
            lines.append(f'flink_taskmanager_Status_JVM_CPU_Load{{host="{pod}"}} 0.42')
            return "\n".join(lines) + "\n"
    return None
