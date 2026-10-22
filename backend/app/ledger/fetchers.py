"""各类云资源的清单拉取与字段标准化。

每个 fetcher 输入 HcsClient，输出 LedgerRecord 列表；新增一类资源 = 写一个 fetcher + 在 catalog 中登记类型。
接口路径、字段名以华为云 / HCS OpenAPI 为准，个别局点版本不同可在这里适配。
ctx 用于同一次同步中各 fetcher 共享数据（例如 EVS 挂载关系要用到 ECS 名称），避免重复调用接口。
"""
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.hcs import HcsClient
from app.hcs.pager import page_all
from app.services.catalog import GAUSSDB_DEPLOY


@dataclass
class LedgerRecord:
    resource_id: str
    name: str
    status: str | None = None
    az: str | None = None
    ip: str | None = None
    spec: str | None = None
    vcpus: int | None = None
    memory_gb: float | None = None
    storage_gb: float | None = None
    created_time: datetime | None = None
    attrs: dict = field(default_factory=dict)


Fetcher = Callable[[HcsClient, dict], list[LedgerRecord]]


def parse_time(v: str | int | float | None) -> datetime | None:
    """兼容 ISO8601（带 / 不带时区）、"2026-01-10 08:00:00"、毫秒 / 秒时间戳。无时区按 UTC 处理。"""
    if v in (None, ""):
        return None
    if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=UTC)
    s = re.sub(r"\s+(UTC|CST|GMT)$", "", str(v).strip()).replace("Z", "+00:00")
    s = re.sub(r"\s*([+-]\d{2}):?(\d{2})$", r"\1:\2", s) if re.search(r"\d[\sT]\d", s) else s   # +0800 → +08:00
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def marker_all(fetch: Callable[[dict], dict], key: str, limit: int = 100, max_pages: int = 200) -> Iterator[dict]:
    """marker 分页（VPC / 子网 / EIP / ELB）：以上一页最后一条的 id 作为下一页 marker。"""
    marker = None
    for _ in range(max_pages):
        data = fetch({"limit": limit, **({"marker": marker} if marker else {})})
        items = data.get(key) or []
        yield from items
        if len(items) < limit:
            return
        marker = items[-1]["id"]


def _gb(mb: float | int | str | None) -> float | None:
    return round(float(mb) / 1024, 1) if mb not in (None, "") else None


def _num(v: str | int | float | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fmt(n: float | None) -> str:
    return "-" if n is None else f"{n:g}"


# ---------------- 计算 ----------------
def _evs_raw(c: HcsClient, ctx: dict) -> list[dict]:
    if "evs_raw" not in ctx:
        ctx["evs_raw"] = list(page_all(lambda p: c.get("evs", "/v2/{project_id}/cloudvolumes/detail", params=p),
                                       "volumes", total_key="count"))
    return ctx["evs_raw"]


def fetch_ecs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    servers = list(page_all(lambda p: c.get("ecs", "/v1/{project_id}/cloudservers/detail", params=p), "servers",
                            mode="page", start=1, total_key="count"))
    disk: dict[str, float] = {}
    for v in _evs_raw(c, ctx):
        for a in v.get("attachments") or []:
            disk[a["server_id"]] = disk.get(a["server_id"], 0) + float(v.get("size") or 0)
    out = []
    for s in servers:
        f, md = s.get("flavor") or {}, s.get("metadata") or {}
        vcpus, ram = int(f.get("vcpus") or 0), _gb(f.get("ram"))
        addrs = [a for lst in (s.get("addresses") or {}).values() for a in lst]
        fixed = next((a["addr"] for a in addrs if a.get("OS-EXT-IPS:type") == "fixed"), None)
        floating = next((a["addr"] for a in addrs if a.get("OS-EXT-IPS:type") == "floating"), None)
        ctx.setdefault("ecs_names", {})[s["id"]] = s["name"]
        if fixed:
            ctx.setdefault("ip_owner", {})[fixed] = f"云服务器 {s['name']}"
        out.append(LedgerRecord(s["id"], s["name"], s.get("status"), s.get("OS-EXT-AZ:availability_zone"), fixed,
                                f"{vcpus} vCPU | {_fmt(ram)} GB", vcpus, ram, disk.get(s["id"]),
                                parse_time(s.get("created")),
                                {"flavor": f.get("id"), "os": md.get("image_name"), "eip": floating,
                                 "vpc_id": md.get("vpc_id"), "charging": md.get("charging_mode")}))
    return out


def fetch_cce(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for it in c.get("cce", "/api/v3/projects/{project_id}/clusters").get("items") or []:
        md, spec, st = it.get("metadata") or {}, it.get("spec") or {}, it.get("status") or {}
        nodes = c.get("cce", f"/api/v3/projects/{{project_id}}/clusters/{md['uid']}/nodes").get("items") or []
        endpoint = next((e.get("url") for e in st.get("endpoints") or [] if e.get("type") == "Internal"), None)
        ip = endpoint.split("//")[-1].split(":")[0] if endpoint else None
        out.append(LedgerRecord(md["uid"], md["name"], st.get("phase"), None, ip,
                                f"{spec.get('version')} | {len(nodes)} 节点", created_time=parse_time(
                                    md.get("creationTimestamp")),
                                attrs={"version": spec.get("version"), "node_count": len(nodes),
                                       "flavor": spec.get("flavor"), "cluster_type": spec.get("type")}))
    return out


# ---------------- 存储 ----------------
def fetch_evs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    names = ctx.get("ecs_names", {})
    out = []
    for v in _evs_raw(c, ctx):
        att = (v.get("attachments") or [{}])[0] if v.get("attachments") else {}
        size = float(v.get("size") or 0)
        out.append(LedgerRecord(v["id"], v.get("name") or v["id"][:8], v.get("status"), v.get("availability_zone"),
                                None, f"{v.get('volume_type')} | {_fmt(size)} GB", storage_gb=size,
                                created_time=parse_time(v.get("created_at")),
                                attrs={"volume_type": v.get("volume_type"),
                                       "attached_to": names.get(att.get("server_id"), att.get("server_id")),
                                       "device": att.get("device"), "bootable": "是" if v.get("bootable") == "true" else "否",
                                       "encrypted": bool(v.get("encrypted"))}))
    return out


# ---------------- 网络 ----------------
def _subnets_raw(c: HcsClient, ctx: dict) -> list[dict]:
    if "subnets_raw" not in ctx:
        ctx["subnets_raw"] = list(marker_all(lambda p: c.get("vpc", "/v1/{project_id}/subnets", params=p), "subnets"))
    return ctx["subnets_raw"]


def fetch_vpc(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    vpcs = list(marker_all(lambda p: c.get("vpc", "/v1/{project_id}/vpcs", params=p), "vpcs"))
    subnets = _subnets_raw(c, ctx)
    ctx["vpc_names"] = {v["id"]: v["name"] for v in vpcs}
    return [LedgerRecord(v["id"], v["name"], v.get("status"), spec=v.get("cidr"),
                         attrs={"cidr": v.get("cidr"), "subnet_count": sum(1 for s in subnets if s.get("vpc_id") == v["id"]),
                                "description": v.get("description")}) for v in vpcs]


def fetch_subnet(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    names = ctx.get("vpc_names", {})
    return [LedgerRecord(s["id"], s["name"], s.get("status"), s.get("availability_zone"), None, s.get("cidr"),
                         attrs={"cidr": s.get("cidr"), "gateway": s.get("gateway_ip"),
                                "vpc_name": names.get(s.get("vpc_id"), s.get("vpc_id")), "dhcp": s.get("dhcp_enable")})
            for s in _subnets_raw(c, ctx)]


def fetch_elb(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    lbs = list(marker_all(lambda p: c.get("elb", "/v3/{project_id}/elb/loadbalancers", params=p), "loadbalancers"))
    out = []
    for lb in lbs:
        if lb.get("vip_address"):
            ctx.setdefault("ip_owner", {})[lb["vip_address"]] = f"负载均衡 {lb['name']}"
        eip = next((e.get("eip_address") for e in lb.get("eips") or []), None)
        azs = lb.get("availability_zone_list") or []
        out.append(LedgerRecord(lb["id"], lb["name"], lb.get("operating_status") or lb.get("provisioning_status"),
                                ",".join(azs) or None, lb.get("vip_address"),
                                "独享型" if lb.get("guaranteed") else "共享型", created_time=parse_time(lb.get("created_at")),
                                attrs={"eip": eip, "listeners": len(lb.get("listeners") or []),
                                       "members": lb.get("member_count", 0), "vpc_id": lb.get("vpc_id")}))
    return out


def fetch_eip(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    owner = ctx.get("ip_owner", {})
    out = []
    for e in marker_all(lambda p: c.get("vpc", "/v1/{project_id}/publicips", params=p), "publicips"):
        private = e.get("private_ip_address")
        out.append(LedgerRecord(e["id"], e.get("alias") or e["public_ip_address"], e.get("status"), None,
                                e.get("public_ip_address"), f"{e.get('bandwidth_size')} Mbit/s",
                                created_time=parse_time(e.get("create_time")),
                                attrs={"bandwidth": e.get("bandwidth_size"),
                                       "share_type": "共享" if e.get("bandwidth_share_type") == "WHOLE" else "独享",
                                       "bound_to": owner.get(private, private) if private else None,
                                       "bandwidth_name": e.get("bandwidth_name")}))
    return out


# ---------------- 数据库与缓存 ----------------
RDS_DEPLOY = {"Ha": "主备", "Single": "单机", "Replica": "只读"}


def fetch_rds(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("rds", "/v3/{project_id}/instances", params=p), "instances",
                      total_key="total_count"):
        ds, vol = i.get("datastore") or {}, i.get("volume") or {}
        cpu, mem, size = _num(i.get("cpu")), _num(i.get("mem")), _num(vol.get("size"))
        az = (i.get("nodes") or [{}])[0].get("availability_zone")
        out.append(LedgerRecord(i["id"], i["name"], i.get("status"), az, (i.get("private_ips") or [None])[0],
                                f"{_fmt(cpu)} vCPU | {_fmt(mem)} GB | {_fmt(size)} GB",
                                int(cpu) if cpu else None, mem, size, parse_time(i.get("created")),
                                {"engine": f"{ds.get('type')} {ds.get('version')}", "ha": RDS_DEPLOY.get(i.get("type") or "",
                                 i.get("type")), "flavor": i.get("flavor_ref")}))
    return out


def gaussdb_attrs(i: dict) -> dict:
    """GaussDB 实例属性（巡检采集 collectors/gaussdb.py 也用这个函数，两边口径一致）。"""
    ver = (i.get("datastore") or {}).get("version")
    return {"version": f"GaussDB {ver}" if ver else None, "type": GAUSSDB_DEPLOY.get(i.get("type") or "", i.get("type")),
            "flavor": i.get("flavor_ref"), "volume_gb": _num((i.get("volume") or {}).get("size"))}


def fetch_gaussdb(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("gaussdb", "/v3/{project_id}/instances", params=p), "instances",
                      total_key="total_count"):
        attrs = gaussdb_attrs(i)
        out.append(LedgerRecord(i["id"], i["name"], i.get("status"), i.get("availability_zone"),
                                (i.get("private_ips") or [None])[0], f"{attrs['type']} | {_fmt(attrs['volume_gb'])} GB",
                                storage_gb=attrs["volume_gb"], created_time=parse_time(i.get("created")), attrs=attrs))
    return out


def fetch_dcs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("dcs", "/v2/{project_id}/instances", params=p), "instances", limit=1000,
                      total_key="instance_num"):
        cap = _num(i.get("capacity"))
        out.append(LedgerRecord(i["instance_id"], i["name"], i.get("status"), ",".join(i.get("az_codes") or []) or None,
                                i.get("ip"), f"{i.get('engine')} {i.get('engine_version')} | {_fmt(cap)} GB",
                                memory_gb=cap, created_time=parse_time(i.get("created_at")),
                                attrs={"engine": i.get("engine"), "engine_version": i.get("engine_version"),
                                       "capacity": cap, "spec_code": i.get("spec_code"), "port": i.get("port")}))
    return out


# ---------------- 大数据与中间件 ----------------
def fetch_kafka(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    data = c.get("dms", "/v2/{project_id}/instances", params={"engine": "kafka"})
    out = []
    for i in data.get("instances") or []:
        total, used = _num(i.get("storage_space")), _num(i.get("used_storage_space"))
        usage = round(used * 100 / total, 1) if total and used is not None else None
        addr = (i.get("connect_address") or "").split(",")[0].rsplit(":", 1)[0]     # "ip:9092,ip:9092" → 第一个 IP
        out.append(LedgerRecord(i["instance_id"], i["name"], i.get("status"), ",".join(i.get("available_zones") or [])
                                or None, addr or None,
                                f"{i.get('specification')} | {_fmt(total)} GB", storage_gb=total,
                                created_time=parse_time(i.get("created_at")),
                                attrs={"engine_version": i.get("engine_version"), "partition_num": i.get("partition_num"),
                                       "storage_usage": usage, "specification": i.get("specification")}))
    return out


def fetch_mrs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    rows = page_all(lambda p: c.get("mrs", "/v1.1/{project_id}/cluster_infos", params=p), "clusters", mode="page",
                    start=1, total_key="clusterTotal", offset_param="currentPage", limit_param="pageSize", limit=50)
    out = []
    for cl in rows:
        comps = ", ".join(x.get("componentName", "") for x in cl.get("componentList") or [])
        out.append(LedgerRecord(cl["clusterId"], cl["clusterName"], cl.get("clusterState"), cl.get("azName"),
                                cl.get("masterNodeIp"), f"{cl.get('clusterVersion')} | {cl.get('totalNodeNum')} 节点",
                                created_time=parse_time(cl.get("createAt")),
                                attrs={"version": cl.get("clusterVersion"), "nodes": cl.get("totalNodeNum"),
                                       "components": comps or None}))
    return out


def fetch_dws(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for cl in c.get("dws", "/v1.0/{project_id}/clusters").get("clusters") or []:
        out.append(LedgerRecord(cl["id"], cl["name"], cl.get("status"), cl.get("availability_zone"),
                                (cl.get("private_ip") or [None])[0],
                                f"{cl.get('node_type')} × {cl.get('number_of_node')}",
                                created_time=parse_time(cl.get("created")),
                                attrs={"version": cl.get("version"), "node_count": cl.get("number_of_node"),
                                       "node_type": cl.get("node_type")}))
    return out


CSS_STATUS = {"100": "CREATING", "200": "AVAILABLE", "303": "UNAVAILABLE"}


def fetch_css(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    rows = page_all(lambda p: c.get("css", "/v1.0/{project_id}/clusters", params=p), "clusters", mode="page",
                    start=1, total_key="totalSize", offset_param="start", limit=10)
    out = []
    for cl in rows:
        ds, inst = cl.get("datastore") or {}, cl.get("instances") or []
        spec = (inst[0].get("specCode") if inst else None) or "-"
        out.append(LedgerRecord(cl["id"], cl["name"], CSS_STATUS.get(str(cl.get("status")), str(cl.get("status"))),
                                (inst[0].get("azCode") if inst else None), (cl.get("endpoint") or "").split(":")[0] or None,
                                f"{spec} × {len(inst)}", created_time=parse_time(cl.get("created")),
                                attrs={"version": f"{ds.get('type')} {ds.get('version')}", "node_count": len(inst),
                                       "spec_code": spec}))
    return out


# 同步顺序有依赖：ECS 在 EVS / EIP 之前（挂载与绑定关系），VPC 在子网之前，ELB 在 EIP 之前
FETCHERS: dict[str, Fetcher] = {
    "ecs_server": fetch_ecs, "cce_cluster": fetch_cce, "evs_volume": fetch_evs, "vpc": fetch_vpc,
    "vpc_subnet": fetch_subnet, "elb": fetch_elb, "eip": fetch_eip, "rds_instance": fetch_rds,
    "gaussdb_instance": fetch_gaussdb, "dcs_instance": fetch_dcs, "kafka_instance": fetch_kafka,
    "mrs_cluster": fetch_mrs, "dws_cluster": fetch_dws, "css_cluster": fetch_css,
}
