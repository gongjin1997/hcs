"""租户A（核心生产）的演示资源清单：Mock HCS 按 OpenAPI 字段格式返回这些数据（转换见 openapi.py / k8s.py），
演示数据（seed）也通过台账同步、巡检从这里拉取，保证台账、巡检、告警、容器事件里看到的是同一批资源。
租户B 的清单见 inventory_b.py（结构相同）。

时间：老资源用固定日期；“最近 30 天新建”的资源用相对时间（NOW - n 天），这样任何时候启动演示环境都有近期变更。
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

NOW = datetime.now(UTC).replace(microsecond=0)
NS = uuid.UUID("6f1d2c1e-7a0b-4c55-9a35-3d1f0c2b9e10")
AZ1, AZ2 = "az1.dc1", "az2.dc1"


def rid(kind: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}:{name}"))


def ago(days: float, hour: int = 10) -> datetime:
    return (NOW - timedelta(days=days)).replace(hour=hour, minute=17, second=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fixed(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# ---------------- 网络 ----------------
VPCS = [("vpc-prod", "10.10.0.0/16", "生产业务 VPC"), ("vpc-test", "10.30.0.0/16", "测试 / 开发 VPC"),
        ("vpc-mgmt", "10.99.0.0/16", "运维管理 VPC")]
SUBNETS = [  # 名称, VPC, 网段, AZ
    ("subnet-prod-web", "vpc-prod", "10.10.1.0/24", AZ1), ("subnet-prod-app", "vpc-prod", "10.10.2.0/24", AZ1),
    ("subnet-prod-db", "vpc-prod", "10.10.3.0/24", AZ2), ("subnet-prod-bigdata", "vpc-prod", "10.10.4.0/24", AZ1),
    ("subnet-prod-cce", "vpc-prod", "10.10.8.0/22", AZ1), ("subnet-test-app", "vpc-test", "10.30.1.0/24", AZ1),
    ("subnet-test-cce", "vpc-test", "10.30.8.0/24", AZ1), ("subnet-mgmt", "vpc-mgmt", "10.99.1.0/24", AZ2),
]
SUBNET_VPC = {s[0]: s[1] for s in SUBNETS}

# ---------------- 计算 ----------------
FLAVORS = {"s6.large.2": (2, 4), "s6.xlarge.2": (4, 8), "s6.2xlarge.2": (8, 16), "s6.xlarge.4": (4, 16),
           "c6.xlarge.2": (4, 8), "c6.2xlarge.2": (8, 16), "c6.2xlarge.4": (8, 32), "c6.4xlarge.2": (16, 32),
           "m6.4xlarge.8": (16, 128)}
EULER, KYLIN, CENTOS = "EulerOS 2.9 64bit", "Kylin V10 SP2 64bit", "CentOS 7.9 64bit"
# 名称, 规格, 镜像, 子网, IP, AZ, 状态, 创建时间, 系统盘 GB, 数据盘 [(GB, 类型)]
EcsRow = tuple[str, str, str, str, str, str, str, datetime, int, list[tuple[int, str]]]
ECS: list[EcsRow] = [
    ("ecs-nginx-01", "s6.xlarge.2", EULER, "subnet-prod-web", "10.10.1.11", AZ1, "ACTIVE", fixed("2025-03-12 08:21"), 40, []),
    ("ecs-nginx-02", "s6.xlarge.2", EULER, "subnet-prod-web", "10.10.1.12", AZ2, "ACTIVE", fixed("2025-03-12 08:25"), 40, []),
    ("ecs-web-01", "c6.2xlarge.2", EULER, "subnet-prod-web", "10.10.1.21", AZ1, "ACTIVE", fixed("2025-03-12 09:02"), 40, []),
    ("ecs-web-02", "c6.2xlarge.2", EULER, "subnet-prod-web", "10.10.1.22", AZ2, "ACTIVE", fixed("2025-03-12 09:04"), 40, []),
    ("ecs-web-03", "c6.2xlarge.2", EULER, "subnet-prod-web", "10.10.1.23", AZ1, "ACTIVE", fixed("2025-06-03 14:40"), 40, []),
    ("ecs-web-04", "c6.2xlarge.2", EULER, "subnet-prod-web", "10.10.1.24", AZ2, "ACTIVE", ago(6), 40, []),
    ("ecs-app-01", "c6.4xlarge.2", KYLIN, "subnet-prod-app", "10.10.2.11", AZ1, "ACTIVE", fixed("2025-03-13 10:11"), 50, [(200, "SSD")]),
    ("ecs-app-02", "c6.4xlarge.2", KYLIN, "subnet-prod-app", "10.10.2.12", AZ2, "ACTIVE", fixed("2025-03-13 10:13"), 50, [(200, "SSD")]),
    ("ecs-app-03", "c6.2xlarge.2", KYLIN, "subnet-prod-app", "10.10.2.13", AZ1, "ACTIVE", fixed("2025-05-20 16:30"), 50, [(200, "SSD")]),
    ("ecs-app-04", "c6.2xlarge.2", KYLIN, "subnet-prod-app", "10.10.2.14", AZ2, "SHUTOFF", fixed("2025-05-20 16:33"), 50, [(200, "SSD")]),
    ("ecs-api-gw-01", "s6.2xlarge.2", EULER, "subnet-prod-app", "10.10.2.21", AZ1, "ACTIVE", fixed("2025-04-02 11:20"), 40, []),
    ("ecs-api-gw-02", "s6.2xlarge.2", EULER, "subnet-prod-app", "10.10.2.22", AZ2, "ACTIVE", fixed("2025-04-02 11:22"), 40, []),
    ("ecs-job-01", "c6.xlarge.2", KYLIN, "subnet-prod-app", "10.10.2.31", AZ1, "ACTIVE", fixed("2025-07-15 09:45"), 40, [(100, "SSD")]),
    ("ecs-etl-01", "m6.4xlarge.8", EULER, "subnet-prod-bigdata", "10.10.4.51", AZ1, "ACTIVE", fixed("2025-02-18 15:00"), 50, [(1000, "SSD")]),
    ("ecs-etl-02", "m6.4xlarge.8", EULER, "subnet-prod-bigdata", "10.10.4.52", AZ1, "ACTIVE", fixed("2025-02-18 15:04"), 50, [(1000, "SSD")]),
    ("ecs-etl-03", "m6.4xlarge.8", EULER, "subnet-prod-bigdata", "10.10.4.53", AZ1, "ACTIVE", ago(4), 50, [(1000, "SSD")]),
    ("ecs-report-01", "c6.2xlarge.4", EULER, "subnet-prod-bigdata", "10.10.4.61", AZ1, "ACTIVE", fixed("2025-08-06 10:30"), 50, [(500, "SAS")]),
    ("ecs-bastion-01", "s6.large.2", CENTOS, "subnet-mgmt", "10.99.1.11", AZ2, "ACTIVE", fixed("2025-01-20 09:00"), 40, []),
    ("ecs-jenkins-01", "c6.2xlarge.2", CENTOS, "subnet-mgmt", "10.99.1.21", AZ2, "ACTIVE", fixed("2025-01-22 14:10"), 50, [(500, "SAS")]),
    ("ecs-gitlab-01", "c6.2xlarge.4", CENTOS, "subnet-mgmt", "10.99.1.22", AZ2, "ACTIVE", fixed("2025-01-22 14:30"), 50, [(1000, "SAS")]),
    ("ecs-ntp-dns-01", "s6.large.2", EULER, "subnet-mgmt", "10.99.1.31", AZ2, "ACTIVE", fixed("2025-01-20 09:30"), 40, []),
    ("ecs-inspector-01", "s6.xlarge.2", EULER, "subnet-mgmt", "10.99.1.41", AZ2, "ACTIVE", ago(20), 40, [(100, "SSD")]),
    ("ecs-test-01", "s6.xlarge.2", EULER, "subnet-test-app", "10.30.1.11", AZ1, "ACTIVE", fixed("2025-04-10 10:00"), 40, []),
    ("ecs-test-02", "s6.xlarge.2", EULER, "subnet-test-app", "10.30.1.12", AZ1, "SHUTOFF", fixed("2025-04-10 10:05"), 40, []),
    ("ecs-dev-01", "s6.large.2", CENTOS, "subnet-test-app", "10.30.1.21", AZ1, "SHUTOFF", fixed("2025-09-01 15:20"), 40, []),
    *[(f"cce-prod-node-0{i}", "c6.2xlarge.4", EULER, "subnet-prod-cce", f"10.10.8.{10 + i}", AZ1 if i % 2 else AZ2,
       "ACTIVE", ago(11) if i == 6 else fixed(f"2025-03-2{i} 10:00"), 50, [(100, "SSD")]) for i in range(1, 7)],
    *[(f"cce-test-node-0{i}", "s6.xlarge.4", EULER, "subnet-test-cce", f"10.30.8.{10 + i}", AZ1, "ACTIVE",
       fixed("2025-04-12 10:00"), 50, [(100, "SSD")]) for i in range(1, 4)],
]
ECS_IP = {e[0]: e[4] for e in ECS}
# 未挂载的云硬盘（台账里的“疑似闲置”）：名称, GB, 类型, AZ, 创建时间
SPARE_VOLUMES = [("vol-backup-old-01", 500, "SAS", AZ1, fixed("2025-08-19 20:00")),
                 ("vol-migrate-tmp", 200, "SSD", AZ2, fixed("2025-10-30 13:00")),
                 ("vol-etl-scratch", 1000, "SAS", AZ1, ago(18))]

# ---------------- 负载均衡 / 公网 IP ----------------
# 名称, VIP, 子网, 独享型, 监听器数, 后端成员数, 创建时间
ELBS = [("elb-prod-web", "10.10.1.100", "subnet-prod-web", True, 2, 4, fixed("2025-03-12 10:00")),
        ("elb-prod-api", "10.10.2.100", "subnet-prod-app", True, 1, 2, fixed("2025-04-02 12:00")),
        ("elb-test-app", "10.30.1.100", "subnet-test-app", False, 1, 1, fixed("2025-04-10 11:00"))]
# 名称, 公网 IP, 状态, 绑定的私网 IP, 带宽, 共享带宽名, 创建时间
EIPS = [("eip-nginx-01", "203.0.113.11", "ACTIVE", "10.10.1.11", 100, None, fixed("2025-03-12 08:30")),
        ("eip-nginx-02", "203.0.113.12", "ACTIVE", "10.10.1.12", 100, None, fixed("2025-03-12 08:31")),
        ("eip-elb-web", "203.0.113.21", "ACTIVE", "10.10.1.100", 200, "bw-prod-shared", fixed("2025-03-12 10:05")),
        ("eip-elb-api", "203.0.113.22", "ACTIVE", "10.10.2.100", 200, "bw-prod-shared", fixed("2025-04-02 12:05")),
        ("eip-bastion", "203.0.113.31", "ACTIVE", "10.99.1.11", 10, None, fixed("2025-01-20 09:10")),
        ("eip-idle-01", "203.0.113.41", "DOWN", None, 20, None, fixed("2025-05-06 17:00"))]
EIP_BY_IP = {e[3]: e for e in EIPS if e[3]}

# ---------------- 数据库与缓存 ----------------
# 名称, 状态, 部署形态, 磁盘 GB, IP, CES 指标 (cpu, mem, disk), 创建时间
GAUSSDB = [("gs-core-01", "ACTIVE", "Ha", 2000, "10.10.3.11", (62.1, 71.3, 87.4), fixed("2025-02-10 08:00")),
           ("gs-report-01", "ACTIVE", "Independent", 1000, "10.10.3.12", (83.5, 64.0, 66.2), fixed("2025-03-01 08:00")),
           ("gs-crm-01", "STORAGE FULL", "Ha", 500, "10.10.3.13", (41.0, 58.2, 99.1), fixed("2025-02-10 08:30")),
           ("gs-bill-01", "ACTIVE", "Ha", 1000, "10.10.3.14", (31.0, 47.5, 54.3), fixed("2025-05-12 08:00")),
           ("gs-audit-01", "ACTIVE", "Independent", 500, "10.10.3.15", (18.2, 39.9, 41.7), fixed("2025-07-01 08:00"))]
# DCS：名称, 状态, 版本, 容量 GB, used_memory MB, IP, CES 指标 (cpu, connected_clients), 创建时间
DCS: list[dict[str, Any]] = [
    {"name": "redis-prod-01", "status": "RUNNING", "ver": "5.0", "cap": 16, "used": 15123, "ip": "10.10.3.41",
     "ces": (46.2, 1840), "created": fixed("2025-01-08 06:12")},
    {"name": "redis-session-01", "status": "RUNNING", "ver": "5.0", "cap": 8, "used": 5030, "ip": "10.10.3.42",
     "ces": (35.0, 920), "created": fixed("2025-01-08 06:20")},
    {"name": "redis-cache-02", "status": "RUNNING", "ver": "5.0", "cap": 32, "used": 27230, "ip": "10.10.3.43",
     "ces": (38.5, 2310), "created": fixed("2025-03-15 11:00")},
    {"name": "redis-rank-01", "status": "RUNNING", "ver": "6.0", "cap": 4, "used": 1851, "ip": "10.10.3.44",
     "ces": (12.4, 160), "created": fixed("2025-06-21 15:30")},
    {"name": "redis-lock-01", "status": "RUNNING", "ver": "6.0", "cap": 2, "used": 467, "ip": "10.10.3.45",
     "ces": (8.9, 420), "created": fixed("2025-06-21 15:40")},
    {"name": "redis-mq-01", "status": "RUNNING", "ver": "5.0", "cap": 8, "used": 4645, "ip": "10.10.3.46",
     "ces": (21.3, 610), "created": fixed("2025-04-08 10:00")},
]

# ---------------- 大数据 ----------------
MRS = [("mrs-prod-01", "MRS 3.2.0-LTS.1", 23, "10.10.4.11", fixed("2025-02-15 09:00")),
       ("mrs-prod-02", "MRS 3.2.0-LTS.1", 15, "10.10.4.12", fixed("2025-02-16 09:00")),
       ("mrs-rt-01", "MRS 3.1.5-LTS", 9, "10.10.4.13", fixed("2025-03-05 09:00"))]
MRS_COMPONENTS = {"mrs-rt-01": ["Zookeeper", "HDFS", "Yarn", "Flink", "Kafka"],
                  "default": ["Zookeeper", "HDFS", "Yarn", "Hive", "Spark2x", "Flink"]}
CCE = [("cce-prod-01", "v1.25", "cce.s2.medium", "10.10.8.10", "cce-prod-node-", fixed("2025-03-20 09:00")),
       ("cce-test-01", "v1.23", "cce.s1.small", "10.30.8.10", "cce-test-node-", fixed("2025-04-12 09:00"))]
DWS = [("dws-prod-01", "8.1.3", "dws.m3.xlarge", 3, "10.10.4.31", fixed("2025-02-20 08:00"))]



def find(kind: str, name: str) -> dict:
    """测试用：按名称取 mock 数据并就地修改（例如把 redis-mq-01 改为 ERROR）。仅支持 DCS。"""
    assert kind == "dcs"
    return next(d for d in DCS if d["name"] == name)


# ---------------- 运行态：ECS 使用率（CES）、CCE 工作负载与事件（容器事件采集） ----------------
# 装了 CES Agent 的实例才有内存 / 磁盘使用率（AGT.ECS）；CPU 使用率（SYS.ECS）所有运行中的实例都有
AGENT_PREFIXES = ("ecs-nginx", "ecs-web", "ecs-app", "ecs-api-gw", "ecs-job", "ecs-etl", "ecs-report", "ecs-bastion",
                  "ecs-jenkins", "ecs-gitlab", "ecs-inspector")
ECS_USAGE: dict[str, dict[str, Any]] = {   # 未列出的实例按名称生成稳定的常规值
    "ecs-etl-02": {"cpu": 93.4, "mem": 78.2},                  # CPU 持续 ≥ 85% → P3
    "ecs-etl-01": {"cpu": 71.5, "mem": 91.7},                  # 内存 ≥ 90% → P3
    "ecs-report-01": {"disks": {"/data": 91.3}},               # 数据盘 ≥ 85% → P3
    "ecs-jenkins-01": {"disks": {"/data": 96.2}},              # 数据盘 ≥ 95% → P2（构建产物堆积）
    "ecs-gitlab-01": {"disks": {"/data": 81.6}},
    "ecs-web-03": {"cpu": 62.0},
}
DEFAULT_UPTIME = timedelta(days=37, hours=5)
BOOT_AGO = {"ecs-api-gw-01": timedelta(minutes=95), "ecs-web-04": timedelta(days=6)}

IMG = "swr.region-01.hcs.example.com"
FLINK_BP = "flink_taskmanager_job_task_backPressuredTimeMsPerSecond"
# CCE 工作负载：Pod 的 restarts / last(上次退出原因, exitCode, 距今分钟) / waiting / pending / evicted 决定容器事件
WORKLOADS: dict[str, list[dict]] = {
    "cce-prod-01": [
        {"ns": "trade", "kind": "Deployment", "workload": "order-service", "hash": "7d9f8c6b5", "container": "order",
         "image": f"{IMG}/trade/order-service:2.14.3", "limit": "2Gi", "labels": {"app": "order-service"},
         "pods": [{"suffix": "x7k2p", "node": "cce-prod-node-01", "ip": "172.16.1.21", "restarts": 4,
                   "last": ("OOMKilled", 137, 6)},
                  {"suffix": "m4q9z", "node": "cce-prod-node-02", "ip": "172.16.2.17"},
                  {"suffix": "c2n8w", "node": "cce-prod-node-04", "ip": "172.16.4.33", "restarts": 1,
                   "last": ("Error", 143, 3 * 1440)},
                  {"suffix": "p5r7t", "pending": True, "age_min": 9}]},
        {"ns": "trade", "kind": "Deployment", "workload": "payment-gateway", "hash": "5c8b7f4d9", "container": "gateway",
         "image": f"{IMG}/trade/payment-gateway:3.2.0", "limit": "1Gi", "labels": {"app": "payment-gateway"},
         "age_min": 2 * 1440,
         "pods": [{"suffix": "h8j3k", "node": "cce-prod-node-03", "ip": "172.16.3.12"},
                  {"suffix": "t6w2q", "node": "cce-prod-node-05", "ip": "172.16.5.40", "restarts": 12,
                   "last": ("Error", 1, 2), "waiting": "CrashLoopBackOff",
                   "waiting_msg": "back-off 5m0s restarting failed container=gateway"}]},
        {"ns": "trade", "kind": "Deployment", "workload": "cart-service", "hash": "6f4d9b8c7", "container": "cart",
         "image": f"{IMG}/trade/cart-service:1.8.5", "limit": "1Gi", "labels": {"app": "cart-service"},
         "pods": [{"suffix": "b3v8n", "node": "cce-prod-node-02", "ip": "172.16.2.31"},
                  {"suffix": "r9k4m", "node": "cce-prod-node-06", "ip": "172.16.6.14"}]},
        {"ns": "trade", "kind": "CronJob", "workload": "report-export", "job": "29328840", "container": "export",
         "image": f"{IMG}/trade/report-export:1.0.7", "limit": "512Mi", "labels": {"app": "report-export"},
         "pods": [{"suffix": "q2w7e", "node": "cce-prod-node-03", "age_min": 35,
                   "evicted": "The node was low on resource: ephemeral-storage. Container export was using 10Gi, "
                              "which exceeds its request of 0."}]},
        {"ns": "member", "kind": "Deployment", "workload": "member-api", "hash": "84c6d9f7b", "container": "api",
         "image": f"{IMG}/member/member-api:5.1.2", "limit": "2Gi", "labels": {"app": "member-api"},
         "pods": [{"suffix": "k7p2s", "node": "cce-prod-node-01", "ip": "172.16.1.45"},
                  {"suffix": "z5x8c", "node": "cce-prod-node-04", "ip": "172.16.4.12", "unready": True}]},
        {"ns": "member", "kind": "StatefulSet", "workload": "member-points", "container": "points",
         "image": f"{IMG}/member/member-points:2.0.4", "limit": "4Gi", "labels": {"app": "member-points"},
         "pods": [{"node": "cce-prod-node-05", "ip": "172.16.5.21"}, {"node": "cce-prod-node-06", "ip": "172.16.6.22"}]},
        {"ns": "flink", "kind": "Deployment", "workload": "flink-jobmanager", "hash": "7b5c9d8f6", "container": "jobmanager",
         "image": f"{IMG}/bigdata/flink:1.15.4", "limit": "4Gi",
         "labels": {"app": "flink-rt-order", "component": "jobmanager"},
         "pods": [{"suffix": "j4m8n", "node": "cce-prod-node-02", "ip": "172.16.2.50"}]},
        {"ns": "flink", "kind": "Deployment", "workload": "flink-taskmanager", "hash": "9d7f6c5b8", "container": "taskmanager",
         "image": f"{IMG}/bigdata/flink:1.15.4", "limit": "8Gi",
         "labels": {"app": "flink-rt-order", "component": "taskmanager"},
         "metrics": {"name": FLINK_BP, "job": "rt_order_sync", "help": "Time (ms/s) this task is back pressured",
                     "hot_task": "Sink: GaussDB gs-core-01",
                     "series": [("Source: Kafka kafka-prod-01", 8), ("Sink: GaussDB gs-core-01", 35)]},
         "pods": [{"suffix": "a1s2d", "node": "cce-prod-node-03", "ip": "172.16.3.61", "bp": 862},
                  {"suffix": "f3g4h", "node": "cce-prod-node-04", "ip": "172.16.4.62", "bp": 118},
                  {"suffix": "j5k6l", "node": "cce-prod-node-06", "ip": "172.16.6.63", "bp": 96}]},
        {"ns": "kube-system", "kind": "Deployment", "workload": "coredns", "hash": "6d4b75cb6", "container": "coredns",
         "image": f"{IMG}/hwofficial/coredns:1.25.1", "limit": "512Mi", "labels": {"k8s-app": "coredns"},
         "age_min": 210 * 1440,
         "pods": [{"suffix": "8xk2v", "node": "cce-prod-node-01", "ip": "172.16.1.2"},
                  {"suffix": "w9p4r", "node": "cce-prod-node-02", "ip": "172.16.2.2"}]},
    ],
    "cce-test-01": [
        {"ns": "test", "kind": "Deployment", "workload": "demo-app", "hash": "58f9c7d6b", "container": "app",
         "image": f"{IMG}/test/demo-app:v2.3.1", "limit": "1Gi", "labels": {"app": "demo-app"}, "age_min": 40,
         "pods": [{"suffix": "n2b5v", "node": "cce-test-node-01", "ip": "172.18.1.10", "waiting": "ImagePullBackOff",
                   "waiting_msg": f'Back-off pulling image "{IMG}/test/demo-app:v2.3.1"'}]},
        {"ns": "test", "kind": "Deployment", "workload": "mock-pay", "hash": "6c9d8e7f5", "container": "mock",
         "image": f"{IMG}/test/mock-pay:0.9.0", "limit": "512Mi", "labels": {"app": "mock-pay"},
         "pods": [{"suffix": "y7u8i", "node": "cce-test-node-02", "ip": "172.18.2.11"}]},
    ],
}
# K8s Event（first_min / last_min = 首次 / 最近一次距今分钟）
K8S_EVENTS: dict[str, list[dict]] = {
    "cce-prod-01": [
        {"kind": "Deployment", "ns": "trade", "name": "order-service", "reason": "ScalingReplicaSet", "type": "Normal",
         "message": "Scaled up replica set order-service-7d9f8c6b5 to 4", "first_min": 9,
         "component": "deployment-controller"},
        {"kind": "Pod", "ns": "trade", "name": "order-service-7d9f8c6b5-p5r7t", "reason": "FailedScheduling",
         "message": "0/6 nodes are available: 6 Insufficient memory. preemption: 0/6 nodes are available: "
                    "6 No preemption victims found for incoming pod.", "first_min": 9, "last_min": 1, "count": 8,
         "component": "default-scheduler"},
        {"kind": "Pod", "ns": "member", "name": "member-api-84c6d9f7b-z5x8c", "container": "api", "reason": "Unhealthy",
         "message": "Readiness probe failed: HTTP probe failed with statuscode: 503", "first_min": 14, "last_min": 1,
         "count": 27},
        {"kind": "Node", "name": "cce-prod-node-03", "reason": "EvictionThresholdMet",
         "message": "Attempting to reclaim ephemeral-storage", "first_min": 38, "last_min": 33, "count": 2,
         "host": "cce-prod-node-03"},
    ],
    "cce-test-01": [
        {"kind": "Pod", "ns": "test", "name": "demo-app-58f9c7d6b-n2b5v", "container": "app", "reason": "Failed",
         "message": f'Failed to pull image "{IMG}/test/demo-app:v2.3.1": rpc error: code = NotFound desc = failed to '
                    "pull and unpack image: not found", "first_min": 40, "last_min": 2, "count": 11},
    ],
}

# ---------------- DataArts Studio 作业（巡检；MRS 作业经作业实例节点获取） ----------------
RT_JOBS = {"rt_order_sync": "NORMAL", "rt_payment_risk": "EXCEPTION", "rt_inventory_cdc": "STOPPED",
           "rt_user_behavior": "NORMAL"}                   # rt_user_behavior 作业正常但聚合节点异常
BATCH_JOBS = [  # 作业名, 实例状态, 计划时间距今分钟, 运行分钟（None = 运行中）
    ("ods_crm_customer_sync", "success", 300, 18), ("dwd_order_detail_daily", "success", 240, 42),
    ("dws_settle_daily", "success", 1500, 15), ("dws_settle_daily", "fail", 55, 12),
    ("ads_report_gen", "running", 110, None), ("dim_product_refresh", "success", 30, 6),
]
# 批作业实例内的节点：作业名 → [(节点名, 节点类型)]。type 为 MRSSpark / MapReduce / MRSFlinkJob / MRSHetuEngine 的节点
# 即 MRS 作业（数据治理中心 API §5.3.17 表 5-182）；其余类型只用于验证采集器会过滤非 MRS 节点。
JOB_NODES = {
    "ods_crm_customer_sync": [("cdm_sync", "CDMJob")],
    "dwd_order_detail_daily": [("prepare", "Shell"), ("spark_etl", "MRSSpark")],
    "dws_settle_daily": [("hive_agg", "SparkSQL"), ("spark_settle", "MRSSpark"), ("mr_export", "MapReduce")],
    "ads_report_gen": [("flink_report", "MRSFlinkJob")],
}
