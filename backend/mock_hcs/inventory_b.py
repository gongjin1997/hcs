"""租户B（互联网业务）的演示资源清单：门户网站、内容管理（CMS）、站内搜索、批处理。

与租户A 的差异（用于演示多租户接入的不同情况）：
- 接入方式 AK/SK（请求签名），不是用户名密码换 Token
- 局点没有部署云监控 CES：CES 接口返回 404，ECS 使用率改由租户自建的 Prometheus（node_exporter）提供
- 没有 DataArts / MRS / GaussDB；只有 1 个 CCE 集群
"""
import uuid
from datetime import timedelta
from typing import Any

from .inventory import AZ1, AZ2, FLAVORS, NOW, ago, fixed, iso  # noqa: F401  openapi.py 通过数据集模块访问

NS = uuid.UUID("2b7e3c9a-5d14-4f6e-8a21-9c0d7e6f5a43")


def rid(kind: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}:{name}"))


VPCS = [("vpc-web", "10.20.0.0/16", "互联网业务 VPC")]
SUBNETS = [("subnet-web-front", "vpc-web", "10.20.1.0/24", AZ1), ("subnet-web-app", "vpc-web", "10.20.2.0/24", AZ1),
           ("subnet-web-data", "vpc-web", "10.20.3.0/24", AZ2), ("subnet-web-cce", "vpc-web", "10.20.8.0/22", AZ1),
           ("subnet-web-ops", "vpc-web", "10.20.9.0/24", AZ2)]
SUBNET_VPC = {s[0]: s[1] for s in SUBNETS}

EULER, UBUNTU, KYLIN = "EulerOS 2.9 64bit", "Ubuntu 22.04 server 64bit", "Kylin V10 SP2 64bit"
ECS: list[tuple] = [
    ("ecs-portal-01", "c6.2xlarge.2", EULER, "subnet-web-front", "10.20.1.11", AZ1, "ACTIVE", fixed("2025-05-08 10:00"), 40, []),
    ("ecs-portal-02", "c6.2xlarge.2", EULER, "subnet-web-front", "10.20.1.12", AZ2, "ACTIVE", fixed("2025-05-08 10:03"), 40, []),
    ("ecs-portal-03", "c6.2xlarge.2", EULER, "subnet-web-front", "10.20.1.13", AZ1, "ACTIVE", ago(5), 40, []),
    ("ecs-cms-01", "s6.2xlarge.2", EULER, "subnet-web-app", "10.20.2.11", AZ1, "ACTIVE", fixed("2025-05-09 14:00"), 40, [(200, "SSD")]),
    ("ecs-cms-02", "s6.2xlarge.2", EULER, "subnet-web-app", "10.20.2.12", AZ2, "ACTIVE", fixed("2025-05-09 14:05"), 40, [(200, "SSD")]),
    ("ecs-search-01", "c6.4xlarge.2", EULER, "subnet-web-app", "10.20.2.21", AZ1, "ERROR", fixed("2025-06-12 09:30"), 50, [(500, "SSD")]),
    ("ecs-batch-01", "c6.4xlarge.2", KYLIN, "subnet-web-app", "10.20.2.31", AZ1, "ACTIVE", fixed("2025-07-01 11:00"), 50, [(500, "SAS")]),
    ("ecs-batch-02", "c6.4xlarge.2", KYLIN, "subnet-web-app", "10.20.2.32", AZ2, "MIGRATING", fixed("2025-07-01 11:05"), 50, [(500, "SAS")]),
    ("ecs-stage-01", "s6.xlarge.2", UBUNTU, "subnet-web-app", "10.20.2.41", AZ1, "SHUTOFF", fixed("2025-08-20 16:00"), 40, []),
    ("ecs-ops-01", "s6.large.2", UBUNTU, "subnet-web-ops", "10.20.9.11", AZ2, "ACTIVE", fixed("2025-05-08 09:00"), 40, []),
    ("ecs-prom-01", "s6.xlarge.4", UBUNTU, "subnet-web-ops", "10.20.9.21", AZ2, "ACTIVE", fixed("2025-05-10 10:00"), 40, [(500, "SAS")]),
    *[(f"cce-web-node-0{i}", "c6.2xlarge.4", EULER, "subnet-web-cce", f"10.20.8.{10 + i}", AZ1 if i % 2 else AZ2, "ACTIVE",
       fixed(f"2025-05-1{i} 10:00"), 50, [(100, "SSD")]) for i in range(1, 5)],
]
SPARE_VOLUMES = [("vol-web-old-logs", 300, "SAS", AZ1, fixed("2025-09-02 18:00"))]
ELBS = [("elb-portal", "10.20.1.100", "subnet-web-front", True, 2, 3, fixed("2025-05-08 12:00"))]
EIPS = [("eip-elb-portal", "198.51.100.11", "ACTIVE", "10.20.1.100", 300, None, fixed("2025-05-08 12:05")),
        ("eip-ops-01", "198.51.100.21", "ACTIVE", "10.20.9.11", 10, None, fixed("2025-05-08 09:10")),
        ("eip-web-spare", "198.51.100.31", "DOWN", None, 50, None, fixed("2025-08-21 10:00"))]
EIP_BY_IP = {e[3]: e for e in EIPS if e[3]}
RDS = [("rds-mysql-cms", "MySQL", "8.0", "Ha", 4, 16, 500, "10.20.3.21", "rds.mysql.x1.xlarge.4.ha", fixed("2025-05-09 10:00")),
       ("rds-pg-portal", "PostgreSQL", "14", "Ha", 2, 8, 200, "10.20.3.22", "rds.pg.x1.large.4.ha", fixed("2025-05-09 10:30"))]
GAUSSDB: list[tuple] = []
DCS: list[dict[str, Any]] = [
    {"name": "redis-portal-01", "status": "RUNNING", "ver": "5.0", "cap": 8, "used": 6011, "ip": "10.20.3.41",
     "ces": (30.2, 2100), "created": fixed("2025-05-08 11:00")},
    {"name": "redis-cms-01", "status": "RUNNING", "ver": "6.0", "cap": 4, "used": 1630, "ip": "10.20.3.42",
     "ces": (12.0, 380), "created": fixed("2025-05-09 11:00")},
]
KAFKA = [("kafka-web-01", "kafka.2u4g.cluster", "2.7", 600, 188, 150, "10.20.3.51", fixed("2025-05-12 10:00"))]
MRS: list[tuple] = []
MRS_COMPONENTS: dict[str, list[str]] = {"default": []}
CCE = [("cce-web-01", "v1.25", "cce.s2.small", "10.20.8.10", "cce-web-node-", fixed("2025-05-11 09:00"))]
DWS: list[tuple] = []
CSS = [("css-web-search", "7.10.2", "ess.spec-4u16g", 3, "10.20.3.61", fixed("2025-06-12 10:00"))]

# ---------------- 运行态（Prometheus node_exporter 提供使用率与开机时间） ----------------
AGENT_PREFIXES = ("ecs-", "cce-web-node-")       # node_exporter：ECS 用 systemd 部署，CCE 节点用 DaemonSet
ECS_USAGE: dict[str, dict[str, Any]] = {
    "ecs-batch-01": {"cpu": 66.0, "mem": 92.4},            # 内存 ≥ 90% → P3
    "ecs-prom-01": {"disks": {"/data": 87.6}},             # Prometheus TSDB 数据盘 ≥ 85% → P3
    "ecs-cms-02": {"cpu": 35.5},
}
DEFAULT_UPTIME = timedelta(days=21, hours=3)
BOOT_AGO = {"ecs-cms-02": timedelta(minutes=25), "ecs-portal-03": timedelta(days=5)}

IMG = "swr.region-01.hcs.example.com"
WORKLOADS: dict[str, list[dict]] = {
    "cce-web-01": [
        {"ns": "web", "kind": "Deployment", "workload": "portal-web", "hash": "5b6c7d8e9", "container": "nginx",
         "image": f"{IMG}/web/portal-web:4.6.0", "limit": "512Mi", "labels": {"app": "portal-web"},
         "pods": [{"suffix": "a8s7d", "node": "cce-web-node-01", "ip": "172.20.1.11"},
                  {"suffix": "f6g5h", "node": "cce-web-node-02", "ip": "172.20.2.12"},
                  {"suffix": "j4k3l", "node": "cce-web-node-03", "ip": "172.20.3.13", "age_min": 180}]},
        {"ns": "web", "kind": "Deployment", "workload": "portal-api", "hash": "6d8f9a7b5", "container": "api",
         "image": f"{IMG}/web/portal-api:2.9.1", "limit": "1Gi", "labels": {"app": "portal-api"},
         "pods": [{"suffix": "q8w2e", "node": "cce-web-node-01", "ip": "172.20.1.33", "restarts": 7,
                   "last": ("Error", 137, 2)},
                  {"suffix": "r5t6y", "node": "cce-web-node-04", "ip": "172.20.4.34"},
                  {"suffix": "u7i8o", "node": "cce-web-node-02", "ip": "172.20.2.35"}]},
        {"ns": "cms", "kind": "Deployment", "workload": "cms-backend", "hash": "7c9b8a6d5", "container": "backend",
         "image": f"{IMG}/cms/cms-backend:3.3.2", "limit": "2Gi", "labels": {"app": "cms-backend"},
         "pods": [{"suffix": "z1x2c", "node": "cce-web-node-03", "ip": "172.20.3.41"},
                  {"suffix": "v3b4n", "node": "cce-web-node-04", "ip": "172.20.4.42"}]},
        {"ns": "cms", "kind": "Deployment", "workload": "cms-worker", "hash": "7f6d5c4b3", "container": "worker",
         "image": f"{IMG}/cms/cms-worker:3.3.2", "limit": "2Gi", "labels": {"app": "cms-worker"},
         "pods": [{"suffix": "g7h8j", "node": "cce-web-node-02", "ip": "172.20.2.51"},
                  {"suffix": "k9l0p", "node": "cce-web-node-03", "ip": "172.20.3.52"}]},
        {"ns": "batch", "kind": "CronJob", "workload": "report-job", "job": "29329120", "container": "job",
         "image": f"{IMG}/batch/report-job:1.2.0", "limit": "1Gi", "labels": {"app": "report-job"},
         "pods": [{"suffix": "m3n4b", "node": "cce-web-node-04", "ip": "172.20.4.61", "completed": True,
                   "age_min": 70}]},
        {"ns": "batch", "kind": "Deployment", "workload": "image-resize", "hash": "8e7d6c5b4", "container": "resize",
         "image": f"{IMG}/batch/image-resize:0.8.3", "limit": "1Gi", "labels": {"app": "image-resize"},
         "pods": [{"suffix": "s2d3f", "node": "cce-web-node-01", "ip": "172.20.1.71", "restarts": 2,
                   "last": ("Error", 1, 300)}]},
        {"ns": "monitoring", "kind": "DaemonSet", "workload": "node-exporter", "container": "node-exporter",
         "image": f"{IMG}/monitoring/node-exporter:1.6.1", "limit": "128Mi", "labels": {"app": "node-exporter"},
         "age_min": 160 * 1440,
         "pods": [{"suffix": f"ne{i}x{i}", "node": f"cce-web-node-0{i}", "ip": f"10.20.8.{10 + i}"} for i in range(1, 5)]},
        {"ns": "kube-system", "kind": "Deployment", "workload": "coredns", "hash": "6d4b75cb6", "container": "coredns",
         "image": f"{IMG}/hwofficial/coredns:1.25.1", "limit": "512Mi", "labels": {"k8s-app": "coredns"},
         "age_min": 160 * 1440,
         "pods": [{"suffix": "p9o8i", "node": "cce-web-node-01", "ip": "172.20.1.2"},
                  {"suffix": "u7y6t", "node": "cce-web-node-02", "ip": "172.20.2.2"}]},
    ],
}
K8S_EVENTS: dict[str, list[dict]] = {
    "cce-web-01": [
        {"kind": "Pod", "ns": "cms", "name": "cms-worker-7f6d5c4b3-g7h8j", "container": "worker", "reason": "BackPressure",
         "message": "消费 Kafka topic cms-publish 积压 125000 条，下游 css-web-search 写入背压（bulk rejected）",
         "first_min": 16, "last_min": 1, "count": 9, "component": "cms-worker"},
        {"kind": "Pod", "ns": "web", "name": "portal-api-6d8f9a7b5-q8w2e", "container": "api", "reason": "Unhealthy",
         "message": 'Liveness probe failed: Get "http://172.20.1.33:8080/actuator/health": context deadline exceeded',
         "first_min": 11, "last_min": 3, "count": 6},
        {"kind": "Deployment", "ns": "web", "name": "portal-web", "reason": "ScalingReplicaSet", "type": "Normal",
         "message": "Scaled up replica set portal-web-5b6c7d8e9 to 3", "first_min": 180,
         "component": "deployment-controller"},
    ],
}

# ---------------- 巡检：租户B 没有 DataArts / MRS ----------------
RT_JOBS: dict[str, str] = {}
BATCH_JOBS: list[tuple] = []
MRS_JOBS: dict[str, list[tuple]] = {}
