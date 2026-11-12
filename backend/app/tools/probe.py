"""HCS 接口探测（局域网联调用）：逐个调用平台用到的 HCS 接口，核对返回字段与平台的字段映射是否一致。

用法（在 api 容器内执行，读取数据库里的“租户接入”配置，凭据不会出现在命令行和报告里）：
    docker-compose exec api python -m app.tools.probe --list
    docker-compose exec api python -m app.tools.probe --tenant tenant-a
    docker-compose exec api python -m app.tools.probe --tenant tenant-a --only ecs,dcs --limit 20 \\
        --out /app/data/probe-tenant-a.json

每个接口输出：结果（OK / FAIL / SKIP）、耗时、返回条数与总数、缺失的必需字段 / 可选字段、
状态类字段的取值分布（用来核对状态映射和规则表达式）、脱敏后的样例结构（--out 写入 JSON 报告）。
- 必需字段缺失 = 台账同步 / 巡检会直接报错，必须先改字段映射（app/ledger/fetchers.py、app/collectors/*.py）
- 可选字段缺失 = 对应列为空或该检查项不生效，按需调整
- 样例里的 ID、IP、名称默认脱敏；--raw 保留原值（只在本机排查时使用，不要外发）
结果：OK 正常 / FAIL 失败（必需接口不通或缺必需字段）/ WARN 已配置的可选能力不可用（CCE K8s API 等）/
SKIP 跳过（AOM 已停用、没有前置资源、未配置工作空间、该租户已关闭这类资源的采集）。
退出码：0 没有 FAIL；1 有 FAIL；2 认证失败或参数错误。
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from app import __version__
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import sanitize_log
from app.hcs import HcsClient, HcsError, client_for
from app.hcs.auth import TokenAuth
from app.hcs.cce_api import available_cluster, management_items
from app.hcs.gaussdb_api import gaussdb_instances_path
from app.hcs.pager import response_total
from app.k8s.kube import SERVICE as K8S
from app.metrics.ces import ecs_metrics
from app.models import HcsAccount, HcsProject
from app.services import collect_switch
from app.services.ecs_quotas import QUOTAS


@dataclass
class Probe:
    code: str
    service: str
    name: str
    path: str
    items: str                              # 列表字段名；空字符串 = 返回本身就是对象
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)          # 统计取值分布的状态类字段
    params: dict = field(default_factory=dict)
    total: str | None = None                # 总数字段（分页结束判断用）
    needs: str | None = None                # workspace / cce_cluster / mrs_cluster / dgc_job / dgc_instance
    limit_param: str | None = "limit"
    max_limit: int | None = None
    used_by: str = ""
    types: list[str] = field(default_factory=list)           # 对应的采集开关（全部关闭时跳过）
    capability: bool = False                # 已配置的可选能力：失败记 WARN，不算必需接口失败
    method: str = "GET"
    body: dict | None = None


P = Probe
TYPES = {"ecs.servers": ["ecs_server"], "ecs.server": ["ecs_server"], "ecs.quotas": ["ecs_server"],
         "evs.volumes": ["evs_volume"], "vpc.vpcs": ["vpc"],
         "vpc.subnets": ["vpc_subnet"], "elb.loadbalancers": ["elb"], "vpc.publicips": ["eip"],
         "gaussdb.instances": ["gaussdb_instance"], "dcs.instances": ["dcs_instance"],
         "dcs.statistics": ["dcs_instance"], "dcs.nodes": ["dcs_instance"],
         "mrs.clusters": ["mrs_cluster", "mrs_job"], "mrs.jobs": ["mrs_job"],
         "dws.clusters": ["dws_cluster"], "cce.clusters": ["cce_cluster", "cce_workload"],
         "cce.nodes": ["cce_cluster"], "cce.nodepools": ["cce_cluster"],
         "k8s.pods": ["cce_workload"], "k8s.events": ["cce_workload"],
         "dataarts.jobs": ["dgc_realtime_job"], "dataarts.job_status": ["dgc_realtime_job"],
         "dataarts.batch_jobs": ["dgc_batch_job"], "dataarts.job_definition": ["dgc_realtime_job"],
         "dataarts.instances": ["dgc_batch_job", "dgc_mrs_node"], "dataarts.instance_detail": ["dgc_mrs_node"]}
PROBES: list[Probe] = [
    P("ecs.servers", "ecs", "ECS 云服务器列表", "/v1/{project_id}/cloudservers/detail", "servers", ["id", "name"],
      ["status", "flavor.id", "flavor.vcpus", "flavor.ram", "addresses", "OS-EXT-AZ:availability_zone",
       "OS-EXT-STS:power_state", "OS-EXT-STS:task_state", "metadata.image_name", "metadata.vpc_id",
       "metadata.charging_mode", "created"], ["status", "OS-EXT-STS:task_state"], {"offset": 1}, "count",
      used_by="台账（云服务器）/ ECS 巡检"),
    P("evs.volumes", "evs", "EVS 云硬盘列表", "/v2/{project_id}/cloudvolumes/detail", "volumes", ["id"],
      ["name", "status", "size", "volume_type", "availability_zone", "attachments[].server_id", "attachments[].device",
       "bootable", "encrypted", "created_at"], ["status", "volume_type"],
      used_by="台账（云硬盘、云服务器磁盘容量）"),
    P("vpc.vpcs", "vpc", "VPC 列表", "/v1/{project_id}/vpcs", "vpcs", ["id", "name"], ["status", "cidr", "description"],
      ["status"], used_by="台账（VPC）"),
    P("vpc.subnets", "vpc", "子网列表", "/v1/{project_id}/subnets", "subnets", ["id", "name"],
      ["status", "cidr", "gateway_ip", "vpc_id", "availability_zone", "dhcp_enable"], ["status"], used_by="台账（子网）"),
    P("elb.loadbalancers", "elb", "ELB 负载均衡列表", "/v3/{project_id}/elb/loadbalancers", "loadbalancers",
      ["id", "name"], ["operating_status", "provisioning_status", "vip_address", "eips[].eip_address",
                       "availability_zone_list", "guaranteed", "listeners", "vpc_id", "created_at"],
      ["operating_status", "provisioning_status"], limit_param=None, used_by="台账（负载均衡）"),
    P("vpc.publicips", "vpc", "EIP 弹性公网 IP 列表", "/v1/{project_id}/publicips", "publicips",
      ["id", "public_ip_address"], ["alias", "status", "private_ip_address", "bandwidth_size", "bandwidth_share_type",
                                    "bandwidth_name", "create_time"], ["status"], used_by="台账（弹性公网 IP）"),
    P("gaussdb.instances", "gaussdb", "GaussDB 实例列表", gaussdb_instances_path(), "instances", ["id", "name"],
      ["status", "type", "datastore.version", "flavor_ref", "flavor_info.vcpu", "flavor_info.mem",
       "volume.size", "availability_zone", "private_ips",
       "nodes[].id", "nodes[].name", "nodes[].role", "nodes[].status", "nodes[].availability_zone", "created"],
      ["status", "nodes[].status", "nodes[].role"],
      {"offset": 0}, "total_count", used_by="台账（GaussDB）/ GaussDB 巡检"),
    P("dcs.instances", "dcs", "DCS 缓存实例列表", "/v2/{project_id}/instances", "instances", ["instance_id", "name"],
      ["status", "engine", "engine_version", "capacity", "capacity_minor", "spec_code", "ip", "port", "max_memory",
       "used_memory", "az_codes", "created_at"], ["status", "engine"], {"offset": 0}, "instance_num",
      used_by="台账（DCS）/ DCS 巡检"),
    P("dcs.statistics", "dcs", "DCS 运行实例统计", "/v2/{project_id}/instances/statistic", "statistics", ["instance_id"],
      ["used_memory", "max_memory", "used_cpu", "input_kbps", "output_kbps", "keys", "cmd_get_count", "cmd_set_count"],
      limit_param=None, used_by="DCS 原生内存巡检（内存 MB，used_cpu 为累计秒数）"),
    P("dcs.nodes", "dcs", "DCS 实例逻辑节点（分片 / 主从 / Proxy）", "/v2/{project_id}/instances-logical-nodes", "instances",
      ["instance_id"], ["node_count", "nodes[].name", "nodes[].status", "nodes[].node_role", "nodes[].node_type",
                        "nodes[].node_ip", "nodes[].node_port", "nodes[].group_id", "nodes[].group_name",
                        "nodes[].az_code"], ["nodes[].status", "nodes[].node_type"], {"offset": 0}, "count",
      max_limit=100, used_by="DCS 节点巡检（主节点 / 从节点 / Proxy 故障；Redis 4.0 及以上）"),
    P("mrs.clusters", "mrs", "MRS 集群列表", "/v1.1/{project_id}/cluster_infos", "clusters",
      ["clusterId", "clusterName"], ["clusterState", "clusterVersion", "totalNodeNum", "azName", "masterNodeIp",
                                     "componentList[].componentName", "createAt"], ["clusterState"],
      {"currentPage": 1}, "clusterTotal", limit_param="pageSize", used_by="台账（MRS）/ MRS 集群巡检"),
    P("dws.clusters", "dws", "DWS 集群列表", "/v1.0/{project_id}/clusters", "clusters", ["id", "name"],
      ["status", "availability_zone", "endpoints[].connect_info", "node_type", "number_of_node", "version", "created"],
      ["status"], total="count", limit_param=None, used_by="台账（DWS）"),
    P("cce.clusters", "cce", "CCE 集群列表", "/api/v3/projects/{project_id}/clusters", "items",
      ["metadata.uid", "metadata.name"], ["status.phase", "status.endpoints", "spec.version", "spec.flavor", "spec.type",
                                          "metadata.creationTimestamp"], ["status.phase"], limit_param=None,
      used_by="台账（CCE 集群）/ 容器事件采集"),
    P("cce.nodes", "cce", "CCE 节点列表（第一个集群）", "/api/v3/projects/{project_id}/clusters/{cluster_uid}/nodes",
      "items", [], ["metadata.name", "status.phase"], ["status.phase"], limit_param=None, needs="cce_cluster",
      used_by="台账 / CCE 节点健康（lastProbeTime 超过 5 分钟不可作为当前健康依据）"),
    P("cce.nodepools", "cce", "CCE 节点池（包含默认池）",
      "/api/v3/projects/{project_id}/clusters/{cluster_uid}/nodepools", "items", ["metadata.uid"],
      ["status.currentNode", "status.creatingNode", "status.deletingNode", "status.conditions"], [],
      {"showDefaultNodePool": "true"}, limit_param=None, needs="cce_cluster", capability=True,
      used_by="CCE 节点池只读视图"),
    P("k8s.pods", K8S, "K8s Pod 列表（第一个 CCE 集群）", "/api/v1/pods", "items",
      ["metadata.name", "metadata.namespace"], ["metadata.ownerReferences[].kind", "metadata.ownerReferences[].name",
                                                "spec.nodeName", "status.phase", "status.containerStatuses[].restartCount",
                                                "status.containerStatuses[].lastState"], ["status.phase"],
      needs="cce_cluster", used_by="容器事件采集（需要集群只读 RBAC）"),
    P("k8s.events", K8S, "K8s 事件列表（第一个 CCE 集群）", "/api/v1/events", "items", ["reason"],
      ["type", "message", "count", "lastTimestamp", "involvedObject.kind", "involvedObject.name",
       "involvedObject.namespace"], ["type", "reason"], needs="cce_cluster", used_by="容器事件采集"),
    P("dataarts.jobs", "dataarts", "DataArts 实时作业列表", "/v1/{project_id}/jobs", "jobs", ["name"],
      ["owner", "priority", "path", "jobType"], ["jobType"], {"jobType": "REAL_TIME", "offset": 0}, "total",
      needs="workspace", used_by="DataArts 实时作业巡检"),
    P("dataarts.job_status", "dataarts", "DataArts 作业状态（第一个实时作业）", "/v1/{project_id}/jobs/{job_name}/status",
      "", [], ["status", "nodes[].name", "nodes[].status"], ["status", "nodes[].status"], limit_param=None,
      needs="dgc_job", used_by="DataArts 实时作业巡检"),
    P("dataarts.instances", "dataarts", "DataArts 批作业实例（近 24 小时）", "/v1/{project_id}/jobs/instances/detail",
      "instances", ["jobName"], ["instanceId", "status", "planTime", "startTime", "endTime"], ["status"],
      {"offset": 0}, "total", needs="workspace", used_by="DataArts 批作业巡检"),
    P("dataarts.instance_detail", "dataarts", "DataArts 作业实例详情（第一个实例的节点）",
      "/v1/{project_id}/jobs/{job_name}/instances/{instance_id}", "nodes", ["nodeName", "status", "type"],
      ["startTime", "endTime", "retryTimes", "logPath"], ["type", "status"], limit_param=None, needs="dgc_instance",
      used_by="DGC 编排节点巡检（MRS/Spark SQL/Hive SQL 节点；不是原生执行）"),
    P("ces.metrics", "ces", "CES 指标列表（SYS.ECS）", "/V1.0/{project_id}/metrics", "metrics", [],
      ["namespace", "metric_name", "dimensions[].name", "unit"], ["metric_name"], {"namespace": "SYS.ECS"},
      used_by="旧部署显式启用的 CES 兼容指标（不用于 DCS 内存）"),
    P("aom.metrics", "aom", "AOM（已停用，不发起请求）", "/v1/{project_id}/ams/metrics", "metrics",
      ["namespace", "metricName"], ["dimensions[].name", "unit"], params={"limit": 1, "start": 0},
      limit_param=None, used_by="所有资源均停用 AOM；保留此条目用于输出跳过说明",
      method="POST", body={"metricItems": [{"namespace": "PAAS.NODE"}]}),
]

PROBES.extend([
    P("dataarts.batch_jobs", "dataarts", "DataArts 批作业定义", "/v1/{project_id}/jobs", "jobs", ["name", "jobType"],
      ["status", "lastInstanceStatus", "lastInstanceEndTime"], ["jobType", "status"],
      {"jobType": "BATCH", "offset": 0}, "total", needs="workspace", used_by="批定义清单；不受24小时实例窗口限制"),
    P("dataarts.job_definition", "dataarts", "DataArts 作业定义与引擎（第一个实时作业）",
      "/v1/{project_id}/jobs/{job_name}", "", ["name", "processType"],
      ["singleNodeJobType", "nodes[].type"], ["processType", "singleNodeJobType", "nodes[].type"],
      limit_param=None, needs="dgc_job", used_by="FlinkSQL/FlinkJar及编排引擎识别"),
    P("mrs.jobs", "mrs", "MRS 原生作业执行（第一个集群）",
      "/v2/{project_id}/clusters/{cluster_id}/job-executions", "job_list", ["job_id", "job_name"],
      ["job_type", "job_state", "job_result", "submitted_time", "started_time", "finished_time", "elapsed_time"],
      ["job_type", "job_state", "job_result"], {"offset": 1}, "total_record", needs="mrs_cluster",
      used_by="MRS 原生执行兼容接口；与DGC节点分开，elapsed_time单位不猜测"),
])

PROBES.append(P("ecs.quotas", "ecs", "ECS 项目实际配额", "/v1/{project_id}/cloudservers/limits", "",
    required=["absolute"], optional=[f"absolute.{field}" for quota in QUOTAS
                                   for field in (quota.limit, quota.used) if field],
    limit_param=None, used_by="ECS 项目配额（原生 limits；路径 tenant_id 实为项目 ID）"))
PROBES.append(P("ecs.server", "ecs", "ECS 单机状态", "/v1/{project_id}/cloudservers/{server_id}", "",
    required=["server.id", "server.name"], optional=["server.status", "server.OS-EXT-STS:task_state"],
    params={"expect_fields": "basic"}, needs="ecs_server", limit_param=None,
    used_by="所选少量关键实例的独立状态巡检"))

DEPENDS = {"cce_cluster": "cce.clusters", "mrs_cluster": "mrs.clusters", "dgc_job": "dataarts.jobs",
           "dgc_instance": "dataarts.instances",
           "workspace": "dataarts.jobs", "ecs_server": "ecs.servers"}
for _p in PROBES:
    _p.types = TYPES.get(_p.code, [])
    _p.capability = _p.service in ("ces", "aom", K8S) or _p.code == "cce.nodepools"

IP = re.compile(r"\b(\d{1,3})\.\d{1,3}\.\d{1,3}\.(\d{1,3})\b")
HEXID = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{15,}$")
NAME_KEYS = {"name", "alias", "clusterName", "jobName", "job_name", "description", "path", "owner", "bandwidth_name",
             "image_name", "namespace", "nodeName", "message", "componentName", "queue"}


def _mask(key: str, v: Any) -> Any:
    if isinstance(v, dict):                      # 字典的键也可能是 ID（例如 ECS addresses 的键是 VPC ID）
        return {(k[:4] + "…" + k[-2:] if HEXID.match(k) else k): _mask(k, x) for k, x in v.items()}
    if isinstance(v, list):
        out = [_mask(key, x) for x in v[:2]]
        return out + [f"…（共 {len(v)} 项）"] if len(v) > 2 else out
    if not isinstance(v, str) or not v:
        return v
    if HEXID.match(v) or (key.lower().endswith("id") or key in ("marker", "uid")) and len(v) >= 16:
        return v[:4] + "…" + v[-2:] if len(v) > 8 else "…"
    if key in NAME_KEYS or key.endswith("_name") and not key.startswith("metric"):
        return v[:2] + "***"
    v = IP.sub(r"\1.*.*.\2", v)
    return v if len(v) <= 48 else v[:40] + "…"


def _walk(obj: Any, path: str) -> tuple[str, Any]:
    """按 a.b[].c 取值：返回（状态, 值）。状态：ok / null / missing / empty（列表为空，无法核对）。"""
    cur = obj
    for part in path.split("."):
        is_list = part.endswith("[]")
        key = part[:-2] if is_list else part
        if not isinstance(cur, dict) or key not in cur:
            return "missing", None
        cur = cur[key]
        if cur is None:
            return "null", None
        if is_list:
            if not isinstance(cur, list):
                return "missing", None
            if not cur:
                return "empty", None
            cur = cur[0]
    return "ok", cur


def _values(items: list[dict], path: str) -> dict[str, int]:
    c: Counter[str] = Counter()
    for it in items:
        if "[]" in path:                         # 列表内字段：统计所有元素
            head, _, rest = path.partition("[].")
            st, lst = _walk(it, head)
            vals = [_walk(x, rest)[1] for x in (lst if st == "ok" and isinstance(lst, list) else [])]
        else:
            vals = [_walk(it, path)[1]]
        c.update(str(v) for v in vals if v is not None)
    return dict(c.most_common(20))


class Runner:
    def __init__(self, client: HcsClient | None, project: HcsProject, limit: int, raw: bool, off: set[str] | None = None):
        self.client, self.project, self.limit, self.raw = client, project, limit, raw
        self.off = off or set()                  # 该租户已关闭采集的资源类型
        self.ctx: dict[str, str] = {}
        self.results: list[dict] = []

    def _headers(self) -> dict | None:
        ws = self.ctx.get("workspace")
        return {"workspace": ws} if ws else None

    def skip_reason(self, p: Probe) -> str | None:
        if p.service == "ces" and not get_settings().hcs_ces_enabled:
            return "该部署未启用 CES，未发起探测"
        if p.service == "ces" and not getattr(self.client, "ces_enabled", False):
            return "该租户未明确选择 CES，未发起探测"
        if p.service == "aom":
            return "AOM 已全局停用，未发起认证或接口请求（--all 也不启用）"
        if p.types and all(t in self.off for t in p.types):
            return "该租户已关闭这类资源的采集（--all 仍然探测）"
        if (p.needs == "workspace" or p.service == "dataarts") and not self.ctx.get("workspace"):
            return "项目未配置 DataArts 工作空间（租户接入 → 项目 → 工作空间）"
        return None

    def run(self, p: Probe) -> dict:  # noqa: C901  # T10
        res: dict[str, Any] = {"code": p.code, "name": p.name, "service": p.service, "used_by": p.used_by}
        if p.service == "aom":
            res.update(evidence_scope="disabled", product_metrics_verified=False, available=False,
                       contract_note="所有资源（含 ECS）均停用 AOM；旧地址、映射和元数据结果不代表当前可用")
        reason = self.skip_reason(p)
        if reason:
            return {**res, "result": "SKIP", "message": reason}
        if self.client is None:
            return {**res, "result": "FAIL", "message": "未创建查询客户端"}
        path, host_vars, headers = p.path, None, None
        params = dict(p.params)
        if p.limit_param:
            params[p.limit_param] = min(self.limit, p.max_limit) if p.max_limit is not None else self.limit
        if p.needs == "workspace" or p.service == "dataarts":
            if not self.ctx.get("workspace"):
                return {**res, "result": "SKIP", "message": "项目未配置 DataArts 工作空间（租户接入 → 项目 → 工作空间）"}
            headers = self._headers()
        if p.needs == "mrs_cluster":
            if not self.ctx.get("mrs_cluster"):
                return {**res, "result": "SKIP", "message": "没有 MRS 集群（或集群列表调用失败）"}
            path = path.replace("{cluster_id}", quote(self.ctx["mrs_cluster"], safe=""))
        if p.needs == "cce_cluster":
            if not self.ctx.get("cce_cluster"):
                return {**res, "result": "SKIP", "message": "没有 CCE 集群（或 CCE 集群列表调用失败）"}
            path = path.replace("{cluster_uid}", quote(self.ctx["cce_cluster"], safe=""))
            if p.service == K8S:
                host_vars = {"cluster_id": self.ctx["cce_cluster"]}
        if p.needs == "dgc_instance":
            if not self.ctx.get("dgc_instance_job") or not self.ctx.get("dgc_instance_id"):
                return {**res, "result": "SKIP", "message": "近 24 小时没有作业实例（或实例列表调用失败）"}
            path = (path.replace("{job_name}", quote(self.ctx["dgc_instance_job"], safe=""))
                    .replace("{instance_id}", quote(self.ctx["dgc_instance_id"], safe="")))
        if p.needs == "dgc_job":
            if not self.ctx.get("dgc_job"):
                return {**res, "result": "SKIP", "message": "没有实时作业（或作业列表调用失败）"}
            path = path.replace("{job_name}", quote(self.ctx["dgc_job"], safe=""))
        if p.needs == "ecs_server":
            if not self.ctx.get("ecs_id"):
                return {**res, "result": "SKIP", "message": "没有 ECS 实例（或实例清单查询失败）"}
            path = path.replace("{server_id}", quote(self.ctx["ecs_id"], safe=""))
        if p.code == "dataarts.instances":
            now_ms = int(time.time() * 1000)
            params.update({"minPlanTime": now_ms - 24 * 3600 * 1000, "maxPlanTime": now_ms})
        started = time.perf_counter()
        try:
            kw: dict[str, Any] = {"params": params}
            if headers:
                kw["headers"] = headers
            if host_vars:
                kw["host_vars"] = host_vars
            if p.method == "POST":
                data = self.client.post(p.service, path, json=p.body, **kw)
            else:
                data = self.client.get(p.service, path, **kw)
        except (HcsError, KeyError) as e:
            return {**res, "result": "WARN" if p.capability else "FAIL",
                    "cost_ms": round((time.perf_counter() - started) * 1000), "message": _safe_error(e), "hint": _hint(e)}
        res["cost_ms"] = round((time.perf_counter() - started) * 1000)
        res["top_keys"] = sorted(data)[:30] if isinstance(data, dict) else []
        if p.items:
            items = data.get(p.items) if isinstance(data, dict) else None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                return {**res, "result": "WARN" if p.capability else "FAIL",
                        "message": f"返回中没有有效对象列表字段 {p.items}（实际字段：{res['top_keys']}）",
                        "hint": "接口版本或返回结构与平台预期不同，需要调整 items 字段名"}
        else:
            items = [data] if isinstance(data, dict) else []
        res["count"] = len(items)
        if p.code in ("cce.clusters", "cce.nodes", "cce.nodepools"):
            try:
                management_items(data)
            except HcsError as error:
                return {**res, "result": "WARN" if p.capability else "FAIL", "message": _safe_error(error)}
        if p.total:
            res["total"] = data.get(p.total) if isinstance(data, dict) else None
            if p.code in ("ecs.servers", "mrs.clusters", "mrs.jobs", "dataarts.jobs", "dataarts.batch_jobs",
                          "dataarts.instances"):
                try:
                    total = response_total(data, p.total, required=True, allow_string=p.code == "mrs.clusters")
                    if total is not None and (len(items) > total or not items and total > 0):
                        raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "列表记录数与已声明总数不一致")
                except HcsError as error:
                    return {**res, "result": "FAIL", "message": _safe_error(error)}
            if res["total"] is None and p.total in ("count", "total_count", "instance_num", "clusterTotal"):
                res["total_missing"] = f"没有总数字段 {p.total}；本次只检查样本，不证明清单完整"
        if p.code in ("elb.loadbalancers", "dws.clusters"):
            try:
                counts = data.get("page_info") if p.code == "elb.loadbalancers" else data
                if counts is not None:
                    if not isinstance(counts, dict):
                        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "上游清单计数结构不正确")
                    count = response_total(counts, "current_count" if p.code == "elb.loadbalancers" else "count")
                    if count is not None and count != len(items):
                        raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "无分页清单的记录数与已声明计数不一致")
            except HcsError as error:
                return {**res, "result": "FAIL", "message": _safe_error(error)}
        self._remember(p, items)
        if not items:
            return {**res, "result": "OK", "message": "接口正常，但没有数据（字段无法核对）"}
        first = items[0]
        missing_req = [f for f in p.required if _walk(first, f)[0] in ("missing", "null")]
        missing_opt = [f for f in p.optional if _walk(first, f)[0] == "missing"]
        null_opt = [f for f in p.optional if _walk(first, f)[0] == "null"]
        res.update({"missing_required": missing_req, "missing_optional": missing_opt, "null_optional": null_opt,
                    "values": {f: _values(items, f) for f in p.status},
                    "sample": sanitize_log(first if self.raw else _mask("", first))})
        res["result"] = ("WARN" if p.capability else "FAIL") if missing_req else "OK"
        if missing_req:
            res["message"] = "缺少必需字段：" + "、".join(missing_req)
        return res

    def _remember(self, p: Probe, items: list[dict]) -> None:
        if not items:
            return
        first = items[0]
        value: object = ""
        if p.code == "cce.clusters":
            self.ctx.pop("cce_cluster", None)
            cluster = available_cluster(items)
            if cluster is None:
                return
            value = cluster["metadata"]["uid"]
        elif p.code == "mrs.clusters":
            value = first.get("clusterId")
        elif p.code == "dataarts.jobs":
            value = first.get("name")
        elif p.code == "ecs.servers":
            value = first.get("id")
        elif p.code == "dataarts.instances":
            job, iid = first.get("jobName"), first.get("instanceId")
            self.ctx["dgc_instance_job"] = job if isinstance(job, str) else ""
            self.ctx["dgc_instance_id"] = str(iid) if type(iid) is int or isinstance(iid, str) and iid.isdecimal() else ""
        key = {"cce.clusters": "cce_cluster", "mrs.clusters": "mrs_cluster", "dataarts.jobs": "dgc_job",
               "ecs.servers": "ecs_id"}.get(p.code)
        if key is not None:
            self.ctx[key] = value if isinstance(value, str) else ""

    def ces_ecs(self) -> dict:
        """ECS 使用率：CPU（SYS.ECS）、内存 / 磁盘（AGT.ECS，需要安装 CES Agent）。"""
        res: dict[str, Any] = {"code": "ces.ecs_usage", "name": "CES 指标数据（第一台 ECS）", "service": "ces",
                               "used_by": "ECS CPU / 内存 / 磁盘使用率巡检"}
        if not get_settings().hcs_ces_enabled:
            return {**res, "result": "SKIP", "message": "该部署未启用 CES，未发起探测"}
        if not getattr(self.client, "ces_enabled", False):
            return {**res, "result": "SKIP", "message": "该租户未明确选择 CES，未发起探测"}
        if not self.ctx.get("ecs_id"):
            return {**res, "result": "SKIP", "message": "没有 ECS（或 ECS 列表调用失败）"}
        if self.client is None:
            return {**res, "result": "FAIL", "message": "未创建查询客户端"}
        started = time.perf_counter()
        try:
            m = ecs_metrics(self.client, [self.ctx["ecs_id"]]).get(self.ctx["ecs_id"], {})
        except HcsError as e:
            return {**res, "result": "WARN", "cost_ms": round((time.perf_counter() - started) * 1000),
                    "message": _safe_error(e), "hint": _hint(e)}
        got = {k: m.get(k) for k in ("cpu_util", "mem_util", "disk_util")}
        miss = [k for k, v in got.items() if v is None]
        return {**res, "result": "OK", "cost_ms": round((time.perf_counter() - started) * 1000), "values": got,
                "message": ("指标齐全" if not miss else f"没有数据：{'、'.join(miss)}"
                            "（内存 / 磁盘需要在云服务器内安装 CES Agent；CPU 没有数据时检查 CES 是否部署）")}


def _safe_error(error: Exception) -> str:
    if isinstance(error, HcsError):
        code = error.code if re.fullmatch(r"[A-Z0-9_.-]{1,64}", error.code) else "UPSTREAM"
        return f"上游请求失败（HTTP {error.status}，{code}）"
    return "云服务 Endpoint 未配置完整"


def _hint(e: Exception) -> str:
    s = str(e)
    if isinstance(e, KeyError):
        return "没有配置该服务的 Endpoint：在 .env 的 INSPECTOR_HCS_ENDPOINTS 或租户接入的 Endpoint 覆盖里补上"
    if "[401]" in s:
        return "认证失败：检查 AK/SK 或用户名 / 密码 / 域名，Token 是否过期"
    if "[403]" in s:
        return "没有权限：给租户的 IAM 用户授予该服务的只读权限（CCE K8s API 需要集群只读 RBAC）"
    if "[404]" in s:
        return "接口不存在：局点未部署该服务，或接口版本 / 路径不同（核对 HCS 版本的 API 文档）"
    if "[0]" in s:
        return "网络不通或域名无法解析：检查 Endpoint、DNS（可在 docker-compose 的 extra_hosts 里加 hosts）、防火墙"
    if "[5" in s:
        return "服务端错误或网关超时：稍后重试，或联系局点运维确认服务状态"
    return ""


def _auth(client: HcsClient, tenant: HcsAccount) -> dict:
    started = time.perf_counter()
    try:
        if tenant.auth_type == "AKSK":
            n = len(client.get("iam", "/v3/projects").get("projects") or [])
            msg = f"AK/SK 签名校验通过，可见项目 {n} 个"
        else:
            if isinstance(client.auth, TokenAuth):
                client.auth.check()
            msg = "用户名 / 密码换取 Token 成功"
    except (HcsError, KeyError) as e:
        return {"code": "iam.auth", "name": "IAM 认证", "service": "iam", "result": "FAIL", "message": _safe_error(e),
                "hint": _hint(e), "cost_ms": round((time.perf_counter() - started) * 1000)}
    return {"code": "iam.auth", "name": "IAM 认证", "service": "iam", "result": "OK", "message": msg,
            "cost_ms": round((time.perf_counter() - started) * 1000)}


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, w: int, right: bool = False) -> str:
    fill = " " * max(0, w - _width(s))
    return fill + s if right else s + fill


def _print(results: list[dict]) -> None:
    w = max(_width(r["name"]) for r in results) + 2
    print(_pad("接口", w) + _pad("结果", 6) + _pad("耗时", 8, True) + "  " + _pad("条数/总数", 11) + "说明")
    for r in results:
        cnt = "" if "count" not in r else f"{r['count']}/{r.get('total', '-') if r.get('total') is not None else '-'}"
        note = r.get("message") or ""
        if r.get("missing_optional"):
            note = (note + "；" if note else "") + "缺少可选字段：" + "、".join(r["missing_optional"])
        if r.get("hint"):
            note += f"（{r['hint']}）"
        cost = f"{r['cost_ms']}ms" if "cost_ms" in r else ""
        print(_pad(r["name"], w) + _pad(r["result"], 6) + _pad(cost, 8, True) + "  " + _pad(cnt, 11) + note)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tools.probe", description="HCS 接口探测与字段核对（只读调用）")
    ap.add_argument("--tenant", help="租户编码（租户接入里的“编码”），--list 查看")
    ap.add_argument("--project", help="项目 ID（默认租户下第一个启用的项目）")
    ap.add_argument("--only", help="只探测这些服务或接口，逗号分隔，例如 ecs,dcs,k8s.events")
    ap.add_argument("--limit", type=int, default=5, help="每个接口取多少条（统计状态取值分布），默认 5，最多 100")
    ap.add_argument("--out", help="JSON 报告输出路径（例如 /app/data/probe-tenant-a.json）")
    ap.add_argument("--raw", action="store_true", help="样例不脱敏（只在本机排查时使用）")
    ap.add_argument("--all", action="store_true", help="采集开关已关闭的资源类型也探测")
    ap.add_argument("--list", action="store_true", help="列出已接入的租户")
    ap.add_argument("--mock-url", help=argparse.SUPPRESS)          # 开发 / 测试：指向 Mock HCS
    a = ap.parse_args(argv)
    with SessionLocal() as db:
        if a.list:
            for t in db.scalars(select(HcsAccount).order_by(HcsAccount.id)):
                projects = [p.project_id for p in db.scalars(select(HcsProject).where(HcsProject.account_id == t.id))]
                state = "启用" if t.enabled else "停用"
                print(f"{t.code:<16}{t.name:<24}{t.auth_type:<10}{state:<6}项目：{', '.join(projects)}")
            return 0
        if not a.tenant:
            ap.error("需要 --tenant（或 --list 查看租户编码）")
        tenant = db.scalar(select(HcsAccount).where(HcsAccount.code == a.tenant))
        if tenant is None:
            print(f"租户不存在：{a.tenant}（--list 查看）", file=sys.stderr)
            return 2
        q = select(HcsProject).where(HcsProject.account_id == tenant.id)
        q = q.where(HcsProject.project_id == a.project) if a.project else q.where(HcsProject.enabled.is_(True))
        project = db.scalar(q.order_by(HcsProject.id))
        if project is None:
            print("租户下没有（启用的）项目", file=sys.stderr)
            return 2
        sw = collect_switch.load(db)
        off = set() if a.all else {t for t in collect_switch.SWITCH_TYPES if not sw.enabled(tenant.id, t)}
        runner = Runner(None, project, max(1, min(a.limit, 100)), a.raw, off)
        runner.ctx["workspace"] = (project.workspaces or [""])[0]
        only = [x.strip() for x in (a.only or "").split(",") if x.strip()]
        chosen = {p.code for p in PROBES if not only or any(
            p.code == o or p.service == o or p.code.startswith(o + ".") for o in only)}
        chosen |= {DEPENDS[p.needs] for p in PROBES if p.code in chosen and p.needs in DEPENDS}
        ces_usage = not only or "ces" in only or "ces.ecs_usage" in only
        if not chosen and not ces_usage:
            print("没有匹配的服务或接口，未发起探测", file=sys.stderr)
            return 2
        if any(runner.skip_reason(p) is None for p in PROBES if p.code in chosen):
            try:
                client = client_for(tenant, project, mock_url=a.mock_url)
            except HcsError as e:
                print(f"无法创建客户端：{_safe_error(e)}", file=sys.stderr)
                return 2
            runner.client = client
            auth = _auth(client, tenant)
        else:
            auth = {"code": "iam.auth", "name": "IAM 认证", "service": "iam", "result": "SKIP",
                    "message": "所选接口均未启用或未配置，未发起认证"}
        results = [auth]
        if auth["result"] in ("OK", "SKIP"):
            for p in PROBES:
                if p.code in chosen:
                    results.append(runner.run(p))
            if ces_usage:
                results.append(runner.ces_ecs())
        _print(results)
        report = {"tool": "hcs-inspector probe", "version": __version__,
                  "at": datetime.now(UTC).isoformat(timespec="seconds"), "tenant": tenant.code,
                  "auth_type": tenant.auth_type, "region": project.region_id, "domain": project.external_domain,
                  "workspace": bool(runner.ctx.get("workspace")), "limit": runner.limit, "raw": a.raw,
                  "summary": dict(Counter(r["result"] for r in results)), "results": results}
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            print(f"\n报告已写入 {a.out}（{'未脱敏，不要外发' if a.raw else '样例已脱敏'}）")
        if auth["result"] == "FAIL":
            return 2
        return 1 if any(r["result"] == "FAIL" for r in results) else 0     # WARN（CES / K8s API 不可用）不算失败


if __name__ == "__main__":
    sys.exit(main())
