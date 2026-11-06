"""各类云资源的清单拉取与字段标准化。

每个 fetcher 输入 HcsClient，输出 LedgerRecord 列表；新增一类资源 = 写一个 fetcher + 在 catalog 中登记类型。
8.5.1 官方核对与未确认边界见 docs/HCS8.5.1台账接口核对.md，不按公有云接口自动试探版本。
ctx 用于同一次同步中各 fetcher 共享数据（例如 EVS 挂载关系要用到 ECS 名称），避免重复调用接口。
"""
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.hcs import HcsClient, HcsError
from app.hcs.cce_api import management_items
from app.hcs.cce_inventory import cluster_info
from app.hcs.gaussdb_api import gaussdb_instances_path
from app.hcs.pager import page_all, response_items, response_total
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
    # 对已确认不可靠的旧字段允许显式空值；状态缺失另由快照合并明确清空。
    clear_fields: tuple[str, ...] = ()
    clear_attrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is not None and not isinstance(self.status, str):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "资源状态格式不正确，未应用快照")
        if self.status is None or not self.status.strip():
            self.status = None


Fetcher = Callable[[HcsClient, dict], list[LedgerRecord]]


def parse_time(v: str | int | float | None) -> datetime | None:
    """兼容 ISO8601（带 / 不带时区）、"2026-01-10 08:00:00"、毫秒 / 秒时间戳。无时区按 UTC 处理。"""
    if v in (None, ""):
        return None
    if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
        try:
            n = float(v)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=UTC) if math.isfinite(n) else None
        except (OverflowError, OSError, ValueError):
            return None
    s = re.sub(r"\s+(UTC|CST|GMT)$", "", str(v).strip()).replace("Z", "+00:00")
    s = re.sub(r"\s*([+-]\d{2}):?(\d{2})$", r"\1:\2", s) if re.search(r"\d[\sT]\d", s) else s   # +0800 → +08:00
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def marker_all(fetch: Callable[[dict], dict], key: str, limit: int = 100, max_pages: int = 200,
               links_key: str | None = None) -> Iterator[dict]:
    """已定义 marker 的接口：末条 id 续页；EVS 显式部分页标记优先于页长推断。"""
    if limit <= 0 or max_pages <= 0:
        raise ValueError("分页大小和页数必须为正数")
    marker = None
    seen_markers = set()
    seen_ids: set[str] = set()
    for _ in range(max_pages):
        data = fetch({"limit": limit, **({"marker": marker} if marker else {})})
        items = response_items(data, key)
        ids: list[str] = []
        for item in items:
            rid = item.get("id")
            if not isinstance(rid, str) or not rid.strip():
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "上游列表包含无效资源 ID")
            ids.append(rid)
        if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表资源 ID 重复，未使用不完整清单")
        seen_ids.update(ids)
        partial = None
        if links_key and data.get(links_key) is not None:
            links = data[links_key]
            # 8.5.1 EVS 字段表写字符串数组、示例为 href/rel 对象数组；两种均不跟随外部 URL。
            if not isinstance(links, list) or any(not isinstance(link, str | dict) for link in links):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "上游列表续页标记格式不正确")
            partial = bool(links)
            if partial and not items:
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游声明部分清单但没有可推进记录，未使用不完整清单")
        yield from items
        if partial is False or partial is None and len(items) < limit:
            return
        marker = items[-1].get("id")
        if not isinstance(marker, str) or not marker or marker in seen_markers:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表分页标记无效或重复")
        seen_markers.add(marker)
    raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "上游列表超过分页上限，未取得完整清单")


def _gb(mb: float | int | str | None) -> float | None:
    value = _num(mb)
    return round(value / 1024, 1) if value is not None else None


def _num(v: str | int | float | None) -> float | None:
    try:
        value = float(v) if v not in (None, "") and not isinstance(v, bool) else None
        return value if value is not None and math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value: str | int | float | None) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number.is_integer() else None


def _fmt(n: float | None) -> str:
    return "-" if n is None else f"{n:g}"


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _project_items(c: HcsClient, items: list[dict], fields: tuple[str, ...]) -> list[dict]:
    """只检查对应 PDF 明确的项目字段；缺失字段不臆造归属，也不丢弃异项目行后继续下线。"""
    expected = getattr(c, "project_id", None)
    if isinstance(expected, str) and expected:
        for item in items:
            for key in fields:
                value = item.get(key)
                if value not in (None, "") and value != expected:
                    raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "上游清单项目归属不一致，未应用快照")
    return items


def remember_owners(ctx: dict, kind: str, bindings: list[tuple[str, str, list[str], dict[str, str]]]) -> None:
    """批量重建当前项目的绑定索引；IP 可跨 VPC 重复，有歧义时不猜归属。"""
    groups = ctx.setdefault("owner_bindings", {})
    groups[kind] = bindings
    ips: dict[str, dict[str, str]] = {}
    ports: dict[str, dict[str, str]] = {}
    for resource_kind, rows in groups.items():
        for rid, label, addresses, port_map in rows:
            identity = f"{resource_kind}:{rid}"
            for address in addresses:
                if isinstance(address, str) and address:
                    ips.setdefault(address, {})[identity] = label
            for port in port_map:
                if isinstance(port, str) and port:
                    ports.setdefault(port, {})[identity] = label
    ctx["ip_owner"] = {ip: next(iter(owners.values())) for ip, owners in ips.items() if len(owners) == 1}
    ctx["port_owner"] = {port: next(iter(owners.values())) for port, owners in ports.items() if len(owners) == 1}


def _connection_host(value: str | None) -> str | None:
    """连接列表可为纯 IP，不能把 IPv6 的末段当成端口裁掉。"""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "实例连接地址格式不正确")
    first = value.split(",", 1)[0].strip()
    if not first:
        return None
    try:
        ip_address(first)
        return first
    except ValueError:
        pass
    try:
        address = urlsplit(f"//{first}")
        if (not address.hostname or address.username is not None or address.password is not None
                or address.path or address.query or address.fragment):
            raise ValueError("invalid connection address")
        _ = address.port
        return address.hostname
    except ValueError as exc:
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "实例连接地址格式不正确") from exc


# ---------------- 计算 ----------------
def _evs_raw(c: HcsClient, ctx: dict) -> list[dict]:
    if "evs_raw" not in ctx:
        volumes = list(marker_all(lambda p: c.get("evs", "/v2/{project_id}/cloudvolumes/detail", params=p),
                                  "volumes", links_key="volumes_links"))
        _project_items(c, volumes, ("os-vol-tenant-attr:tenant_id",))
        for volume in volumes:
            attachments = volume.get("attachments")
            if attachments is not None:
                for attachment in response_items({"attachments": attachments}, "attachments"):
                    server = attachment.get("server_id")
                    if not isinstance(server, str) or not server.strip():
                        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "EVS 挂载记录的云服务器 ID 无效")
        ctx["evs_raw"] = volumes
    return ctx["evs_raw"]


def fetch_ecs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    def fetch(params: dict) -> dict:
        data = c.get("ecs", "/v1/{project_id}/cloudservers/detail", params=params)
        response_total(data, "count", required=True, allow_string=False)
        _project_items(c, response_items(data, "servers"), ("tenant_id",))
        return data

    servers = list(page_all(fetch, "servers",
                            mode="page", start=1, total_key="count"))
    disk: dict[str, float] = {}
    unknown_disk = set()
    for v in _evs_raw(c, ctx):
        # 多挂载/重连记录可对应同一云主机，一块卷对同一主机只计一次。
        for server_id in {a["server_id"] for a in v.get("attachments") or []}:
            size = _num(v.get("size"))
            if size is None:
                unknown_disk.add(server_id)
            else:
                disk[server_id] = disk.get(server_id, 0) + size
                if not math.isfinite(disk[server_id]):
                    unknown_disk.add(server_id)
    out = []
    bindings: list[tuple[str, str, list[str], dict[str, str]]] = []
    for s in servers:
        f, md = s.get("flavor") or {}, s.get("metadata") or {}
        raw_cpu, ram = _num(f.get("vcpus")), _gb(f.get("ram"))
        vcpus = int(raw_cpu) if raw_cpu is not None and raw_cpu >= 0 and raw_cpu.is_integer() else None
        addrs = [a for lst in (s.get("addresses") or {}).values() for a in lst]
        fixed = next((a["addr"] for a in addrs if a.get("OS-EXT-IPS:type") == "fixed"), None)
        floating = next((a["addr"] for a in addrs if a.get("OS-EXT-IPS:type") == "floating"), None)
        ctx.setdefault("ecs_names", {})[s["id"]] = s["name"]
        fixed_ips = sorted({a["addr"] for a in addrs if a.get("OS-EXT-IPS:type") == "fixed"
                            and isinstance(a.get("addr"), str) and a["addr"]})
        fixed_ports = {a["OS-EXT-IPS:port_id"]: a["addr"] for a in addrs
                       if a.get("OS-EXT-IPS:type") == "fixed" and isinstance(a.get("OS-EXT-IPS:port_id"), str)
                       and a["OS-EXT-IPS:port_id"] and isinstance(a.get("addr"), str) and a["addr"]}
        bindings.append((s["id"], f"云服务器 {s['name']}", fixed_ips, fixed_ports))
        out.append(LedgerRecord(s["id"], s["name"], s.get("status"), s.get("OS-EXT-AZ:availability_zone"), fixed,
                                f"{_fmt(vcpus)} vCPU | {_fmt(ram)} GB" if vcpus is not None or ram is not None else None,
                                vcpus, ram, None if s["id"] in unknown_disk else disk.get(s["id"]),
                                parse_time(s.get("created")),
                                {"flavor": f.get("id"), "os": md.get("image_name"), "eip": floating,
                                 "fixed_ips": fixed_ips, "fixed_ports": fixed_ports,
                                 "vpc_id": md.get("vpc_id"), "charging": md.get("charging_mode")},
                                clear_fields=tuple(k for k, value in {
                                    "vcpus": vcpus, "memory_gb": ram,
                                    "storage_gb": None if s["id"] in unknown_disk else disk.get(s["id"])
                                }.items() if value is None)))
    remember_owners(ctx, "ecs", bindings)
    return out


def fetch_cce(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for it in management_items(c.get("cce", "/api/v3/projects/{project_id}/clusters")):
        md, spec = it.get("metadata") or {}, it.get("spec") or {}
        st = it.get("status")
        if st is not None and not isinstance(st, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "CCE 集群状态格式不正确")
        st = {} if st is None else st
        try:
            nodes = management_items(c.get("cce", f"/api/v3/projects/{{project_id}}/clusters/{md['uid']}/nodes"))
            node_count = len(nodes)
        except HcsError:
            node_count = None
            ctx["cce_node_failures"] = ctx.get("cce_node_failures", 0) + 1
        endpoint = next((e.get("url") for e in st.get("endpoints") or [] if e.get("type") == "Internal"), None)
        try:
            ip = urlsplit(endpoint).hostname if endpoint else None
        except ValueError as exc:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "CCE 内网访问地址格式不正确") from exc
        info = cluster_info(it)
        out.append(LedgerRecord(md["uid"], md["name"], st.get("phase"), None, ip,
                                f"{spec.get('version')} | {node_count if node_count is not None else '未知'} 节点",
                                created_time=parse_time(md.get("creationTimestamp")),
                                attrs={"version": spec.get("version"), "node_count": node_count,
                                       "node_source": "available" if node_count is not None else "error",
                                       "reason": info["reason"], "message": info["message"],
                                       "endpoints": info["endpoints"], "job_id": info["job_id"],
                                       "flavor": spec.get("flavor"), "cluster_type": spec.get("type")},
                                clear_attrs=("node_count",) if node_count is None else ()))
    return out


# ---------------- 存储 ----------------
def fetch_evs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    names = ctx.get("ecs_names", {})
    out = []
    for v in _evs_raw(c, ctx):
        attachments = v.get("attachments") or []
        attached = list(dict.fromkeys(names.get(a["server_id"], a["server_id"]) for a in attachments))
        devices = list(dict.fromkeys(a["device"] for a in attachments if a.get("device")))
        size = _num(v.get("size"))
        bootable = _flag(v.get("bootable"))
        out.append(LedgerRecord(v["id"], v.get("name") or v["id"][:8], v.get("status"), v.get("availability_zone"),
                                None, f"{v.get('volume_type')} | {_fmt(size)} GB", storage_gb=size,
                                created_time=parse_time(v.get("created_at")),
                                attrs={"volume_type": v.get("volume_type"),
                                       "attached_to": ", ".join(attached) or None,
                                       "device": ", ".join(devices) or None,
                                       "attachments": [{k: a.get(k) for k in ("server_id", "device", "attachment_id")}
                                                       for a in attachments],
                                       "bootable": ("是" if bootable else "否") if bootable is not None else None,
                                       "encrypted": None, "encrypted_reported": _flag(v.get("encrypted"))},
                                clear_fields=("storage_gb",) if size is None else (),
                                clear_attrs=("encrypted", "attached_to", "device", "bootable")))
    return out


# ---------------- 网络 ----------------
def _subnets_raw(c: HcsClient, ctx: dict) -> list[dict]:
    if "subnets_raw" not in ctx:
        subnets = list(marker_all(lambda p: c.get("vpc", "/v1/{project_id}/subnets", params=p), "subnets"))
        ctx["subnets_raw"] = _project_items(c, subnets, ("project_id",))
    return ctx["subnets_raw"]


def fetch_vpc(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    vpcs = list(marker_all(lambda p: c.get("vpc", "/v1/{project_id}/vpcs", params=p), "vpcs"))
    _project_items(c, vpcs, ("project_id",))
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
    # 8.5.1 原生 V3 Query 表只列 cluster_id；不能套用兼容 V2 的 limit/marker。
    data = c.get("elb", "/v3/{project_id}/elb/loadbalancers")
    lbs = response_items(data, "loadbalancers")
    _project_items(c, lbs, ("tenant_id",))
    page_info = data.get("page_info")
    if page_info is not None:
        if not isinstance(page_info, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "ELB 分页信息格式不正确")
        count = page_info.get("current_count")
        if isinstance(count, str) and count.isdecimal():
            count = int(count)
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "ELB 当前记录数格式不正确")
            if count != len(lbs):
                raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "ELB 当前记录数与列表不一致，未使用不完整清单")
        # next_marker 仅定义为末条记录 ID，并不等于“还有下一页”。
    out = []
    bindings: list[tuple[str, str, list[str], dict[str, str]]] = []
    for lb in lbs:
        addresses = [lb["vip_address"]] if lb.get("vip_address") else []
        port_map = {lb["vip_port_id"]: lb.get("vip_address") or ""} if lb.get("vip_port_id") else {}
        bindings.append((lb["id"], f"负载均衡 {lb['name']}", addresses, port_map))
        eip = next((e.get("eip_address") for e in lb.get("eips") or []), None)
        azs = lb.get("availability_zone_list") or []
        listeners = lb.get("listeners")
        listener_count = None if listeners is None else len(response_items({"listeners": listeners}, "listeners"))
        out.append(LedgerRecord(lb["id"], lb["name"], lb.get("operating_status"),
                                ",".join(azs) or None, lb.get("vip_address"),
                                created_time=parse_time(lb.get("created_at")),
                                attrs={"eip": eip, "listeners": listener_count, "guaranteed": lb.get("guaranteed"),
                                       "members": None, "vpc_id": lb.get("vpc_id"), "vip_port_id": lb.get("vip_port_id"),
                                       "provisioning_status": lb.get("provisioning_status")},
                                clear_fields=("spec",), clear_attrs=("listeners", "members")))
    remember_owners(ctx, "elb", bindings)
    return out


def fetch_eip(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    owner = ctx.get("ip_owner", {})
    port_owner = ctx.get("port_owner", {})
    out = []
    eips = list(marker_all(lambda p: c.get("vpc", "/v1/{project_id}/publicips", params=p), "publicips"))
    for e in _project_items(c, eips, ("tenant_id", "project_id")):
        private = e.get("private_ip_address")
        share = e.get("bandwidth_share_type")
        share_label = {"WHOLE": "共享", "PER": "独享"}.get(share) if isinstance(share, str) else None
        bandwidth = _num(e.get("bandwidth_size"))
        port_id = e.get("port_id")
        bound_to = port_owner.get(port_id, private) if port_id else owner.get(private, private) if private else None
        out.append(LedgerRecord(e["id"], e.get("alias") or e["public_ip_address"], e.get("status"), None,
                                e.get("public_ip_address"), f"{bandwidth:g} Mbit/s" if bandwidth is not None else None,
                                created_time=parse_time(e.get("create_time")),
                                attrs={"bandwidth": bandwidth,
                                       "share_type": share_label,
                                       "bound_to": bound_to, "port_id": port_id,
                                       "bandwidth_name": e.get("bandwidth_name")},
                                clear_fields=("spec",) if bandwidth is None else (),
                                clear_attrs=("share_type", "bound_to", "port_id")
                                + (("bandwidth",) if bandwidth is None else ())))
    return out


# ---------------- 数据库与缓存 ----------------
RDS_DEPLOY = {"Ha": "主备", "Single": "单机", "Replica": "只读"}


def fetch_rds(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("rds", "/v3/{project_id}/instances", params=p), "instances",
                      total_key="total_count"):
        ds, vol = i.get("datastore") or {}, i.get("volume") or {}
        cpu, mem, size = _num(i.get("cpu")), _num(i.get("mem")), _num(vol.get("size"))
        vcpus = int(cpu) if cpu is not None and cpu > 0 and cpu.is_integer() else None
        az = (i.get("nodes") or [{}])[0].get("availability_zone")
        out.append(LedgerRecord(i["id"], i["name"], i.get("status"), az, (i.get("private_ips") or [None])[0],
                                f"{_fmt(vcpus)} vCPU | {_fmt(mem)} GB | {_fmt(size)} GB",
                                vcpus, mem, size, parse_time(i.get("created")),
                                {"engine": f"{ds.get('type')} {ds.get('version')}", "ha": RDS_DEPLOY.get(i.get("type") or "",
                                 i.get("type")), "flavor": i.get("flavor_ref")},
                                clear_fields=tuple(k for k, value in {
                                    "vcpus": vcpus, "memory_gb": mem, "storage_gb": size
                                }.items() if value is None)))
    return out


def gaussdb_attrs(i: dict) -> dict:
    """GaussDB 实例属性（巡检采集 collectors/gaussdb.py 也用这个函数，两边口径一致）。"""
    ver = (i.get("datastore") or {}).get("version")
    flavor = i.get("flavor_info") or {}
    cpu = _num(flavor.get("vcpu"))
    memory = _num(flavor.get("mem"))
    memory = memory if memory is not None and memory >= 0 else None
    vcpus = int(cpu) if cpu is not None and cpu >= 0 and cpu.is_integer() else None
    return {"version": f"GaussDB {ver}" if ver else None, "type": GAUSSDB_DEPLOY.get(i.get("type") or "", i.get("type")),
            "flavor": i.get("flavor_ref"), "vcpus": vcpus, "memory_size": memory,
            "volume_size": _num((i.get("volume") or {}).get("size")), "volume_gb": None,
            "memory_unit_verified": get_settings().hcs_gaussdb_api_variant == "v3.1", "volume_unit_verified": False}


def gaussdb_spec(attrs: dict) -> str | None:
    """容量只展示当前明确选定接口族已声明的单位。"""
    return " | ".join(part for part in (attrs.get("type"),
                         f"{attrs['vcpus']} vCPU" if attrs.get("vcpus") is not None else None,
                         f"{attrs['memory_size']:g} GB" if attrs.get("memory_unit_verified") is True
                         and attrs.get("memory_size") is not None else None) if part) or None


def fetch_gaussdb(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("gaussdb", gaussdb_instances_path(), params=p), "instances",
                      total_key="total_count"):
        attrs = gaussdb_attrs(i)
        memory = attrs["memory_size"] if attrs["memory_unit_verified"] else None
        zones = list(dict.fromkeys(node["availability_zone"] for node in i.get("nodes") or []
                                   if node.get("availability_zone")))
        out.append(LedgerRecord(i["id"], i["name"], i.get("status"), ",".join(zones) or i.get("availability_zone"),
                                (i.get("private_ips") or [None])[0], gaussdb_spec(attrs), vcpus=attrs["vcpus"],
                                memory_gb=memory, created_time=parse_time(i.get("created")), attrs=attrs,
                                clear_fields=("storage_gb",) + (("memory_gb",) if memory is None else ()),
                                clear_attrs=("volume_gb",) + tuple(k for k in ("memory_size", "volume_size")
                                                                  if attrs[k] is None)))
    return out


def fetch_dcs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    out = []
    for i in page_all(lambda p: c.get("dcs", "/v2/{project_id}/instances", params=p), "instances", limit=1000,
                      total_key="instance_num"):
        cap = _num(i.get("capacity"))
        if cap is None or cap == 0:
            minor = _num(i.get("capacity_minor"))
            cap = minor if minor is not None and minor > 0 else None
        out.append(LedgerRecord(i["instance_id"], i["name"], i.get("status"), ",".join(i.get("az_codes") or []) or None,
                                i.get("ip"), f"{i.get('engine')} {i.get('engine_version')} | {_fmt(cap)} GB",
                                memory_gb=cap, created_time=parse_time(i.get("created_at")),
                                attrs={"engine": i.get("engine"), "engine_version": i.get("engine_version"),
                                       "capacity": cap, "spec_code": i.get("spec_code"), "port": i.get("port")},
                                clear_fields=("memory_gb",) if cap is None else (),
                                clear_attrs=("capacity",) if cap is None else ()))
    return out


# ---------------- 大数据与中间件 ----------------
def fetch_kafka(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    def fetch(params: dict) -> dict:
        # DMS 2.3.0.7 for 8.5.1 未定义 instance_num 为全量总数；不能用它提前停止分页。
        return c.get("dms", "/v2/{project_id}/instances", params={"engine": "kafka", **params})

    out = []
    for i in page_all(fetch, "instances", limit=50, total_key=None):
        total, used = _num(i.get("storage_space")), _num(i.get("used_storage_space"))
        ratio = _num(used / total * 100) if total and used is not None else None
        usage = round(ratio, 1) if ratio is not None else None
        addr = _connection_host(i.get("connect_address"))
        partitions = _count(i.get("partition_num"))
        spec = " | ".join(part for part in (i.get("specification"), f"{total:g} GB" if total is not None else None)
                          if part) or None
        out.append(LedgerRecord(i["instance_id"], i["name"], i.get("status"), ",".join(i.get("available_zones") or [])
                                or None, addr or None,
                                spec, storage_gb=total,
                                created_time=parse_time(i.get("created_at")),
                                attrs={"engine_version": i.get("engine_version"), "partition_num": partitions,
                                       "storage_usage": usage, "specification": i.get("specification")},
                                clear_fields=("storage_gb",) if total is None else (),
                                clear_attrs=tuple(k for k, value in {
                                    "storage_usage": usage, "partition_num": partitions
                                }.items() if value is None)))
    return out


def fetch_mrs(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    def fetch(params: dict) -> dict:
        data = c.get("mrs", "/v1.1/{project_id}/cluster_infos", params=params)
        response_total(data, "clusterTotal", required=True)
        _project_items(c, response_items(data, "clusters"), ("tenantId",))
        return data

    rows = page_all(fetch, "clusters", mode="page",
                    start=1, total_key="clusterTotal", offset_param="currentPage", limit_param="pageSize", limit=50)
    out, seen = [], set()
    for cl in rows:
        if (any(not isinstance(cl.get(key), str) or not cl[key].strip() for key in ("clusterId", "clusterName"))
                or cl["clusterId"] in seen or cl.get("clusterState") is not None
                and not isinstance(cl["clusterState"], str)):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群标识、名称、状态不正确或标识重复")
        seen.add(cl["clusterId"])
        comps = ", ".join(x.get("componentName", "") for x in cl.get("componentList") or [])
        count = _count(cl.get("totalNodeNum"))
        nodes = cl.get("totalNodeNum") if count is not None else None
        spec = " | ".join(part for part in (cl.get("clusterVersion"), f"{count} 节点" if count is not None else None)
                          if part) or None
        out.append(LedgerRecord(cl["clusterId"], cl["clusterName"], cl.get("clusterState"), cl.get("azName"),
                                cl.get("masterNodeIp"), spec,
                                created_time=parse_time(cl.get("createAt")),
                                attrs={"version": cl.get("clusterVersion"), "nodes": nodes,
                                       "components": comps or None}, clear_attrs=("nodes",) if nodes is None else ()))
    return out


def fetch_dws(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    data = c.get("dws", "/v1.0/{project_id}/clusters")
    clusters = response_items(data, "clusters")
    count = data.get("count")
    if isinstance(count, str) and count.isdecimal():
        count = int(count)
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "DWS 集群总数格式不正确")
        if count != len(clusters):
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "DWS 集群列表与总数不一致，未使用不完整清单")
    out = []
    for cl in clusters:
        endpoints = cl.get("endpoints")
        endpoints = [] if endpoints is None else response_items({"endpoints": endpoints}, "endpoints")
        connection = next((endpoint.get("connect_info") for endpoint in endpoints if endpoint.get("connect_info")), None)
        if connection is not None and not isinstance(connection, str):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "DWS 内网连接信息格式不正确")
        try:
            host = urlsplit(f"//{connection}").hostname if connection else None
        except ValueError as exc:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "DWS 内网连接信息格式不正确") from exc
        node_count = _count(cl.get("number_of_node"))
        node_type = cl.get("node_type")
        spec = (f"{node_type} × {node_count}" if node_type else f"{node_count} 节点") if node_count is not None else node_type
        out.append(LedgerRecord(cl["id"], cl["name"], cl.get("status"), cl.get("availability_zone"),
                                host,
                                spec,
                                created_time=parse_time(cl.get("created")),
                                attrs={"version": cl.get("version"), "node_count": node_count,
                                       "node_type": node_type}, clear_fields=("ip",),
                                clear_attrs=("node_count",) if node_count is None else ()))
    return out


CSS_STATUS = {"100": "CREATING", "200": "AVAILABLE", "303": "UNAVAILABLE"}


def fetch_css(c: HcsClient, ctx: dict) -> list[LedgerRecord]:
    # 未认证的历史兼容实现：当前 start 按页号推进，不是已确认的 HCS 8.5.1 契约。
    # 其他版次的同名 start 可表示记录索引；未补现场契约前不据此改参或自动回退 offset。
    rows = page_all(lambda p: c.get("css", "/v1.0/{project_id}/clusters", params=p), "clusters", mode="page",
                    start=1, total_key="totalSize", offset_param="start", limit=10)
    out = []
    for cl in rows:
        ds, inst = cl.get("datastore") or {}, cl.get("instances")
        inst = response_items({"instances": inst}, "instances") if inst is not None else None
        spec = (inst[0].get("specCode") if inst else None) or "-"
        status = cl.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, str | int)):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "CSS 集群状态格式不正确")
        status = CSS_STATUS.get(str(status), str(status)) if status is not None else None
        out.append(LedgerRecord(cl["id"], cl["name"], status,
                                (inst[0].get("azCode") if inst else None), (cl.get("endpoint") or "").split(":")[0] or None,
                                f"{spec} × {len(inst)}" if inst is not None else None,
                                created_time=parse_time(cl.get("created")),
                                attrs={"version": f"{ds.get('type')} {ds.get('version')}",
                                       "node_count": len(inst) if inst is not None else None, "spec_code": spec},
                                clear_fields=("spec",) if inst is None else (),
                                clear_attrs=("node_count",) if inst is None else ()))
    return out


# 同步顺序有依赖：ECS 在 EVS / EIP 之前（挂载与绑定关系），VPC 在子网之前，ELB 在 EIP 之前
FETCHERS: dict[str, Fetcher] = {
    "ecs_server": fetch_ecs, "cce_cluster": fetch_cce, "evs_volume": fetch_evs, "vpc": fetch_vpc,
    "vpc_subnet": fetch_subnet, "elb": fetch_elb, "eip": fetch_eip, "rds_instance": fetch_rds,
    "gaussdb_instance": fetch_gaussdb, "dcs_instance": fetch_dcs, "kafka_instance": fetch_kafka,
    "mrs_cluster": fetch_mrs, "dws_cluster": fetch_dws, "css_cluster": fetch_css,
}
