"""资源/指标唯一说明源，供指南页面、规则 target 和 Markdown 文档复用。

口径以当前 collectors、metrics、ledger/fetchers 为准，不以演示数据证明上游能力。
事实的 check_items 是规则引擎实际分发边界；空列表表示流水线通用事实。
"""

from app.schemas.guide import GuideField, ResourceGuide
from app.services.catalog import RESOURCE_TYPES, TYPE_TASKS


def _field(
    key: str,
    label: str,
    unit: str | None,
    meaning: str,
    source: str,
    data_type: str = "string",
    checks: tuple[str, ...] = (),
    *,
    limitations: tuple[str, ...] = (),
    availability: str = "collected",
) -> GuideField:
    return GuideField.model_validate(
        {
            "key": key,
            "label": label,
            "unit": unit,
            "meaning": meaning,
            "source": source,
            "data_type": data_type,
            "optional": True,
            "check_items": list(checks),
            "limitations": list(limitations),
            "availability": availability,
        }
    )


def _state(key: str, label: str, meaning: str, source: str, check: str) -> GuideField:
    return _field(key, label, None, meaning, source, checks=(check,))


def _number(
    key: str, label: str, unit: str, meaning: str, source: str, check: str, *, limitations: tuple[str, ...] = ()
) -> GuideField:
    return _field(key, label, unit, meaning, source, "number", (check,), limitations=limitations)


_CES_LIMIT = ("AOM 已停用；可选 Prometheus，CES 仅供旧部署显式启用；无数据或权限不足不等于 0。",)
_EVENT_LIMIT = ("按本地已采集事件的 count 汇总，不是完整审计记录；漏采、事件过期和重复来源可能影响结果。",)
_STALE = "last_seen 不是指标时间；ECS 区分求值/源时间，DCS/GaussDB 使用响应接收时间，作业/容器使用本地采集证据及有效期。"
_NO_DATA = "未知、null、缺失和未采集表示没有证据，不等于 0、健康或采集成功。"


# 与 Observation.metrics 对应；另保留两项页面遗留展示键，明确 not_collected。
_METRICS: dict[str, list[GuideField]] = {
    "ecs_server": [
        _number(
            "cpu_util",
            "CPU 使用率",
            "%",
            "CPU 百分比；Prometheus 默认取 5 分钟非空闲比例。",
            "metrics/prometheus.py: node_cpu_seconds_total；旧部署可选 CES；AOM 已停用",
            "cpu",
            limitations=_CES_LIMIT + ("不同来源的采样与聚合口径不同，切换来源后不直接比较短时波动。",),
        ),
        _number(
            "mem_util",
            "内存使用率",
            "%",
            "Prometheus 默认 (1-MemAvailable/MemTotal)×100。",
            "node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes；旧部署可选 CES；AOM 已停用",
            "memory",
            limitations=("Prometheus 需要 node_exporter 与私网 IP 标签匹配；AOM 已停用。",),
        ),
        _number(
            "disk_util",
            "最满分区使用率",
            "%",
            "Prometheus 在同一实际采样时间返回的文件系统中取最大值。",
            "(1-node_filesystem_avail_bytes/node_filesystem_size_bytes)×100；旧部署可选 CES；AOM 已停用",
            "disk",
            limitations=("Prometheus 默认排除 tmpfs、overlay、squashfs、iso9660；缺失的挂载点不参与最大值。",),
        ),
        _field(
            "disk_mount",
            "最满分区挂载点",
            None,
            "与 disk_util 同一序列的 mount_point/mountpoint；无标签时为 '-'。",
            "metrics/ces.py / metrics/prometheus.py",
            checks=("disk",),
        ),
        _number(
            "reboots_24h",
            "近 24 小时观测重启",
            "次",
            "汇总本地近 24 小时 STATUS/REBOOT 变更，并计入本轮新进入 REBOOT/HARD_REBOOT 或开机时间变化。",
            "collectors/ecs.py: ResourceChange 与 Prometheus node_boot_time_seconds",
            "reboot",
            limitations=(
                "轮询会漏掉两次采集间完成的重启；双来源可能重复计数，不能视为精确重启审计。",
                "开机时间须比历史值前移超过 60 秒且不早于上次观测减 60 秒；首次无历史基线不计。",
            ),
        ),
    ],
    "dgc_realtime_job": [
        _number(
            "nodes",
            "返回节点总数",
            "个",
            "作业状态响应 nodes 数组长度；只代表响应中返回的节点。",
            "collectors/dataarts.py: 作业 /status 的 nodes",
            "job_status",
        ),
        _number(
            "bad_nodes",
            "异常或停止节点数",
            "个",
            "所有返回节点状态均已识别时，统计 EXCEPTION 或 STOPPED；状态缺失或未知时保留为空。",
            "collectors/dataarts.py: 作业 /status 的 nodes[].status",
            "job_status",
        ),
    ],
    "dgc_batch_job": [
        _number(
            "run_minutes",
            "最新实例运行耗时",
            "分钟",
            "(endTime 或本轮时间 - startTime)/60000，保留 1 位小数；每作业只取 24 小时窗口内 planTime 最新实例。",
            "collectors/dataarts.py: 实例 startTime/endTime，按毫秒差换算",
            "instance_status",
            limitations=("缺少有效起止时间时保留未知；executeTime 的文档与样例单位冲突，不作备用耗时。",),
        ),
        _field(
            "p90_minutes",
            "近24小时成功实例 P90",
            "分钟",
            "近24小时同工作空间、同作业成功实例的实际耗时P90，使用nearest-rank；不足有效样本时为空。",
            "collectors/dataarts.py: instances/detail 的 startTime/endTime 毫秒差值",
            "number",
            ("instance_status",),
            limitations=("默认至少5条有效成功实例，是本地策略；不使用executeTime或上游未定义的p90_minutes字段。",),
            availability="derived",
        ),
    ],
    "mrs_job": [
        _number(
            "run_minutes",
            "节点运行时长",
            "分钟",
            "节点 endTime − startTime（毫秒时间戳）；节点运行中以采集时刻为结束；取作业最新一次实例。",
            "DataArts 作业实例详情 nodes[].startTime / endTime（§5.3.17）",
            "job_status",
            limitations=(
                "窗口默认 24 小时（接口上限 3 天）；起止时间缺失时为空，不把未知补零。",
                "只覆盖 DataArts 编排的 MRS 节点，不含在 MRS 控制台直接提交的作业。",
            ),
        ),
        _number(
            "retry_times",
            "失败重试次数",
            "次",
            "节点 retryTimes 原值。",
            "DataArts 作业实例详情 nodes[].retryTimes（§5.3.17）",
            "job_status",
        ),
    ],
    "gaussdb_instance": [
        _field("cpu_usage", "CPU 使用率", "%", "当前快照不采集；CPU 指标见原生巡检报告及其检查时间。",
               "GaussDB inspections/records/{record_id} 原生巡检指标", "number", availability="not_collected"),
        _field("mem_usage", "内存使用率", "%", "当前快照不采集；内存指标见原生巡检报告，不用配置容量推算。",
               "GaussDB inspections/records/{record_id} 原生巡检指标", "number", availability="not_collected"),
        _number("disk_usage", "磁盘使用率", "%", "原生 used×100/total；分母大于 0，保留两位小数。",
                "GET /gaussdb/v3/{project_id}/instances/{instance_id}/volume-usage", "disk",
                limitations=("原生 used/total 单位 GB；不是实例列表中未核实单位的 volume.size。",)),
        _number("disk_used_gb", "磁盘已用", "GB", "原生磁盘 used 字段，不混入台账配置容量。",
                "GaussDB volume-usage.used", "disk"),
        _number("disk_total_gb", "磁盘总量", "GB", "原生磁盘 total 字段，0 分母时使用率为空。",
                "GaussDB volume-usage.total", "disk"),
    ],
    "dcs_instance": [
        _number(
            "memory_usage",
            "缓存内存使用率",
            "%",
            "used_memory×100/max_memory，保留 1 位小数；分母必须存在且非 0。",
            "collectors/dcs.py: DCS v2 instances/statistic 或实例列表 used_memory/max_memory",
            "memory",
            limitations=("不依赖 CES；不是 used_memory/capacity，不能混用原始内存量与 GB 配置容量。",),
        ),
        _number(
            "used_memory_mb",
            "已用缓存内存",
            "MB",
            "DCS v2 used_memory 原值；8.5.1 原生实例和统计接口规定单位为 MB。",
            "collectors/dcs.py: used_memory，无单位转换",
            "memory",
            limitations=("不套用 Redis INFO 或其他监控系统同名字段的 byte 单位，不擅自再次换算。",),
        ),
        _number(
            "max_memory_mb",
            "可用内存上限",
            "MB",
            "DCS max_memory 原值，与 used_memory 使用同一单位，作为 memory_usage 分母。",
            "collectors/dcs.py: max_memory，无单位转换",
            "memory",
            limitations=("原生接口单位为 MB；不是 GB 规格 capacity，也不是连接数上限。",),
        ),
        _field(
            "cpu_usage", "CPU 使用率", "%",
            "当前不采集；DCS v2 used_cpu 是累计 CPU 秒数，不能当作百分比。",
            "DCS v2 instances/statistic: used_cpu，单位秒", "number",
            availability="not_collected",
            limitations=("保留旧展示键，采集时清空旧值，不补零或使用 AOM。",),
        ),
        _field("used_cpu_seconds", "CPU 累计时间", "秒", "V2 used_cpu 为用户态与内核态累计 CPU 时间，按原值保存。",
               "DCS v2 instances/statistic.used_cpu", "number",
               limitations=("不是 CPU 使用率；V1 同名字段的百分比语义未启用，不自动回退。",)),
        _field("input_kbps", "网络入速率", "kbit/s", "原生 input_kbps；历史窗口使用有效样本均值。",
               "DCS v2 instances/statistic.input_kbps", "number"),
        _field("output_kbps", "网络出速率", "kbit/s", "原生 output_kbps；历史窗口使用有效样本均值。",
               "DCS v2 instances/statistic.output_kbps", "number"),
        _field("keys", "数据条数", "个", "原生键数量；历史窗口保留末次值，不换算为速率。",
               "DCS v2 instances/statistic.keys", "integer"),
        _field("cmd_get_count", "GET 累计次数", "次", "原生累计计数；重置后的较小值保留，不据此计算 QPS。",
               "DCS v2 instances/statistic.cmd_get_count", "integer"),
        _field("cmd_set_count", "SET 累计次数", "次", "原生累计计数；重置后的较小值保留，不据此计算 QPS。",
               "DCS v2 instances/statistic.cmd_set_count", "integer"),
        _field(
            "connected_clients", "客户端连接数", "个",
            "当前不采集；原生实例统计接口没有此实例级字段。",
            "DCS v2 instances/statistic 契约未提供实例连接数", "number",
            availability="not_collected",
            limitations=("不把节点会话数拼成实例连接数，采集时清空旧值。",),
        ),
    ],
    "cce_workload": [
        _number(
            "pods",
            "当前 Pod 数",
            "个",
            "该集群/命名空间/类型/工作负载下，当前未标记删除的 PodState 数量。",
            "k8s/collector.py: Pod 快照汇总",
            "pod_restart",
        ),
        _number(
            "ready_pods",
            "就绪 Pod 数",
            "个",
            "Pod 至少有一个已返回容器状态，且所有返回容器均 ready 时计入。",
            "k8s/collector.py: containerStatuses[].ready 汇总",
            "pod_restart",
            limitations=("不是 Deployment 期望副本数；当前不输出 desired_replicas，不应据此计算缺失副本。",),
        ),
        _number(
            "restarts_24h",
            "近 24 小时重启事件计数",
            "次",
            "过去 24 小时 RESTART 与 OOM_KILLED 事件的 count 之和，包含 OOM。",
            "k8s/collector.py: PodEvent + RESTART_TYPES",
            "pod_restart",
            limitations=_EVENT_LIMIT,
        ),
        _number(
            "crashloop_pods",
            "持续 CrashLoop Pod 数",
            "个",
            "持续中的 CRASH_LOOP 事件按 Pod 名去重；同一 Pod 多容器只计一个。",
            "k8s/collector.py: ongoing CRASH_LOOP",
            "pod_crashloop",
            limitations=_EVENT_LIMIT,
        ),
        _number(
            "backpressure_min",
            "最长持续背压",
            "分钟",
            "当前持续背压事件从 first_time 到本轮的最长整分钟数，不是各 Pod 时长之和。",
            "k8s/collector.py: ongoing BACKPRESSURE，来自 K8s Event 或可选 Pod 指标探针",
            "pod_backpressure",
            limitations=(
                "未配探针且无匹配事件时的 0 仅表示未观测到背压；不证明业务吞吐正常。",
                "探针原始指标单位由探针配置决定，不能把所有探针认作百分比。",
            ),
        ),
    ],
}


_FACTS: dict[str, list[GuideField]] = {
    "ecs_server": [
        _state(
            "status",
            "ECS 原始状态",
            "如 ACTIVE、SHUTOFF、ERROR；保留上游状态，不推测操作系统或应用健康。",
            "ECS cloudservers/detail status",
            "instance_status",
        ),
        _state(
            "status_text",
            "ECS 中文状态",
            "按 catalog.ECS_STATUS 翻译；未知值保留原文。",
            "collectors/ecs.py + services/catalog.py",
            "instance_status",
        ),
        _state(
            "power_state",
            "电源状态",
            "OS-EXT-STS:power_state 整数映射为 RUNNING、SHUTDOWN 等；未知值保留。",
            "ECS OS-EXT-STS:power_state",
            "instance_status",
        ),
        _state(
            "task_state",
            "任务状态",
            "ECS 当前任务状态；无任务时规则事实为 ''，属性中为 null。",
            "ECS OS-EXT-STS:task_state",
            "instance_status",
        ),
        _field(
            "transitional",
            "是否过渡状态",
            None,
            "status 属于 BUILD、REBOOT、HARD_REBOOT、RESIZE、VERIFY_RESIZE、REVERT_RESIZE、MIGRATING、REBUILD。",
            "collectors/ecs.py: TRANSITIONAL",
            "boolean",
            ("instance_status",),
        ),
    ],
    "dgc_realtime_job": [
        _state(
            "status",
            "作业状态",
            "作业 /status 原始状态，如 NORMAL、EXCEPTION、STOPPED、STARTING、STOPPING。",
            "DataArts 作业状态接口",
            "job_status",
        ),
        _state(
            "job_status",
            "节点所属作业状态",
            "与节点观测绑定的父作业状态；节点事实不使用 job_status 检查项中的 status 键。",
            "DataArts /status.status",
            "node_status",
        ),
        _state(
            "node_status",
            "单节点状态",
            "逐节点返回的原始状态，sub_object 为节点名；不是全作业 bad_nodes 汇总。",
            "DataArts /status.nodes[].status",
            "node_status",
        ),
    ],
    "dgc_batch_job": [
        _state(
            "last_status",
            "最新实例状态",
            "按 planTime 最新实例的原始状态，如 fail、running、running-exception、success；注意大小写。",
            "DataArts instances/detail.status",
            "instance_status",
        ),
        _number(
            "p90_minutes",
            "耗时基线事实",
            "分钟",
            "近24小时同工作空间和作业的成功实例耗时P90；缺失时为空，真实零时长保留为0。",
            "collectors/dataarts.py: 完整 instances/detail 清单中的成功实例 startTime/endTime",
            "instance_status",
            limitations=("按独立instanceId去重，默认至少5个有效样本；不跨工作空间混合，不用失败或运行中的实例。",),
        ),
    ],
    "mrs_cluster": [
        _state(
            "cluster_state",
            "集群状态",
            "clusterState 原值；内置规则针对 running、abnormal、failed、frozen，实际枚举以局点为准。",
            "MRS V1.1 cluster_infos.clusterState",
            "cluster_status",
        ),
    ],
    "mrs_job": [
        _state(
            "node_status",
            "节点状态",
            "MRS 类节点状态：waiting、running、success、fail、skip、pause、manual-stop；未知值不判为正常。",
            "DataArts 作业实例详情 nodes[].status（表 5-182）",
            "job_status",
        ),
        _state(
            "job_status",
            "所属实例状态",
            "节点所在作业实例的状态：waiting、running、success、fail、running-exception、pause、manual-stop。",
            "DataArts 作业实例列表 instances[].status（表 5-178）",
            "job_status",
        ),
    ],
    "gaussdb_instance": [
        _state(
            "status",
            "实例状态",
            "GaussDB 实例 status 原值；已核实 ACTIVE、BUILD_FAILED、FAILED、STORAGE FULL、SHUTDOWN 等。"
            "FROZEN 为旧部署兼容值，未知状态不判为正常。",
            "GaussDB gaussdb_instances_path()；默认 /gaussdb/v3.3/{project_id}/instances",
            "instance_status",
        ),
        _field("cpu_usage", "CPU 使用率（实时未采集）", "%", "旧规则兼容，不把原生巡检历史值当当前观测。",
               "GaussDB 原生巡检报告", "number", ("cpu",), availability="not_collected"),
        _field("mem_usage", "内存使用率（实时未采集）", "%", "旧规则兼容，不把配置容量当内存使用率。",
               "GaussDB 原生巡检报告", "number", ("memory",), availability="not_collected"),
    ],
    "dcs_instance": [
        _state(
            "status",
            "实例状态",
            "DCS status 原值；已核实 RUNNING、ERROR、CREATEFAILED、CREATING、RESTARTING、EXTENDING、RESTORING。"
            "CLOSED/FROZEN/UPGRADING 为旧部署兼容值；未知状态不判为正常。",
            "DCS /v2/{project_id}/instances",
            "instance_status",
        ),
        _field("cpu_usage", "CPU 使用率（未采集）", "%",
               "旧规则兼容字段；原生 v2 仅有累计 CPU 秒数，当前不产生 cpu 观测。",
               "DCS v2 instances/statistic 不提供 CPU 百分比", "number", ("cpu",),
               availability="not_collected"),
        _field("connected_clients", "客户端连接数（未采集）", "个",
               "旧规则兼容字段；当前不产生 connections 观测。",
               "DCS v2 instances/statistic 不提供实例连接数", "number", ("connections",),
               availability="not_collected"),
        _field("max_clients", "连接数上限（旧规则）", "个",
               "实例属性仍保留原生 max_clients；没有连接数时不产生 connections 观测。",
               "DCS instances.max_clients", "number", ("connections",),
               availability="not_collected"),
    ],
    "cce_workload": [
        _number(
            "restarts_10m",
            "近 10 分钟重启计数",
            "次",
            "最近 10 分钟 RESTART/OOM_KILLED 事件 count 合计。",
            "k8s/collector.py: cnt(RESTART_TYPES)",
            "pod_restart",
            limitations=_EVENT_LIMIT,
        ),
        _number(
            "restarts_1h",
            "近 1 小时重启计数",
            "次",
            "最近 1 小时 RESTART/OOM_KILLED 事件 count 合计。",
            "k8s/collector.py: cnt(RESTART_TYPES)",
            "pod_restart",
            limitations=_EVENT_LIMIT,
        ),
        _number(
            "oom_1h",
            "近 1 小时 OOM 计数",
            "次",
            "最近 1 小时 OOM_KILLED 事件 count 合计，非唯一 Pod 数；可来自 Pod 上次 OOMKilled 退出或匹配 K8s OOM 事件。",
            "k8s/collector.py: cnt(OOM_KILLED)",
            "pod_oom",
            limitations=_EVENT_LIMIT,
        ),
        _state(
            "status",
            "工作负载就绪概况",
            "如 '2/3 Ready'，由当前就绪数/当前 Pod 数拼接；不是 Kubernetes 原生 Deployment phase。",
            "k8s/collector.py: 工作负载汇总",
            "pod_crashloop",
        ),
        _number(
            "backpressure_pods",
            "持续背压 Pod 数",
            "个",
            "持续背压事件按 Pod 名去重后的数量。",
            "k8s/collector.py: ongoing BACKPRESSURE",
            "pod_backpressure",
            limitations=_EVENT_LIMIT,
        ),
        _number(
            "evicted_1h",
            "近 1 小时驱逐事件计数",
            "次",
            "最近 1 小时 EVICTED 事件 count 之和，可能来自状态与 K8s Event；不是去重后的 Pod 数。",
            "k8s/collector.py: cnt(EVICTED)",
            "pod_evicted",
            limitations=_EVENT_LIMIT + ("旧内置规则文案单位为“个”，但实际聚合的是事件 count。",),
        ),
    ],
}


def _attribute(
    key: str,
    label: str,
    meaning: str,
    source: str,
    unit: str | None = None,
    data_type: str = "string",
    *,
    limitations: tuple[str, ...] = (),
) -> GuideField:
    return _field(key, label, unit, meaning, source, data_type, limitations=limitations)


_ATTRS: dict[str, list[GuideField]] = {
    "ecs_server": [
        _attribute("flavor", "规格编码", "flavor.id；规格标识不等于 CPU 使用率。", "ledger.fetch_ecs"),
        _attribute("os", "镜像/操作系统名称", "metadata.image_name，不是系统实时版本探测。", "ledger.fetch_ecs"),
        _attribute("eip", "公网 IP", "addresses 中首个 floating 地址。", "ledger.fetch_ecs"),
        _attribute("fixed_ips", "私网地址", "原生清单的全部 fixed 地址；重复地址不用于猜测指标归属。",
                   "ledger.fetch_ecs", data_type="array"),
        _attribute("fixed_ports", "私网端口映射", "原生端口 ID 到固定地址的映射，用于消除重叠地址的归属歧义。",
                   "ledger.fetch_ecs", data_type="object"),
        _attribute("vpc_id", "VPC ID", "metadata.vpc_id 原值。", "ledger.fetch_ecs"),
        _attribute("charging", "计费模式", "metadata.charging_mode；前端将 0/1 展示为按需/包周期。", "ledger.fetch_ecs"),
        _attribute(
            "metrics_source",
            "当前选择指标源",
            "PROMETHEUS/NONE 或显式兼容 CES；AOM 已停用，旧来源不能作为当前采样。",
            "metrics/ecs_readings.py: ecs_source(tenant, policy)",
        ),
        _attribute("ecs_state_checked_at", "状态查询时间", "独立状态巡检完成查询的 UTC 时间，不是使用率采样时间。",
                   "collectors/ecs.py"),
        _attribute("ecs_monitoring", "指标采样证据", "各项真实采样时间、采集时间、来源及缺失诊断；配置摘要用于隔离来源变化。",
                   "collectors/ecs_metrics.py", data_type="object"),
        _attribute("boot_observed_at", "开机时间观测时间", "开机秒数对应的真实上游样本时间。",
                   "collectors/ecs_metrics.py"),
        _attribute(
            "task_state", "任务状态", "当前 ECS 扩展任务状态；无值为 null。", "collectors/ecs.py: OS-EXT-STS:task_state"
        ),
        _attribute("power_state", "电源状态", "扩展电源状态的映射文本。", "collectors/ecs.py: OS-EXT-STS:power_state"),
        _attribute(
            "boot_time", "开机时间", "Prometheus 开机秒数转 UTC ISO 8601。", "collectors/ecs.py: node_boot_time_seconds"
        ),
        _attribute(
            "boot_ts",
            "开机时间戳",
            "用于比对重启的 Unix 时间戳。",
            "collectors/ecs.py: node_boot_time_seconds",
            "秒",
            "integer",
        ),
    ],
    "cce_cluster": [
        _attribute("version", "Kubernetes 版本", "spec.version。", "ledger.fetch_cce"),
        _attribute(
            "node_count",
            "节点清单数量",
            "集群 nodes 查询响应 items 数量，不是健康节点数。",
            "ledger.fetch_cce",
            "个",
            "integer",
        ),
        _attribute("flavor", "集群规格", "spec.flavor；不是节点机器规格。", "ledger.fetch_cce"),
        _attribute("node_source", "节点清单状态", "nodes 查询结果的可用/失败标记；不可用时节点数保持未知。",
                   "ledger.fetch_cce"),
        _attribute("reason", "集群异常原因", "管理面 status.reason；不把任务 jobID 当健康状态。", "cce_cluster_health"),
        _attribute("message", "集群异常说明", "管理面 status.message；仅在受限视图中查看，不写入日志或告警。",
                   "cce_cluster_health"),
        _attribute("endpoints", "管理面访问地址", "来自 status.endpoints，保留 URL 协议、主机与端口；只读展示。",
                   "cce_cluster_health"),
        _attribute("job_id", "管理面任务 ID", "status.jobID 是短时操作上下文，不作为集群健康状态。",
                   "cce_cluster_health"),
        _attribute("cluster_type", "集群类型", "spec.type 原值。", "ledger.fetch_cce"),
    ],
    "evs_volume": [
        _attribute("encrypted_reported", "加密字段原始值", "文档将 encrypted 标为不支持；原值不作为实际加密判断。",
                   "ledger.fetch_evs", data_type="boolean"),
        _attribute("volume_type", "磁盘类型", "volume_type 原值。", "ledger.fetch_evs"),
        _attribute("attached_to", "挂载云服务器", "展示全部有效挂载关系，优先映射为 ECS 名称。", "ledger.fetch_evs"),
        _attribute("device", "挂载设备", "全部有效 attachment.device，不是文件系统挂载点。", "ledger.fetch_evs"),
        _attribute("attachments", "挂载关系", "上游明确返回的挂载服务器、设备和挂载 ID。", "ledger.fetch_evs",
                   data_type="array"),
        _attribute(
            "bootable",
            "是否系统盘",
            "仅明确 true/false 才显示是/否；缺失或非法值保持未知。",
            "ledger.fetch_evs",
        ),
        _attribute(
            "encrypted",
            "是否加密",
            "未确认该局点支持 encrypted 时为空，不能把保留字段的 false 解释为未加密。",
            "ledger.fetch_evs",
            data_type="boolean",
        ),
    ],
    "vpc": [
        _attribute("cidr", "VPC 网段", "vpcs.cidr；不是使用率或剩余 IP 数。", "ledger.fetch_vpc"),
        _attribute(
            "subnet_count", "关联子网数", "同次同步子网列表中 vpc_id 匹配该 VPC 的条目数。", "ledger.fetch_vpc", "个", "integer"
        ),
        _attribute("description", "描述", "上游 description 原文。", "ledger.fetch_vpc"),
    ],
    "vpc_subnet": [
        _attribute("cidr", "子网网段", "子网 cidr；当前未扣除保留地址计算可分配 IP。", "ledger.fetch_subnet"),
        _attribute("gateway", "网关地址", "gateway_ip 原值。", "ledger.fetch_subnet"),
        _attribute("vpc_name", "所属 VPC", "本次同步名称缓存命中时为名称，否则保留 VPC ID。", "ledger.fetch_subnet"),
        _attribute("dhcp", "DHCP 开关", "dhcp_enable 原值。", "ledger.fetch_subnet", data_type="boolean"),
    ],
    "eip": [
        _attribute(
            "bandwidth", "配置带宽", "bandwidth_size 原值；这是配置上限，不是实时吞吐。", "ledger.fetch_eip", "Mbit/s", "number"
        ),
        _attribute("share_type", "带宽类型", "bandwidth_share_type 原值。", "ledger.fetch_eip"),
        _attribute("port_id", "关联端口 ID", "以原生端口关系匹配绑定资源；地址存在歧义时不猜测。", "ledger.fetch_eip"),
        _attribute("bandwidth_name", "带宽名称", "bandwidth_name 原值。", "ledger.fetch_eip"),
        _attribute(
            "bound_to",
            "绑定对象",
            "按 private_ip_address 在本次 ECS/ELB IP 缓存中匹配；未命中时保留私网 IP。",
            "ledger.fetch_eip",
        ),
    ],
    "elb": [
        _attribute("guaranteed", "预留字段原值", "原生文档将 guaranteed 说明为预留字段，不据此推定物理独享。",
                   "ledger.fetch_elb", data_type="boolean"),
        _attribute("eip", "公网 IP", "eips 中首个 eip_address；缺失不代表故障，可能是内网负载均衡。", "ledger.fetch_elb"),
        _attribute(
            "listeners",
            "监听器引用数",
            "loadbalancer.listeners 数组长度；不是实时活跃连接数。",
            "ledger.fetch_elb",
            "个",
            "integer",
        ),
        _attribute(
            "members",
            "返回后端成员数",
            "member_count 原值，缺失时为空；不是健康成员数。",
            "ledger.fetch_elb",
            "个",
            "integer",
        ),
        _attribute("vpc_id", "所属 VPC ID", "vpc_id 原值。", "ledger.fetch_elb"),
        _attribute("vip_port_id", "VIP 端口 ID", "原生 VIP 端口标识，用于关联 EIP。", "ledger.fetch_elb"),
        _attribute("provisioning_status", "配置状态", "配置状态原值；不能用 ACTIVE 代替缺失的运行状态。", "ledger.fetch_elb"),
    ],
    "gaussdb_instance": [
        _attribute("vcpus", "规格 vCPU", "flavor_info.vcpu 的核数，不将节点数作为 CPU 使用率。",
                   "ledger.gaussdb_attrs", "核", "integer"),
        _attribute("gaussdb_monitoring", "磁盘采样时间与来源",
                   "原生 v3 磁盘用量响应接收时间、状态与有效期；台账更新时间不替代采样时间。",
                   "collectors/gaussdb.py", data_type="object"),
        _attribute("memory_size", "内存原值", "flavor_info.mem；列表表格未明确单位，不直接写 GB。",
                   "ledger.gaussdb_attrs", data_type="number"),
        _attribute("volume_size", "磁盘原值", "volume.size；未确认单位时保留原值而非容量 GB。",
                   "ledger.gaussdb_attrs", data_type="number"),
        _attribute("memory_unit_verified", "内存单位已核实", "是否有当前契约的单位证据。",
                   "ledger.gaussdb_attrs", data_type="boolean"),
        _attribute("volume_unit_verified", "磁盘单位已核实", "是否有当前契约的单位证据。",
                   "ledger.gaussdb_attrs", data_type="boolean"),
        _attribute("metrics_source", "当前选择指标源", "GAUSSDB 原生状态和磁盘接口，CPU/内存巡检报告单独展示。",
                   "collectors/gaussdb.py: metrics_source=GAUSSDB"),
        _attribute("version", "GaussDB 版本", "datastore.version 前加 GaussDB，缺失时为 null。", "ledger.gaussdb_attrs"),
        _attribute("type", "部署形态", "Ha/Independent/Distributed 映射为主备/独立部署/分布式。", "ledger.gaussdb_attrs"),
        _attribute("flavor", "规格编码", "flavor_ref 原值。", "ledger.gaussdb_attrs"),
        _attribute(
            "volume_gb",
            "配置存储容量",
            "兼容保留键；当前列表未核实 volume.size 的单位，因此 volume_gb 和 storage_gb 为空，原值另存 volume_size。",
            "ledger.gaussdb_attrs",
            "GB",
            "number",
        ),
        _attribute(
            "nodes",
            "节点状态清单",
            "每节点仅保存 name、role、status；当前未创建独立节点规则检查项。",
            "collectors/gaussdb.py: instances[].nodes",
            data_type="array",
        ),
    ],
    "dcs_instance": [
        _attribute("metrics_source", "当前统计来源", "DCS 原生接口获取记录，不请求 AOM 或 CES。",
                   "collectors/dcs.py: metrics_source=DCS"),
        _attribute("dcs_monitoring", "原生统计时间与来源", "V2 接口、响应接收时间、过期阈值及错误提示；源端未提供采样时间。",
                   "collectors/dcs.py: dcs_monitoring", data_type="object"),
        _attribute("engine", "缓存引擎", "engine 原值。", "DCS instances / ledger.fetch_dcs"),
        _attribute("engine_version", "引擎版本", "engine_version 原值。", "DCS instances / ledger.fetch_dcs"),
        _attribute(
            "capacity",
            "配置缓存容量",
            "台账 _num(capacity)，并写入 memory_gb；不是已用量。",
            "DCS instances.capacity",
            "GB",
            "number",
        ),
        _attribute("spec_code", "规格编码", "spec_code 原值。", "DCS instances"),
        _attribute("ip", "实例访问 IP", "巡检属性 ip；台账还保存 top-level ip。", "DCS instances.ip"),
        _attribute("port", "访问端口", "port 原值，无吞吐含义。", "DCS instances.port", data_type="integer"),
        _attribute(
            "max_memory",
            "原始内存上限",
            "上游 max_memory，与 metrics.max_memory_mb 同源；MB 是当前适配约定。",
            "DCS instances.max_memory",
            "MB",
            "number",
        ),
        _attribute(
            "max_clients",
            "最大连接数",
            "max_clients 原值；未给出时为空，禁止当 0 分母。",
            "DCS instances.max_clients",
            "个",
            "number",
        ),
    ],
    "mrs_cluster": [
        _attribute("version", "集群版本", "clusterVersion 原值。", "MRS cluster_infos / ledger.fetch_mrs"),
        _attribute(
            "nodes", "配置节点数", "totalNodeNum 原值；不是健康节点数。", "MRS cluster_infos.totalNodeNum", "个", "number"
        ),
        _attribute("components", "组件清单", "componentList[].componentName 拼接；不是组件健康探测结果。", "ledger.fetch_mrs"),
    ],
    "dws_cluster": [
        _attribute("version", "DWS 版本", "version 原值。", "ledger.fetch_dws"),
        _attribute("node_count", "配置节点数", "number_of_node 原值。", "ledger.fetch_dws", "个", "number"),
        _attribute("node_type", "节点规格", "node_type 原值；不在本地推算 CPU/内存/磁盘容量。", "ledger.fetch_dws"),
    ],
    "dgc_realtime_job": [
        _attribute(key, label, meaning, "collectors/dataarts.py: 作业列表与 /status")
        for key, label, meaning in (
            ("workspace", "工作空间", "采集配置的工作空间；资源 ID 以 workspace/name 区分。"),
            ("owner", "作业负责人", "owner 原值，不等于平台资源责任人。"),
            ("priority", "优先级", "priority 原值，未假设数值等级或大小方向。"),
            ("path", "作业路径", "path 原值。"),
            ("job_type", "作业类型", "jobType 原值。"),
            ("job_status", "作业状态", "状态响应 status 原值。"),
            ("engine", "执行引擎", "engine 或 jobEngine 原值；缺失时不推测。"),
            ("start_time", "开始时间原值", "startTime 原样保存；局点格式与单位待核验。"),
            ("end_time", "结束时间原值", "endTime 原样保存；局点格式与单位待核验。"),
        )
    ]
    + [
        _attribute(
            "nodes",
            "节点状态清单",
            "每节点 name/status 数组，与 metrics.nodes 的数量区分。",
            "collectors/dataarts.py",
            data_type="array",
        ),
        _field(
            "restarts_24h",
            "实时作业重启（展示保留键）",
            "次",
            "前端有此展示，但当前 DataArts 采集器不输出该字段。",
            "frontend/views/ledger/columns.ts；当前无采集来源",
            "number",
            availability="not_collected",
            limitations=("不得把前端旧版的 ?? 0 兜底当成已测量重启次数。",),
        ),
    ],
    "dgc_batch_job": [
        _attribute("batch_baseline", "批作业基线", "24小时窗口、有效/最低样本数及基线状态；窗口时间为Unix毫秒。",
                   "collectors/dataarts.py", data_type="object"),
        _attribute("instance_type", "实例类型原值", "instanceType；可能包含实时节点关联调度实例，不强称独立批作业。",
                   "collectors/dataarts.py", data_type="integer"),
        _attribute("submitted_time", "提交时间原值", "submitTime，Unix 毫秒时间戳；未提供时为空。", "collectors/dataarts.py",
                   "毫秒", "integer"),
    ] + [
        _attribute(key, label, meaning, "collectors/dataarts.py: instances/detail", unit, "number" if unit else "string")
        for key, label, meaning, unit in (
            ("workspace", "工作空间", "采集工作空间；不是项目 ID。", None),
            ("instance_id", "最新实例 ID", "instanceId 原值；每次新执行可能改变。", None),
            ("job_type", "作业类型", "jobType 原值。", None),
            ("engine", "执行引擎", "engine 或 jobEngine 原值。", None),
            ("instance_status", "实例状态", "最近计划实例 status。", None),
            ("plan_time", "计划时间", "planTime 原值，也是选取最新实例的排序键；当前适配按毫秒时间窗查询。", "毫秒"),
            ("start_time", "开始时间", "startTime 原值，耗时计算按毫秒。", "毫秒"),
            ("end_time", "结束时间", "endTime 原值，未结束时可能为空。", "毫秒"),
        )
    ]
    + [
        _field(
            "schedule",
            "调度计划",
            None,
            "资源目录保留的调度展示字段；当前批实例采集器没有查询作业调度定义。",
            "services/catalog.py 展示列，当前采集器未采集",
            "string",
            availability="not_collected",
            limitations=("空值不表示作业没有调度；应在 DGC 作业配置中核对。",),
        ),
    ],
    "mrs_job": [
        _attribute(key, label, meaning, "collectors/mrs.py: DataArts 作业实例列表 + 实例详情")
        for key, label, meaning in (
            ("workspace", "工作空间", "DataArts 工作空间 ID，同名作业按工作空间区分。"),
            ("job_name", "所属作业", "节点所在的 DataArts 作业名称。"),
            ("node_name", "节点名称", "nodeName 原值；与作业名共同组成资源 ID。"),
            ("node_type", "节点类型", "MRSSpark、MapReduce、MRSFlinkJob 或 MRSHetuEngine。"),
            ("instance_id", "实例 ID", "作业实例 ID；变化即新一轮执行，状态持续时间重新计时。"),
            ("job_status", "实例状态", "所属作业实例的 status 原值。"),
            ("node_status", "节点状态", "节点 status 原值。"),
            ("plan_time", "计划时间", "实例 planTime（毫秒时间戳）。"),
            ("start_time", "开始时间", "节点 startTime（毫秒时间戳）。"),
            ("end_time", "结束时间", "节点 endTime（毫秒时间戳），运行中为空。"),
            ("retry_times", "失败重试次数", "节点 retryTimes 原值。"),
            ("log_path", "日志路径", "节点 logPath 原值，只作展示，平台不读取日志内容。"),
        )
    ],
    "cce_workload": [
        _attribute(
            key,
            label,
            meaning,
            "k8s/collector.py: 工作负载汇总",
            "个" if key in ("pods", "ready_pods") else None,
            "integer" if key in ("pods", "ready_pods") else "string",
        )
        for key, label, meaning in (
            ("cluster", "集群名称", "CCE metadata.name。"),
            ("cluster_id", "集群 ID", "CCE metadata.uid。"),
            ("namespace", "命名空间", "Pod metadata.namespace；排除项按任务参数 exclude_namespaces。"),
            ("kind", "工作负载类型", "按 ownerReferences 推断，ReplicaSet 映射 Deployment，特定 Job 名推断 CronJob。"),
            ("workload", "工作负载名称", "按 ownerReferences/名称推断，无法关联时可能为 Pod 自身。"),
            ("pods", "当前 Pod 数", "与 metrics.pods 同一快照汇总。"),
            ("ready_pods", "就绪 Pod 数", "与 metrics.ready_pods 同一快照汇总。"),
        )
    ],
}


# 只登记 fetcher 实际赋值的容量列，不按规格名称臆造资源容量。
_CAPACITY: dict[str, list[GuideField]] = {
    "ecs_server": [
        _attribute(
            "vcpus",
            "分配 vCPU",
            "int(flavor.vcpus)，缺失当前回退 0，需区分真实配置与缺字段。",
            "ledger.fetch_ecs",
            "vCPU",
            "integer",
        ),
        _attribute("memory_gb", "分配内存", "flavor.ram / 1024，保留 1 位小数。", "ledger.fetch_ecs: _gb", "GB", "number"),
        _attribute(
            "storage_gb",
            "挂载 EVS 容量合计",
            "将 attachments 包含此 ECS 的 EVS size 累加；不含未在 EVS 清单里的本地盘。",
            "ledger.fetch_ecs: _evs_raw",
            "GB",
            "number",
        ),
    ],
    "evs_volume": [
        _attribute(
            "storage_gb", "云盘配置容量", "size 转数值；缺失或单位无依据时保留未知，不是已用空间。",
            "ledger.fetch_evs", "GB", "number"
        )
    ],
    "gaussdb_instance": [
        _attribute("vcpus", "规格 vCPU", "flavor_info.vcpu 核数，不乘节点数冒充集群总量。",
                   "ledger.fetch_gaussdb", "核", "integer"),
        _attribute("storage_gb", "配置存储", "字段保留；列表未明确容量单位时不填 GB。",
                   "ledger.fetch_gaussdb", "GB", "number"),
    ],
    "dcs_instance": [
        _attribute(
            "memory_gb",
            "配置缓存容量",
            "capacity 转数值；与使用率的 max_memory 分母分开理解。",
            "ledger.fetch_dcs",
            "GB",
            "number",
        )
    ],
}


# 每类型的排查顺序和操作建议；此处不设置业务阈值，也不执行 HCS 变更。
_CONTENT: dict[str, tuple[str, list[str], list[str], list[str]]] = {
    "ecs_server": (
        "实例状态、可选使用率与观测重启相结合，区分基础设施状态和操作系统压力。",
        [
            "先核对预期运行状态、ECS 原始状态与电源/任务状态，再看 CPU、内存、最满分区。",
            "使用率为空先查租户指标源、CES Agent 或 Prometheus 目标/IP 关联，"
            "再查业务；开机时间变化用于辅助判断操作系统内重启。",
            "重启高时对照资源变更、宿主 HA 和系统日志，区分人工操作、内核故障与计划维护。",
        ],
        [
            "生产关键实例标记关键资源并维护责任人，计划关机先设置预期状态或维护窗口。",
            "对最满分区排查日志、临时文件和 inode，扩容前确认真正的文件系统瓶颈；不要拿 EVS 配置容量当可用空间。",
        ],
        ["ACTIVE 只代表 ECS 运行状态，不代表应用请求成功；CES/Prometheus 查询不是应用 QPS。", _STALE],
    ),
    "cce_cluster": (
        "CCE 集群清单和节点规模；容器异常由独立的 cce_workload 指南解释。",
        [
            "核对集群 phase、版本、规格与节点清单返回量；节点数异常先确认查询权限和响应完整性。",
            "集群可用但容器异常时，转到工作负载/容器事件按命名空间定位，不把集群状态当所有 Pod 健康。",
        ],
        [
            "升级前核对版本兼容、节点容量和维护窗口；同步台账后复核版本与节点数。",
            "K8s API 访问单独核对认证与最小只读权限，集群清单权限不保证能读取 Pod/Event。",
        ],
        ["独立 CCE 管理面健康快照包含节点/节点池；detail=true 容量字段是配置汇总，不代表使用率。",
         "节点 lastProbeTime 超过 5 分钟显示未知；节点池 Scalable=False 不单独判为故障。",
         "集群 CPU/内存实时样本仅在租户逐项启用且原生 metrics.k8s.io 可访问时展示，不支持 AOM。"],
    ),
    "evs_volume": (
        "EVS 配置容量、盘类型和首条挂载关系，用于容量台账与挂载核对。",
        [
            "按磁盘状态、类型和 attached_to/device 核对挂载，设备名不是文件系统路径。",
            "容量增长先区分云盘配置、分区大小和文件系统可用量；本页只有配置容量。",
        ],
        [
            "扩容和卸载前由运维核对备份、应用依赖及多挂载关系；平台不执行变更。",
            "闲置盘先确认业务归属和快照/备份要求，再走释放审批，不能仅以未显示挂载判定可删。",
        ],
        ["只展示第一条 attachment；没有 IOPS、吞吐、时延或文件系统使用率采集。"],
    ),
    "vpc": (
        "VPC 网段、关联子网数量和描述，用于网络规划核对。",
        [
            "按 CIDR 和所属项目核对地址规划，子网数取本次清单匹配结果。",
            "跨网段不通需进一步查路由、安全组、ACL 与 DNS；VPC 清单状态不能证明连通性。",
        ],
        [
            "网段变更前核对对等连接、VPN 和重叠 CIDR，保留用途与责任人。",
            "VPC 与子网应在同轮同步中核对，防止把局部失败引起的数量缺口当成删除。",
        ],
        ["未采集实时流量、丢包、地址利用率或网络探测结果。"],
    ),
    "vpc_subnet": (
        "子网 CIDR、网关、DHCP 与所属 VPC，辅助地址分配排查。",
        [
            "先核对所属 VPC 和网段，随后核对网关/DHCP 与云服务器私网地址。",
            "无法分配地址时在上游核验已分配/保留 IP，当前不从 CIDR 推算剩余可用 IP。",
        ],
        [
            "地址规划保留扩容余量，变更 DHCP/网关前核对现有实例依赖。",
            "所属 VPC 显示为 ID 时先查本轮 VPC 同步是否成功，避免误认成孤立子网。",
        ],
        ["无实时地址消耗、流量或丢包指标；名称关联失败不等于 VPC 已不存在。"],
    ),
    "eip": (
        "公网 IP、配置带宽和绑定对象清单，区分带宽配额与真实网络吞吐。",
        [
            "核对 EIP 状态、带宽类型、绑定私网 IP；绑定对象来自当前 ECS/ELB 清单匹配。",
            "带宽不足要结合上游流量监控确认方向与高峰，本页 Mbit/s 是配置值，不是每秒实际请求数。",
        ],
        [
            "共享带宽需按整体用量规划；调大带宽前排查异常流量与业务峰值。",
            "看似未绑定的 EIP 先核验关联缓存和上游实际绑定，再考虑释放以免中断服务。",
        ],
        ["当前没有公网流量、入出方向速率、丢包、连接数 metrics。"],
    ),
    "elb": (
        "负载均衡配置、VIP、公网 IP、监听器引用与返回的成员数。",
        [
            "先核对 operating_status/provisioning_status、VIP 与 VPC，再在上游检查监听器和后端健康。",
            "members 是返回成员数量，不是可用成员数；业务故障还要核对证书、健康检查和目标端口。",
        ],
        [
            "变更后端或证书前检查健康检查配置和回滚方案，结合应用错误率验证。",
            "把配置容量与真实并发/QPS分开规划，不能从监听器个数推算负载。",
        ],
        ["没有后端逐个健康状态、请求 QPS、HTTP 错误率或时延采集；member_count 缺失时旧实现回退 0。"],
    ),
    "gaussdb_instance": (
        "原生实例状态、磁盘使用量、慢 SQL、实时会话及原生巡检报告。",
        [
            "异常先看实例 status、节点和组件状态；磁盘按原生 volume-usage 的 GB 用量查询。",
            "磁盘高时查业务数据、日志、临时文件与容量增长，CPU/内存高再查慢 SQL、会话和执行计划。",
        ],
        [
            "CPU、内存按原生巡检报告的单位、时间与节点展示；缺少报告时明确无数据。",
            "变更容量和清理数据均应由授权 DBA 评估备份与恢复窗口；只读巡检不做自动清理。",
        ],
        ["会话与慢 SQL 仅查询，不提供终止会话、执行 SQL 或扩容；巡检报告不能当实时性能值。", _STALE],
    ),
    "dcs_instance": (
        "实例状态、内存、网络、CPU 累计时间及命令计数；历史仅包含实际保存的原生记录。",
        [
            "先核对状态、used_memory/max_memory；内存高排查大 Key、无过期 Key、淘汰策略与容量增长。",
            "CPU 使用率、连接异常需在上游核对；V2 提供 CPU 累计秒，不提供 CPU% 或实例级连接数。",
        ],
        [
            "按业务访问模式设置 TTL 与淘汰策略，避免将 KEYS/大范围命令作为常规巡检操作。",
            "采集、历史和导出统一使用 DCS 原生样本；缺失不当 0，used_cpu 累计秒不作为 CPU 使用率。",
        ],
        ["memory_usage 的分母为 max_memory，不是配置 capacity；实例接口内存无需 CES。",
         "新鲜度按本地收到响应的时间计算，不能证明源端采样延迟；旧快照和过期值不作当前统计。", _STALE],
    ),
    "mrs_cluster": (
        "MRS 集群状态、版本、配置节点数和组件清单。",
        [
            "状态异常/冻结时先核对集群操作与上游控制台，再看节点与组件服务详情。",
            "作业失败转到 mrs_job；组件名称出现不表示 HDFS/YARN 等服务健康。",
        ],
        [
            "维护、扩缩容和升级前核对组件依赖及作业窗口，避免把计划操作作为突发异常。",
            "容量与队列规划另取组件真实监控；totalNodeNum 只能证明返回的配置规模。",
        ],
        ["当前未采集 HDFS 剩余空间、YARN 内存/队列使用率、组件健康或节点负载。"],
    ),
    "dws_cluster": (
        "DWS 集群状态、版本、节点规格和配置节点数清单。",
        [
            "先核对集群状态与节点数量变化，节点规格字符串不在本地解析为资源容量。",
            "查询慢需进一步在上游查慢 SQL、锁、数据倾斜与执行计划，本页清单无法判定 SQL 健康。",
        ],
        ["扩容前评估数据重分布、备份、业务低峰和回滚路径。", "容量治理结合真实存储趋势和查询负载，不只以节点数设告警。"],
        ["没有 DWS 磁盘使用率、连接数、查询耗时或独立巡检 target。"],
    ),
    "dgc_realtime_job": (
        "DataArts 实时作业与逐节点状态，按工作空间和作业名区分。",
        [
            "先判断作业是否应运行，再区分作业 EXCEPTION/STOPPED 与父作业 NORMAL 但节点异常。",
            "状态为空或工作空间局部失败时先查本轮运行记录、权限与节点状态响应；列表成功不代表明细成功。",
        ],
        [
            "关键实时链路维护上下游依赖和责任人；启动/停止持续时间结合本地状态历史评估。",
            "背压与重启应从实际引擎或容器事件补充证据，不能从节点总数推导。",
        ],
        ["当前没有 DataArts 实时作业重启、处理吞吐或背压指标；attrs.restarts_24h 是前端遗留键。", _STALE],
    ),
    "dgc_batch_job": (
        "DataArts 最近 24 小时内每作业最新计划实例的状态和运行时间。",
        [
            "先看最新实例 ID、planTime 和 last_status；同名作业在不同工作空间分开处理。",
            "运行超时先核对 start/end 是否存在，再查调度依赖、数据量、队列和引擎日志。",
        ],
        [
            "根据业务周期配置有依据的 SLA；没有实测历史基线时不评估超 P90。",
            "没有窗口内实例可能是当天未调度或采集失败，不应自动标记执行成功；计划停跑应维护预期状态。",
        ],
        ["仅取窗口内最新计划实例，不是完整执行历史或成功率；当前未计算/保存历史 P90。", _STALE],
    ),
    "mrs_job": (
        "DataArts 作业实例中的 MRS 类节点（MRSSpark / MapReduce / MRSFlinkJob / MRSHetuEngine）："
        "每个作业最近一次实例里各节点的状态与运行时长。",
        [
            "按工作空间、作业名、节点名和实例 ID 定位；fail 先看节点 logPath 指向的日志，"
            "再到 MRS Manager / YARN 查看 application。",
            "节点长时间 waiting 通常是上游节点未完成或 MRS 队列资源不足，当前未采集队列容量指标。",
        ],
        [
            "window_hours 默认 24，接口限制查询范围不超过 3 天；作业很多时用 max_details 控制每轮详情查询数。",
            "实例进入 success / fail / manual-stop 后详情不再变化，平台复用本地记录，只对未终态实例重复查询。",
        ],
        ["只覆盖 DataArts 编排的 MRS 节点；MRS 控制台直接提交的作业不可见。", _STALE],
    ),
    "cce_workload": (
        "按工作负载汇总 Pod 快照、重启/OOM、CrashLoop、驱逐和可选背压事件。",
        [
            "从集群/命名空间/工作负载定位容器事件，查看退出原因、exitCode 与上一次容器日志。",
            "OOM 检查内存限制与进程/JVM 设置；CrashLoop 检查配置、依赖和镜像；驱逐检查节点压力与 requests。",
            "背压先确认来源是 Event 关键字还是探针，再沿下游写入、瓶颈算子与消费能力定位；探针采集失败要看错误。",
        ],
        [
            "重启统计含 OOM，不能再次相加作总数；同一事件可能在不同来源出现，应对照时间线核验。",
            "探针只选业务有解释的指标，核对单位、阈值、聚合方式及持续/恢复时间；不应随意拿通用百分比代替。",
            "首次看到 Pod 只回填最近 1 小时内最后一次重启，不用累计 restartCount 回填整段历史。",
        ],
        [
            "不是完整 K8s 审计；默认排除 kube-public/kube-node-lease，不采集期望副本数。",
            "集群非 Available 时跳过；Pod/Event 或探针权限失败不应被解释为零异常。",
            _STALE,
        ],
    ),
}

_TASKS = TYPE_TASKS   # 资源类型 → 采集任务，由 catalog.TASK_SPECS 派生，不在此重复维护


def _common_facts() -> list[GuideField]:
    return [
        _field(
            "critical",
            "关键资源标记",
            None,
            "资源 critical 标记，由流水线补入；不是上游性能指标。",
            "services/pipeline.py: Resource.critical",
            "boolean",
            availability="derived",
        ),
        _field(
            "expected_running",
            "预期应运行",
            None,
            "资源 expected_state.running，未设置时默认 True；计划停机应明确维护。",
            "services/pipeline.py: Resource.expected_state",
            "boolean",
            availability="derived",
        ),
        _field(
            "status_duration_min",
            "本地状态持续时间",
            "分钟",
            "有效的显式持续分钟优先；否则按独立检查项、子对象和执行身份的本地观测计算，缺采或身份变化后重计。",
            "services/inspection_evidence.py: 独立状态证据",
            "number",
            availability="derived",
            limitations=("是平台从首次观测到状态变化的时长，不是上游精确提交/开始时间。",),
        ),
    ]


def get_resource_guide(resource_type: str) -> ResourceGuide | None:
    """返回独立对象，调用方修改列表不会污染共享元数据。"""
    meta = RESOURCE_TYPES.get(resource_type)
    if meta is None:
        return None
    summary, guidance, practices, limits = _CONTENT[resource_type]
    metrics = _METRICS.get(resource_type, [])
    facts = list(_FACTS.get(resource_type, []))
    # 只有 Observation.facts 实际含同名键才复用；展示型汇总不自动成为规则事实。
    exclude = {
        "dgc_realtime_job": {"nodes"},
        "gaussdb_instance": {"disk_used_gb", "disk_total_gb"},
        "cce_workload": {"pods", "ready_pods", "restarts_24h"},
    }.get(resource_type, set())
    exclude |= {field.key for field in facts}
    facts += [f for f in metrics if f.key not in exclude and f.availability != "not_collected"]
    if resource_type in _TASKS:
        if resource_type != "cce_cluster":
            facts += _common_facts()
    ledger_fields = []
    if meta["ledger"]:
        ledger_fields = [
            _attribute(
                "status_raw",
                "台账状态",
                "LedgerRecord.status 写入资源 status_raw；保留或按类型映射上游状态。",
                "ledger/fetchers.py + ledger/sync.py",
            ),
            _attribute(
                "spec", "规格展示", "由本类型 fetcher 拼接的规格/网段/带宽说明，不统一解析为容量。", "ledger/fetchers.py"
            ),
        ] + _CAPACITY.get(resource_type, [])
    return ResourceGuide(
        resource_type=resource_type,
        name=meta["name"],
        service=meta["service"],
        category=meta["category"],
        ledger=meta["ledger"],
        summary=summary,
        collection_tasks=_TASKS.get(resource_type, []),
        metrics=[f.model_copy(deep=True) for f in metrics],
        facts=[f.model_copy(deep=True) for f in facts],
        attributes=[f.model_copy(deep=True) for f in _ATTRS[resource_type]] + ([
            _attribute("metrics_monitoring", "本地采集时间",
                       "作业与容器运行指标的实际采集时间和有效期；未返回字段清空，缺采与过期不补零。",
                       "services/pipeline.py", data_type="object"),
            _attribute("inspection_monitoring", "巡检有效性",
                       "实际规则评估时间、有效与缺失检查数、有效期；无数据或过期不视为健康。",
                       "services/pipeline.py", data_type="object")
        ] if resource_type in _TASKS and resource_type != "cce_cluster" else []),
        ledger_fields=ledger_fields,
        guidance=list(guidance),
        best_practices=list(practices),
        limitations=[_NO_DATA, *limits],
    )


def list_resource_guides() -> list[ResourceGuide]:
    """按 catalog 顺序返回全部资源类型；不读取租户/资源数据或调用 HCS。"""
    guides = [get_resource_guide(code) for code in RESOURCE_TYPES]
    return [guide for guide in guides if guide is not None]


def rule_fact_fields(resource_type: str, check_item: str | None = None) -> list[GuideField]:
    """按实际检查项取规则事实；错误检查项返回空，避免通用事实造出不存在的 target。"""
    guide = get_resource_guide(resource_type)
    if guide is None:
        return []
    checks = {c for f in guide.facts for c in f.check_items}
    if check_item is not None and check_item not in checks:
        return []
    return [f for f in guide.facts if check_item is None or not f.check_items or check_item in f.check_items]


def render_resource_guides_markdown() -> str:
    """文档由同一元数据生成；测试逐字校验，防止代码与文档口径漂移。"""
    lines = [
        "# 资源与指标指南",
        "",
        "> 由 `app.services.resource_guides.render_resource_guides_markdown()` 生成；修改说明源后重新生成。",
        "> 说明基于当前适配器实现，不承诺所有 HCS 局点都提供同名字段；真实映射需只读联调确认。",
        "",
        "## 使用与数据边界",
        "",
        "- 页面：`/guides?type=ecs_server`；支持资源类型切换、按键/含义/来源检索。",
        "- API：`GET /api/v1/guides` 返回全部指南，`GET /api/v1/guides/{resource_type}` 返回单项；只需登录，不读取租户数据。",
        "- `metrics` 是资源监控快照；`facts` 是规则每次观测的事实，必须按 `check_items` 使用，不能任意混用。",
        "- `attributes` 是资源扩展属性；`ledger_fields` 是顶层台账容量/配置。属性和台账列不会自动成为规则变量。",
        "- 所有上游字段均可能缺失；optional=true 不表示任意回退 0。数值单位是当前代码约定，未确认的原始时间字段不推测单位。",
        "- GB/MB 沿用项目展示约定；仅 ECS flavor.ram 明确除以 1024，不能自行假设所有容量都来自字节或统一为 GiB。",
        f"- {_STALE}",
        "- 指标异常与采集异常分开排查：先检查任务运行状态/局部错误、指标源和权限，再评估业务指标。",
        "- 内置规则只是初始配置；已部署规则可能已调整，指南不提供固定的通用告警阈值。",
        "- HCS API 采集 QPS 与应用吞吐、数据库 QPS 无关，详见《采集与QPS最佳实践》。",
        "",
        "## 结构化元数据约定",
        "",
        "`GuideField` 包含 key、label、unit、meaning、source、data_type、optional、availability、check_items、limitations。",
        "`availability=not_collected` 仅用于指出旧展示列不具备采集来源；derived 表示平台计算；collected 不保证本轮有值。",
        "事实中 check_items=[] 仅表示当前巡检类型的流水线通用字段；纯台账类型 facts=[]，不支持凭空创建规则。",
        "规则可复用 `rule_fact_fields(resource_type, check_item)`；不存在的检查项返回空列表。",
        "",
    ]
    for guide in list_resource_guides():
        lines += [
            f"## {guide.name}（`{guide.resource_type}`）",
            "",
            guide.summary,
            "",
            f"- 服务：`{guide.service}`；分类：`{guide.category}`；台账：{'是' if guide.ledger else '否'}。",
            f"- 巡检任务：{'、'.join(guide.collection_tasks) or '无独立巡检任务；仅台账同步'}。",
            "",
        ]
        for attr, title, prefix in (
            ("metrics", "监控快照 metrics", "metrics."),
            ("facts", "规则事实 facts", ""),
            ("attributes", "扩展属性 attrs", "attrs."),
            ("ledger_fields", "台账字段", ""),
        ):
            fields = getattr(guide, attr)
            lines += [f"### {title}", ""]
            if not fields:
                lines += ["当前无此类输出；不能将未采集解释为 0 或健康。", ""]
                continue
            lines += [
                "| 字段 | 名称 / 类型 | 单位 | 含义 | 来源 | 检查项 / 适用限制 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for f in fields:
                context = "、".join(f.check_items) or ("流水线通用" if attr == "facts" else "本资源类型")
                notes = [f"{context}；可选；{f.availability}", *f.limitations]
                values = [
                    f"`{prefix}{f.key}`",
                    f"{f.label} / {f.data_type}",
                    f.unit or "无单位/原样文本",
                    f.meaning,
                    f.source,
                    "；".join(notes),
                ]
                lines.append("| " + " | ".join(v.replace("|", "\\|").replace("\n", " ") for v in values) + " |")
            lines.append("")
        for title, values in (
            ("排障顺序", guide.guidance),
            ("最佳实践", guide.best_practices),
            ("适用限制", guide.limitations),
        ):
            lines += [f"### {title}", "", *[f"- {v}" for v in values], ""]
    return "\n".join(lines)
