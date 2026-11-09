"""平台唯一的业务目录：云服务、资源类型、巡检任务及展示口径。

其他模块（采集开关、台账、巡检证据、资源说明、任务说明、内置任务初始化、Probe）一律从这里派生，
不再各自维护列表；一致性由 tests/test_catalog_consistency.py 守护。本模块只放纯数据，不导入业务代码。
"""
from dataclasses import dataclass

SERVICES = {
    "ecs": "ECS", "evs": "EVS", "vpc": "VPC", "elb": "ELB", "cce": "CCE",
    "dws": "DWS", "dataarts": "DataArts Studio", "mrs": "MRS", "gaussdb": "GaussDB", "dcs": "DCS",
    "platform": "巡检平台",
}
# 已下线的服务与资源类型（本局点不使用）：迁移 0018 清理存量数据，代码中不再出现采集或展示入口
RETIRED_SERVICES = ("rds", "dms", "css")
RETIRED_TYPES = ("rds_instance", "kafka_instance", "css_cluster")

# 台账分类（资源台账页左侧导航）
CATEGORIES = [
    {"code": "compute", "name": "计算"},
    {"code": "storage", "name": "存储"},
    {"code": "network", "name": "网络"},
    {"code": "database", "name": "数据库与缓存"},
    {"code": "bigdata", "name": "大数据与中间件"},
    {"code": "job", "name": "作业"},
    {"code": "container", "name": "容器工作负载"},
]
# 不进资源台账的分类（只做巡检）
NON_LEDGER_CATEGORIES = ("job", "container")


def _c(key: str, label: str, width: int = 120) -> dict:
    return {"key": key, "label": label, "width": width}


# ledger：是否纳入资源台账（作业类只做巡检，不进台账）；group：总览页健康分组（有 group 才参与巡检健康分）
# tag：邮件主题中的【服务】标签；columns：台账列表中该类型特有的列（通用列见前端 LedgerView）
RESOURCE_TYPES: dict[str, dict] = {
    "ecs_server": {"service": "ecs", "name": "云服务器 ECS", "category": "compute", "group": "ecs", "tag": "ECS",
                   "ledger": True,
                   "columns": [_c("attrs.os", "操作系统", 150), _c("attrs.flavor", "规格编码", 140),
                               _c("attrs.eip", "弹性公网 IP", 130), _c("metrics.cpu_util", "CPU(%)", 80),
                               _c("metrics.mem_util", "内存(%)", 80), _c("metrics.disk_util", "磁盘(%)", 80)]},
    "cce_cluster": {"service": "cce", "name": "CCE 集群", "category": "compute", "tag": "CCE", "ledger": True,
                    "columns": [_c("attrs.version", "版本", 90), _c("attrs.node_count", "节点数", 80),
                                _c("attrs.flavor", "集群规格", 130), _c("attrs.node_source", "节点清单", 100),
                                _c("attrs.reason", "集群原因", 140), _c("attrs.endpoints", "访问地址", 260)]},
    "evs_volume": {"service": "evs", "name": "云硬盘 EVS", "category": "storage", "tag": "EVS", "ledger": True,
                   "columns": [_c("attrs.volume_type", "磁盘类型", 100), _c("attrs.attached_to", "挂载云服务器", 150),
                               _c("attrs.bootable", "系统盘", 80)]},
    "vpc": {"service": "vpc", "name": "虚拟私有云 VPC", "category": "network", "tag": "VPC", "ledger": True,
            "columns": [_c("attrs.cidr", "网段", 140), _c("attrs.subnet_count", "子网数", 80)]},
    "vpc_subnet": {"service": "vpc", "name": "子网", "category": "network", "tag": "子网", "ledger": True,
                   "columns": [_c("attrs.cidr", "网段", 140), _c("attrs.gateway", "网关", 120),
                               _c("attrs.vpc_name", "所属 VPC", 130)]},
    "eip": {"service": "vpc", "name": "弹性公网 IP", "category": "network", "tag": "EIP", "ledger": True,
            "columns": [_c("attrs.bandwidth", "带宽(Mbit/s)", 110), _c("attrs.share_type", "带宽类型", 90),
                        _c("attrs.bound_to", "绑定对象", 150)]},
    "elb": {"service": "elb", "name": "负载均衡 ELB", "category": "network", "tag": "ELB", "ledger": True,
            "columns": [_c("attrs.eip", "公网 IP", 130), _c("attrs.listeners", "监听器", 80),
                        _c("attrs.members", "后端成员", 90)]},
    "gaussdb_instance": {"service": "gaussdb", "name": "GaussDB 实例", "category": "database", "group": "gaussdb",
                         "tag": "GaussDB", "ledger": True,
                         "columns": [_c("attrs.version", "版本", 120), _c("attrs.type", "部署形态", 100)]},
    "dcs_instance": {"service": "dcs", "name": "DCS 缓存实例", "category": "database", "group": "dcs", "tag": "DCS",
                     "ledger": True, "columns": [_c("attrs.engine_version", "版本", 80),
                                                 _c("attrs.port", "端口", 70)]},
    "mrs_cluster": {"service": "mrs", "name": "MRS 集群", "category": "bigdata", "group": "mrs", "tag": "MRS集群",
                    "ledger": True, "columns": [_c("attrs.version", "版本", 150), _c("attrs.nodes", "节点数", 80),
                                                _c("attrs.components", "组件", 200)]},
    "dws_cluster": {"service": "dws", "name": "DWS 数据仓库", "category": "bigdata", "tag": "DWS", "ledger": True,
                    "columns": [_c("attrs.version", "版本", 90), _c("attrs.node_count", "节点数", 80),
                                _c("attrs.node_type", "节点规格", 140)]},
    # ---- 作业类：只巡检，不进台账 ----
    "dgc_realtime_job": {"service": "dataarts", "name": "实时作业", "category": "job", "group": "dgc_realtime",
                         "tag": "DataArts实时作业", "ledger": False,
                         "columns": [_c("attrs.workspace", "工作空间", 120)]},
    "dgc_batch_job": {"service": "dataarts", "name": "批作业", "category": "job", "group": "dgc_batch",
                      "tag": "DataArts批作业", "ledger": False,
                      "columns": [_c("attrs.workspace", "工作空间", 120), _c("attrs.schedule", "调度", 120)]},
    "mrs_job": {"service": "mrs", "name": "MRS 原生作业执行", "category": "job", "group": "mrs", "tag": "MRS作业",
                "ledger": False, "columns": [_c("attrs.cluster", "集群", 140), _c("attrs.job_id", "执行 ID", 160),
                                             _c("attrs.job_type", "作业类型", 110)]},
    "dgc_mrs_node": {"service": "dataarts", "name": "DataArts MRS 节点", "category": "job", "group": "dgc_batch",
                     "tag": "DataArtsMRS节点", "ledger": False,
                     "columns": [_c("attrs.workspace", "工作空间", 120), _c("attrs.job_name", "所属作业", 140),
                                 _c("attrs.node_type", "节点类型", 110)]},
    # ---- 容器工作负载：由 CCE 容器事件采集汇总（Deployment / StatefulSet ...），只巡检，不进台账 ----
    "cce_workload": {"service": "cce", "name": "容器工作负载", "category": "container", "group": "cce", "tag": "CCE",
                     "ledger": False,
                     "columns": [_c("attrs.cluster", "集群", 120), _c("attrs.namespace", "命名空间", 110),
                                 _c("attrs.kind", "类型", 100), _c("metrics.pods", "Pod 数", 70)]},
}
LEDGER_TYPES = [k for k, v in RESOURCE_TYPES.items() if v["ledger"]]
INSPECT_ONLY_TYPES = [k for k, v in RESOURCE_TYPES.items() if not v["ledger"]]

# ---- 采集范围：采集开关之下再细分，只巡检（或屏蔽）一部分对象 ----
# 每类资源可用于“包含 / 排除”规则的匹配维度。匹配引擎见 app.collectors.scope，存取与同步见 app.services.collect_scope。
# - id / name：资源 ID / 名称；cluster 同时匹配集群名称与集群 ID
# - env / business / owner / critical 来自台账管理字段（页面维护），只有台账类资源提供；
#   采集器在调用云接口前拿不到这些字段，由巡检流水线按台账记录做最终判定
SCOPE_DIM_LABELS: dict[str, str] = {
    "name": "名称", "id": "资源 ID", "cluster": "集群", "namespace": "命名空间", "workload": "工作负载",
    "workspace": "工作空间", "job": "作业", "node": "节点",
    "env": "环境", "business": "业务系统", "owner": "负责人", "critical": "核心资源",
}
LEDGER_SCOPE_DIMS = ("env", "business", "owner", "critical")
_SCOPE_SPECIFIC: dict[str, tuple[str, ...]] = {
    "dgc_realtime_job": ("workspace", "job"),
    "dgc_batch_job": ("workspace", "job"),
    "mrs_job": ("cluster", "job", "id"),
    "dgc_mrs_node": ("workspace", "job", "node"),
    "cce_workload": ("cluster", "namespace", "workload"),
    "cce_cluster": ("cluster",),
}
SCOPE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    code: _SCOPE_SPECIFIC.get(code, ("name", "id")) + (LEDGER_SCOPE_DIMS if meta["ledger"] else ())
    for code, meta in RESOURCE_TYPES.items()}


@dataclass(frozen=True)
class TaskSpec:
    """内置任务定义。collector=True 的任务必须在 collectors/registry.TASKS 中有同名采集器。

    types：任务采集 / 写入的资源类型（采集开关按类型启停任务）；checks：任务负责的规则检查项（缺采中断证据时用于归属）；
    default_enabled=False 的任务在新库中默认关闭，需要在页面显式开启。
    """
    code: str
    name: str
    service: str
    interval_sec: int
    types: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    note: str | None = None
    cron: str | None = None
    collector: bool = True
    default_enabled: bool = True


LEDGER_TASK_CODE = "ledger_sync"
CLEANUP_TASK_CODE = "data_cleanup"
TASK_SPECS: tuple[TaskSpec, ...] = (
    TaskSpec("dgc_realtime_status", "DataArts 实时作业状态", "dataarts", 60, ("dgc_realtime_job",),
             ("job_status", "node_status")),
    TaskSpec("dgc_batch_instance", "DataArts 批作业实例", "dataarts", 300, ("dgc_batch_job",), ("instance_status",)),
    TaskSpec("mrs_cluster_status", "MRS 集群状态", "mrs", 120, ("mrs_cluster",), ("cluster_status",),
             default_enabled=False),
    TaskSpec("mrs_job_status", "MRS 原生作业执行", "mrs", 300, ("mrs_job",), ("job_status",),
             note="只读 MRS cluster_infos + V2 job-executions；局点已有调用证据，上传的 8.5.1 PDF 未列出 V2 作业；"
                  "状态与结果分离，不猜 elapsed_time 单位，不按 Flink 类型推断流批", default_enabled=False),
    TaskSpec("dgc_mrs_node_status", "DataArts MRS 节点状态", "dataarts", 300, ("dgc_mrs_node",), ("job_status",),
             note="独立读取 DataArts 实例详情的 MRSSpark/MapReduce/MRSFlinkJob/MRSHetuEngine/HiveSQL/SparkSQL 节点；"
                  "仅依赖 DataArts 工作空间，不代表 MRS 原生执行全集", default_enabled=False),
    # GaussDB 没有实时 CPU / 内存接口（原生接口只有磁盘用量与巡检记录），所以不负责 cpu / memory 检查项：
    # 否则这两类规则每轮都“缺采”，实例永远是“部分覆盖”，无法判为健康
    TaskSpec("gaussdb_status", "GaussDB 实例状态", "gaussdb", 120, ("gaussdb_instance",), ("instance_status",),
             note="只查询实例清单和状态，不等待磁盘用量或组件接口；容量与组件分别调度"),
    TaskSpec("gaussdb_storage", "GaussDB 磁盘容量", "gaussdb", 300, ("gaussdb_instance",), ("disk",),
             note="在线实例通过 GaussDB volume-usage 接口采集已用量、总量与使用率；明细有界并发，失败记缺采",
             default_enabled=False),
    TaskSpec("gaussdb_components", "GaussDB 组件健康", "gaussdb", 300, ("gaussdb_instance",), ("components",),
             note="单独检查 CN / DN / CMS / GTM / ETCD 组件状态；慢 SQL、会话、巡检报告仍在 GaussDB 页面按需查询",
             default_enabled=False),
    TaskSpec("dcs_status", "DCS 实例状态与内存", "dcs", 60, ("dcs_instance",), ("instance_status", "memory", "cpu", "nodes"),
             note="每轮批量查询一次逻辑节点（分片、主从、Proxy 及节点状态）；CPU 由两次 instances/statistic 的累计 CPU 秒数折算"
                  "（Δ秒 ÷ Δ时间 ÷ 主节点数），首次采样与实例重启后不输出"),
    TaskSpec("ecs_status", "ECS 状态与告警巡检", "ecs", 120, ("ecs_server",),
             ("instance_status", "cpu", "memory", "disk", "reboot"),
             note="ECS 状态与告警判定；使用独立指标采样点，不重复查询指标源。各租户的频率、范围与指标在本任务的“租户策略”设置"),
    TaskSpec("ecs_metrics", "ECS 使用指标采样", "ecs", 300, ("ecs_server",),
             note="按租户策略采样 CPU、内存等指标；只查询纳入范围的本地实例，缺失不补零"),
    TaskSpec("ecs_quota", "ECS 项目配额采集", "ecs", 3600, ("ecs_server",),
             note="查询各项目实际配额；独立启停与周期，不调用云端配额修改接口"),
    TaskSpec("cce_pod_events", "CCE 容器事件采集", "cce", 60, ("cce_workload",),
             ("pod_restart", "pod_oom", "pod_crashloop", "pod_backpressure", "pod_evicted"),
             note="采集 CCE 集群 Kubernetes Events，汇总容器重启 / OOM / 背压等事件"),
    TaskSpec("cce_cluster_health", "CCE 节点健康与原生指标", "cce", 120, ("cce_cluster",),
             note="只读采集 CCE 集群节点、节点池健康与显式启用的 Kubernetes 原生指标", default_enabled=False),
    TaskSpec(LEDGER_TASK_CODE, "资源台账同步", "ledger", 3600, collector=False,
             note=f"同步 {len(LEDGER_TYPES)} 类资源的台账（新增 / 变更 / 释放），关闭采集的资源类型会跳过"),
    TaskSpec(CLEANUP_TASK_CODE, "数据清理（按保留期删除过期数据）", "platform", 86400, cron="30 3 * * *",
             collector=False, note="按“系统设置 → 数据保留”删除过期的运行记录、通知、已恢复告警、审计、变更记录等"),
)
TASK_BY_CODE: dict[str, TaskSpec] = {t.code: t for t in TASK_SPECS}
COLLECTOR_TASK_CODES = tuple(t.code for t in TASK_SPECS if t.collector)
# 任务 → 资源类型 / 资源类型 → 任务（采集开关、资源说明共用）
TASK_TYPES: dict[str, list[str]] = {t.code: list(t.types) for t in TASK_SPECS if t.types}
TYPE_TASKS: dict[str, list[str]] = {}
for _task in TASK_SPECS:
    for _t in _task.types:
        TYPE_TASKS.setdefault(_t, []).append(_task.code)
TASK_CHECKS: dict[str, set[str]] = {t.code: set(t.checks) for t in TASK_SPECS if t.collector}
TASK_NOTES: dict[str, str] = {t.code: t.note for t in TASK_SPECS if t.note}
TASK_STEPS: dict[str, tuple[str, ...]] = {
    "dgc_realtime_status": ("读取已配置工作空间作业", "并发查询作业与节点状态", "评估状态规则"),
    "dgc_batch_instance": ("查询批作业及最近实例", "计算有效执行耗时和 P90 基线", "检查失败与运行超时"),
    "mrs_cluster_status": ("查询 MRS 集群", "检查集群状态"),
    "mrs_job_status": ("读取 MRS 集群清单", "分页读取每个集群的原生作业执行", "分别检查状态与结果"),
    "dgc_mrs_node_status": ("读取 DataArts 作业实例", "检查实例详情中的 MRS 类节点"),
    "gaussdb_status": ("查询 GaussDB 实例清单", "检查实例状态"),
    "gaussdb_storage": ("查询实例清单并筛选在线实例", "并发查询磁盘用量", "保存容量历史并检查磁盘规则"),
    "gaussdb_components": ("查询实例清单并筛选在线实例", "并发查询组件清单", "检查宕机与未识别状态"),
    "dcs_status": ("查询 DCS 实例与统计", "批量读取逻辑节点", "检查状态、内存、CPU 与节点"),
    "ecs_status": ("查询 ECS 状态", "读取已有有效指标采样", "评估状态与指标告警"),
    "ecs_metrics": ("筛选已纳入采集的本地实例", "按租户配置查询指标源", "保存实际采样点"),
    "ecs_quota": ("查询项目配额", "保存当前配额快照"),
    "cce_pod_events": ("读取已配置集群 Events", "汇总容器事件", "执行已启用探针"),
    "cce_cluster_health": ("筛选已启用集群", "读取节点与节点池", "查询显式启用的原生指标"),
    LEDGER_TASK_CODE: ("按租户和类型读取资源清单", "对比新增与变更", "完整采集后确认释放"),
    CLEANUP_TASK_CODE: ("读取数据保留设置", "分批清理过期记录", "记录清理数量"),
}

# 平台自身告警的对象类型（service = platform，见 app.services.platform_watch）：不进台账、不参与健康分、没有采集开关
PLATFORM_TYPES: dict[str, dict] = {
    "platform_task": {"service": "platform", "name": "巡检任务", "tag": "平台", "ledger": False},
    "platform_tenant": {"service": "platform", "name": "租户接入", "tag": "平台", "ledger": False},
    "platform_notify": {"service": "platform", "name": "通知投递", "tag": "平台", "ledger": False},
    "platform_worker": {"service": "platform", "name": "后台进程", "tag": "平台", "ledger": False},
}

ENV_TYPES = {"PROD": "生产", "TEST": "测试", "DEV": "开发"}
# GaussDB 部署形态（台账同步与巡检采集共用同一口径，避免两边写入的属性互相覆盖被误判为变更）
GAUSSDB_DEPLOY = {"Ha": "集中式", "centralization_standard": "集中式", "Combined": "分布式（混合部署）",
                 "Independent": "独立部署", "Distributed": "分布式"}

# ECS 实例状态（HCS 8.5.1 ECS 接口 status 字段）
ECS_STATUS = {"ACTIVE": "运行中", "SHUTOFF": "关机", "BUILD": "创建中", "REBOOT": "重启中", "HARD_REBOOT": "强制重启中",
              "RESIZE": "变更规格中", "VERIFY_RESIZE": "规格变更待确认", "REVERT_RESIZE": "规格回退中",
              "MIGRATING": "迁移中", "REBUILD": "重装中", "ERROR": "故障", "DELETED": "已删除", "SHELVED": "已搁置",
              "SHELVED_OFFLOADED": "已搁置", "UNKNOWN": "未知"}

# 总览页服务健康卡片
HEALTH_GROUPS = [
    {"code": "ecs", "name": "ECS 云服务器", "service": "ecs"},
    {"code": "dgc_realtime", "name": "DataArts 实时作业", "service": "dataarts"},
    {"code": "dgc_batch", "name": "DataArts 批作业", "service": "dataarts"},
    {"code": "mrs", "name": "MRS 集群与作业", "service": "mrs"},
    {"code": "gaussdb", "name": "GaussDB 数据库", "service": "gaussdb"},
    {"code": "dcs", "name": "DCS 缓存", "service": "dcs"},
    {"code": "cce", "name": "CCE 容器", "service": "cce"},
]

SEVERITY_LABEL = {"P1": "紧急", "P2": "重要", "P3": "次要", "P4": "提示"}
SEVERITY_COLOR = {"P1": "#D93026", "P2": "#F57C00", "P3": "#F9A825", "P4": "#1976D2", "RESOLVED": "#2E7D32"}
STATUS_LABEL = {"FIRING": "告警中", "ACKED": "已认领", "SUPPRESSED": "已抑制", "RESOLVED": "已恢复", "CLOSED": "已关闭"}
HEALTH_BY_SEVERITY = {"P1": "CRITICAL", "P2": "MAJOR", "P3": "WARNING", "P4": "INFO"}
RESOURCE_SCORE = {None: 100, "P4": 95, "P3": 80, "P2": 60, "P1": 0}   # 资源分：按最高活动告警级别
CRITICAL_WEIGHT = 3


def type_meta(code: str) -> dict:
    """资源类型字典（含平台自身告警的对象类型）。"""
    return RESOURCE_TYPES.get(code) or PLATFORM_TYPES.get(code) or {}


def type_name(code: str) -> str:
    return type_meta(code).get("name", code)


def category_types(category: str) -> list[str]:
    return [k for k, v in RESOURCE_TYPES.items() if v["category"] == category]


def ledger_categories() -> list[dict]:
    """资源台账页的分类（不含只巡检的作业 / 容器工作负载）。"""
    return [c for c in CATEGORIES if c["code"] not in NON_LEDGER_CATEGORIES]
