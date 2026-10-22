"""资源类型、服务、级别等字典（前后端共用的展示口径）。"""

SERVICES = {
    "ecs": "ECS", "evs": "EVS", "vpc": "VPC", "elb": "ELB", "rds": "RDS", "dms": "DMS Kafka", "cce": "CCE",
    "dws": "DWS", "css": "CSS", "dataarts": "DataArts Studio", "mrs": "MRS", "gaussdb": "GaussDB", "dcs": "DCS",
}

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
                                _c("attrs.flavor", "集群规格", 130)]},
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
    "rds_instance": {"service": "rds", "name": "云数据库 RDS", "category": "database", "tag": "RDS", "ledger": True,
                     "columns": [_c("attrs.engine", "引擎", 120), _c("attrs.ha", "部署方式", 90)]},
    "gaussdb_instance": {"service": "gaussdb", "name": "GaussDB 实例", "category": "database", "group": "gaussdb",
                         "tag": "GaussDB", "ledger": True,
                         "columns": [_c("attrs.version", "版本", 120), _c("attrs.type", "部署形态", 100)]},
    "dcs_instance": {"service": "dcs", "name": "DCS 缓存实例", "category": "database", "group": "dcs", "tag": "DCS",
                     "ledger": True, "columns": [_c("attrs.engine_version", "版本", 80),
                                                 _c("attrs.port", "端口", 70)]},
    "kafka_instance": {"service": "dms", "name": "Kafka 实例", "category": "bigdata", "tag": "Kafka", "ledger": True,
                       "columns": [_c("attrs.engine_version", "版本", 80), _c("attrs.partition_num", "分区数", 80),
                                   _c("attrs.storage_usage", "存储使用率(%)", 120)]},
    "mrs_cluster": {"service": "mrs", "name": "MRS 集群", "category": "bigdata", "group": "mrs", "tag": "MRS集群",
                    "ledger": True, "columns": [_c("attrs.version", "版本", 150), _c("attrs.nodes", "节点数", 80),
                                                _c("attrs.components", "组件", 200)]},
    "dws_cluster": {"service": "dws", "name": "DWS 数据仓库", "category": "bigdata", "tag": "DWS", "ledger": True,
                    "columns": [_c("attrs.version", "版本", 90), _c("attrs.node_count", "节点数", 80),
                                _c("attrs.node_type", "节点规格", 140)]},
    "css_cluster": {"service": "css", "name": "CSS 云搜索", "category": "bigdata", "tag": "CSS", "ledger": True,
                    "columns": [_c("attrs.version", "版本", 120), _c("attrs.node_count", "节点数", 80)]},
    # ---- 作业类：只巡检，不进台账 ----
    "dgc_realtime_job": {"service": "dataarts", "name": "实时作业", "category": "job", "group": "dgc_realtime",
                         "tag": "DataArts实时作业", "ledger": False,
                         "columns": [_c("attrs.workspace", "工作空间", 120)]},
    "dgc_batch_job": {"service": "dataarts", "name": "批作业", "category": "job", "group": "dgc_batch",
                      "tag": "DataArts批作业", "ledger": False,
                      "columns": [_c("attrs.workspace", "工作空间", 120), _c("attrs.schedule", "调度", 120)]},
    "mrs_job": {"service": "mrs", "name": "MRS 作业", "category": "job", "group": "mrs", "tag": "MRS作业",
                "ledger": False, "columns": [_c("attrs.cluster", "集群", 120), _c("attrs.job_type", "类型", 100)]},
    # ---- 容器工作负载：由 CCE 容器事件采集汇总（Deployment / StatefulSet ...），只巡检，不进台账 ----
    "cce_workload": {"service": "cce", "name": "容器工作负载", "category": "container", "group": "cce", "tag": "CCE",
                     "ledger": False,
                     "columns": [_c("attrs.cluster", "集群", 120), _c("attrs.namespace", "命名空间", 110),
                                 _c("attrs.kind", "类型", 100), _c("metrics.pods", "Pod 数", 70)]},
}
LEDGER_TYPES = [k for k, v in RESOURCE_TYPES.items() if v["ledger"]]

ENV_TYPES = {"PROD": "生产", "TEST": "测试", "DEV": "开发"}
# GaussDB 部署形态（台账同步与巡检采集共用同一口径，避免两边写入的属性互相覆盖被误判为变更）
GAUSSDB_DEPLOY = {"Ha": "主备", "Independent": "独立部署", "Distributed": "分布式"}

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


def type_name(code: str) -> str:
    return RESOURCE_TYPES.get(code, {}).get("name", code)


def category_types(category: str) -> list[str]:
    return [k for k, v in RESOURCE_TYPES.items() if v["category"] == category]


def ledger_categories() -> list[dict]:
    """资源台账页的分类（不含只巡检的作业 / 容器工作负载）。"""
    return [c for c in CATEGORIES if c["code"] not in NON_LEDGER_CATEGORIES]
