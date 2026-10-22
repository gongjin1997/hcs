"""容器事件类型、K8s Event 归类规则、工作负载推断。"""
import re

# level：CRITICAL / WARNING / INFO；ongoing：持续型事件（有开始和结束）
EVENT_TYPES: dict[str, dict] = {
    "RESTART": {"name": "容器重启", "level": "WARNING", "color": "#F57C00"},
    "OOM_KILLED": {"name": "OOM 终止", "level": "CRITICAL", "color": "#D93026"},
    "CRASH_LOOP": {"name": "CrashLoopBackOff", "level": "CRITICAL", "color": "#B3261E", "ongoing": True},
    "BACKPRESSURE": {"name": "背压", "level": "WARNING", "color": "#7B1FA2", "ongoing": True},
    "EVICTED": {"name": "Pod 驱逐", "level": "WARNING", "color": "#8D6E63"},
    "PROBE_FAILED": {"name": "健康检查失败", "level": "WARNING", "color": "#E0A100"},
    "SCHEDULE_FAILED": {"name": "调度失败", "level": "WARNING", "color": "#546E7A"},
    "IMAGE_PULL_FAILED": {"name": "镜像拉取失败", "level": "WARNING", "color": "#00838F"},
    "NODE_PRESSURE": {"name": "节点资源压力", "level": "WARNING", "color": "#6D4C41"},
    "SCALED": {"name": "扩缩容", "level": "INFO", "color": "#1976D2"},
    "WARNING": {"name": "其他 Warning", "level": "WARNING", "color": "#78909C"},
}
LEVEL_LABEL = {"CRITICAL": "严重", "WARNING": "警告", "INFO": "提示"}
SOURCE_LABEL = {"K8S_EVENT": "K8s 事件", "POD_STATUS": "Pod 状态", "PROBE": "背压探针"}
RESTART_TYPES = ("RESTART", "OOM_KILLED")

_IMAGE_REASONS = {"ErrImagePull", "ImagePullBackOff", "InspectFailed", "ErrImageNeverPull"}
_NODE_PRESSURE = {"EvictionThresholdMet", "NodeHasDiskPressure", "NodeHasInsufficientMemory", "NodeHasMemoryPressure",
                  "NodeHasInsufficientPID", "FreeDiskSpaceFailed", "ImageGCFailed"}


def classify(reason: str, event_type: str, message: str, backpressure: re.Pattern[str]) -> str | None:
    """K8s Event → 容器事件类型；返回 None 表示不关心（大部分 Normal 事件）。

    容器重启、CrashLoopBackOff 以 Pod 状态（restartCount / waiting.reason）为准，对应的 BackOff / Killing 事件不重复记录。
    """
    msg = message or ""
    if backpressure.search(reason or "") or backpressure.search(msg):
        return "BACKPRESSURE"
    if reason in ("OOMKilling", "SystemOOM"):
        return "OOM_KILLED"
    if reason == "BackOff":
        return "IMAGE_PULL_FAILED" if "image" in msg.lower() else None
    if reason in _IMAGE_REASONS or (reason == "Failed" and "image" in msg.lower()):
        return "IMAGE_PULL_FAILED"
    if reason in ("Unhealthy", "ProbeWarning"):
        return "PROBE_FAILED"
    if reason == "FailedScheduling":
        return "SCHEDULE_FAILED"
    if reason in ("Evicted", "Preempting", "TaintManagerEviction"):
        return "EVICTED"
    if reason in _NODE_PRESSURE:
        return "NODE_PRESSURE"
    if reason in ("ScalingReplicaSet", "SuccessfulRescale"):
        return "SCALED"
    if reason == "Killing":
        return None
    return "WARNING" if event_type == "Warning" else None


def workload_of(pod: dict) -> tuple[str, str]:
    """按 ownerReferences 推断所属工作负载：ReplicaSet → Deployment，Job（CronJob 创建）→ CronJob。"""
    meta = pod.get("metadata") or {}
    owners = meta.get("ownerReferences") or []
    owner = next((o for o in owners if o.get("controller")), owners[0] if owners else None)
    if not owner:
        return "Pod", meta.get("name", "")
    kind, name = owner.get("kind", ""), owner.get("name", "")
    if kind == "ReplicaSet":
        h = (meta.get("labels") or {}).get("pod-template-hash")
        if h and name.endswith(f"-{h}"):
            return "Deployment", name[: -len(h) - 1]
        return "Deployment", name.rsplit("-", 1)[0]
    if kind == "Job":
        base, _, suffix = name.rpartition("-")
        if base and suffix.isdigit() and len(suffix) >= 8:
            return "CronJob", base
    return kind, name


def workload_from_name(pod: str) -> tuple[str, str]:
    """Pod 已不存在时按命名规则推断：{deploy}-{rs hash}-{5 位随机}、{sts}-{序号}。"""
    parts = pod.split("-")
    if len(parts) >= 3 and len(parts[-1]) == 5 and 6 <= len(parts[-2]) <= 10:
        return "Deployment", "-".join(parts[:-2])
    if len(parts) >= 2 and parts[-1].isdigit():
        return "StatefulSet", "-".join(parts[:-1])
    return "Pod", pod


def match_selector(labels: dict, selector: str) -> bool:
    """简单标签选择器：a=b,c!=d,e（存在）。"""
    for item in [x.strip() for x in (selector or "").split(",") if x.strip()]:
        if "!=" in item:
            k, v = item.split("!=", 1)
            if labels.get(k.strip()) == v.strip():
                return False
        elif "=" in item:
            k, v = item.split("=", 1)
            if labels.get(k.strip().rstrip("=")) != v.strip().lstrip("="):
                return False
        elif item not in labels:
            return False
    return True
