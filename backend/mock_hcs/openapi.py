"""把演示资源清单（inventory.py = 租户A，inventory_b.py = 租户B）转换成各服务 OpenAPI 的返回格式（字段参照 HCS 8.5.1）。

所有函数的第一个参数 ds 是数据集模块；运行时读取模块属性，测试中 monkeypatch 清单（例如 inventory.DCS）即可生效。
"""
import hashlib
import time
from types import ModuleType
from typing import Any

POWER_STATE = {"ACTIVE": 1, "SHUTOFF": 4, "ERROR": 0, "BUILD": 0, "REBOOT": 1, "HARD_REBOOT": 1, "MIGRATING": 1,
               "RESIZE": 1, "VERIFY_RESIZE": 1, "REBUILD": 0}
TASK_STATE = {"REBOOT": "rebooting", "HARD_REBOOT": "rebooting_hard", "MIGRATING": "migrating", "RESIZE": "resize_prep",
              "BUILD": "spawning", "REBUILD": "rebuilding"}
VM_STATE = {"ACTIVE": "active", "SHUTOFF": "stopped", "ERROR": "error", "BUILD": "building", "MIGRATING": "active",
            "REBOOT": "active", "HARD_REBOOT": "active", "RESIZE": "active", "VERIFY_RESIZE": "resized"}


def stable(name: str, salt: str, lo: float, hi: float) -> float:
    """按名称生成稳定的“随机”数（同一资源每次请求返回相近的值，便于演示阈值告警）。"""
    h = int(hashlib.md5(f"{salt}:{name}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(lo + (hi - lo) * h, 1)


def jitter(v: float, amp: float = 0.6) -> float:
    """在基准值附近小幅波动（按分钟变化），模拟真实监控数据。"""
    return round(max(0.0, min(100.0, v + amp * ((int(time.time() // 60) % 7) - 3) / 3)), 1)


def ecs_servers(ds: ModuleType) -> list[dict]:
    out = []
    for name, flavor, image, subnet, ip, az, status, created, *_ in ds.ECS:
        vcpus, ram = ds.FLAVORS[flavor]
        vpc = ds.rid("vpc", ds.SUBNET_VPC[subnet])
        addrs = [{"addr": ip, "version": 4, "OS-EXT-IPS:type": "fixed"}]
        if ip in ds.EIP_BY_IP:
            addrs.append({"addr": ds.EIP_BY_IP[ip][1], "version": 4, "OS-EXT-IPS:type": "floating"})
        out.append({"id": ds.rid("ecs", name), "name": name, "status": status, "OS-EXT-AZ:availability_zone": az,
                    "OS-EXT-STS:power_state": POWER_STATE.get(status, 0), "OS-EXT-STS:task_state": TASK_STATE.get(status, ""),
                    "OS-EXT-STS:vm_state": VM_STATE.get(status, "active"),
                    "flavor": {"id": flavor, "name": flavor, "vcpus": str(vcpus), "ram": str(ram * 1024)},
                    "addresses": {vpc: addrs}, "created": ds.iso(created), "updated": ds.iso(ds.NOW),
                    "metadata": {"image_name": image, "vpc_id": vpc, "charging_mode": "0"}})
    return out


def evs_volumes(ds: ModuleType) -> list[dict]:
    out = []
    for e in ds.ECS:
        name, az, created, sys_gb, data = e[0], e[5], e[7], e[8], e[9]
        disks = [(f"{name}-sys", sys_gb, "SSD", "/dev/vda", "true")]
        disks += [(f"{name}-data-0{k}", gb, t, f"/dev/vd{chr(98 + k - 1)}", "false") for k, (gb, t) in enumerate(data, 1)]
        for vname, gb, vtype, dev, boot in disks:
            out.append({"id": ds.rid("evs", vname), "name": vname, "status": "in-use", "size": gb, "volume_type": vtype,
                        "availability_zone": az, "bootable": boot, "encrypted": vtype == "SSD" and "etl" in name,
                        "attachments": [{"server_id": ds.rid("ecs", name), "device": dev}],
                        "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000000")})
    for vname, gb, vtype, az, created in ds.SPARE_VOLUMES:
        out.append({"id": ds.rid("evs", vname), "name": vname, "status": "available", "size": gb, "volume_type": vtype,
                    "availability_zone": az, "bootable": "false", "encrypted": False, "attachments": [],
                    "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000000")})
    return out


def vpcs(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("vpc", n), "name": n, "cidr": cidr, "status": "OK", "description": d} for n, cidr, d in ds.VPCS]


def subnets(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("subnet", n), "name": n, "cidr": cidr, "gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
             "vpc_id": ds.rid("vpc", vpc), "status": "ACTIVE", "availability_zone": az, "dhcp_enable": True}
            for n, vpc, cidr, az in ds.SUBNETS]


def publicips(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("eip", n), "alias": n, "public_ip_address": ip, "status": st, "private_ip_address": priv,
             "bandwidth_size": bw, "bandwidth_share_type": "WHOLE" if share else "PER",
             "bandwidth_name": share or f"bandwidth-{n}", "create_time": c.strftime("%Y-%m-%d %H:%M:%S")}
            for n, ip, st, priv, bw, share, c in ds.EIPS]


def loadbalancers(ds: ModuleType) -> list[dict]:
    eip_of = {e[3]: e[1] for e in ds.EIPS if e[3]}
    return [{"id": ds.rid("elb", n), "name": n, "vip_address": vip, "vpc_id": ds.rid("vpc", ds.SUBNET_VPC[subnet]),
             "operating_status": "ONLINE", "provisioning_status": "ACTIVE", "guaranteed": ded,
             "availability_zone_list": [ds.AZ1, ds.AZ2] if ded else [ds.AZ1],
             "eips": [{"eip_address": eip_of[vip]}] if vip in eip_of else [],
             "listeners": [{"id": ds.rid("listener", f"{n}-{k}")} for k in range(listeners)], "member_count": members,
             "created_at": ds.iso(c)} for n, vip, subnet, ded, listeners, members, c in ds.ELBS]


def rds_instances(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("rds", n).replace("-", "") + "in01", "name": n, "status": "ACTIVE", "type": ha,
             "datastore": {"type": eng, "version": ver}, "volume": {"type": "CLOUDSSD", "size": gb},
             "cpu": str(cpu), "mem": str(mem), "private_ips": [ip], "flavor_ref": flavor,
             "nodes": [{"availability_zone": ds.AZ1, "role": "master"}] + ([{"availability_zone": ds.AZ2, "role": "slave"}]
                                                                          if ha == "Ha" else []),
             "created": c.strftime("%Y-%m-%dT%H:%M:%S+0000")} for n, eng, ver, ha, cpu, mem, gb, ip, flavor, c in ds.RDS]


def gaussdb_id(ds: ModuleType, name: str) -> str:
    return ds.rid("gaussdb", name).replace("-", "") + "in14"


def gaussdb_instances(ds: ModuleType) -> list[dict]:
    out = []
    for n, st, typ, gb, ip, _ces, c in ds.GAUSSDB:
        roles = ("master", "slave", "slave") if typ == "Ha" else ("master",)
        out.append({"id": gaussdb_id(ds, n), "name": n, "status": st, "type": typ,
                    "datastore": {"type": "GaussDB", "version": "3.208"}, "volume": {"type": "ULTRAHIGH", "size": gb},
                    "flavor_ref": "gaussdb.opengauss.ee.dn.m6.2xlarge.8", "private_ips": [ip],
                    "availability_zone": f"{ds.AZ1},{ds.AZ2}" if typ == "Ha" else ds.AZ2, "created": ds.iso(c),
                    "nodes": [{"name": f"{n}_dn{k}", "role": r, "status": "ACTIVE"} for k, r in enumerate(roles, 1)]})
    return out


def dcs_instances(ds: ModuleType) -> list[dict]:
    return [{"instance_id": ds.rid("dcs", d["name"]), "name": d["name"], "status": d["status"], "engine": "Redis",
             "engine_version": d["ver"], "capacity": d["cap"], "used_memory": d["used"], "max_memory": d["cap"] * 1024,
             "max_clients": 10000, "spec_code": f"redis.ha.xu1.large.r2.{d['cap']}", "ip": d["ip"], "port": 6379,
             "az_codes": [ds.AZ1, ds.AZ2], "created_at": ds.iso(d["created"])} for d in ds.DCS]


def kafka_instances(ds: ModuleType) -> list[dict]:
    return [{"instance_id": ds.rid("kafka", n), "name": n, "engine": "kafka", "engine_version": ver, "status": "RUNNING",
             "specification": spec, "storage_space": gb, "used_storage_space": used, "partition_num": str(parts),
             "available_zones": [ds.AZ1], "connect_address": f"{ip}:9092,{ip[:-1]}{int(ip[-1]) + 1}:9092",
             "created_at": str(int(c.timestamp() * 1000))} for n, spec, ver, gb, used, parts, ip, c in ds.KAFKA]


def mrs_clusters(ds: ModuleType) -> list[dict]:
    return [{"clusterId": ds.rid("mrs", n), "clusterName": n, "clusterState": "running", "clusterVersion": ver,
             "totalNodeNum": nodes, "azName": ds.AZ1, "masterNodeIp": ip, "createAt": str(int(c.timestamp())),
             "componentList": [{"componentName": x} for x in ds.MRS_COMPONENTS.get(n, ds.MRS_COMPONENTS["default"])]}
            for n, ver, nodes, ip, c in ds.MRS]


def cce_clusters(ds: ModuleType) -> list[dict]:
    return [{"kind": "Cluster", "metadata": {"uid": ds.rid("cce", n), "name": n,
                                             "creationTimestamp": c.strftime("%Y-%m-%d %H:%M:%S.000000 +0000 UTC")},
             "spec": {"version": ver, "flavor": flavor, "type": "VirtualMachine"},
             "status": {"phase": "Available", "endpoints": [{"url": f"https://{ip}:5443", "type": "Internal"}]}}
            for n, ver, flavor, ip, _prefix, c in ds.CCE]


def cce_nodes(ds: ModuleType, uid: str) -> list[dict]:
    prefix = next((p for n, _v, _f, _ip, p, _c in ds.CCE if ds.rid("cce", n) == uid), None)
    return [{"metadata": {"uid": ds.rid("ecs", e[0]), "name": e[0]}, "status": {"phase": "Active", "privateIP": e[4]}}
            for e in ds.ECS if prefix and e[0].startswith(prefix)]


def dws_clusters(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("dws", n), "name": n, "status": "AVAILABLE", "version": ver, "node_type": nt,
             "number_of_node": num, "availability_zone": ds.AZ1, "private_ip": [ip], "created": ds.iso(c)}
            for n, ver, nt, num, ip, c in ds.DWS]


def css_clusters(ds: ModuleType) -> list[dict]:
    return [{"id": ds.rid("css", n), "name": n, "status": "200", "datastore": {"type": "elasticsearch", "version": ver},
             "instances": [{"name": f"{n}-ess-esn-{k}-1", "specCode": spec, "azCode": ds.AZ1, "status": "200"}
                           for k in range(1, num + 1)], "endpoint": f"{ip}:9200", "created": ds.iso(c)}
            for n, ver, spec, num, ip, c in ds.CSS]


# ---------------- ECS 监控数据（CES / Prometheus 共用同一份基准值） ----------------
def ecs_usage(ds: ModuleType) -> dict[str, dict[str, Any]]:
    """{ECS 名称: {cpu, mem, disks: {挂载点: 使用率}, agent: 是否装了 CES Agent / node_exporter, ip, boot}}。

    关机 / 故障的实例没有数据。"""
    out: dict[str, dict[str, Any]] = {}
    for e in ds.ECS:
        name, ip, status, data = e[0], e[4], e[6], e[9]
        if status not in ("ACTIVE", "MIGRATING", "RESIZE", "VERIFY_RESIZE"):
            continue
        ov = ds.ECS_USAGE.get(name, {})
        disks = {"/": stable(name, "root", 22, 61)}
        if data:
            disks["/data"] = stable(name, "data", 18, 72)
        disks.update(ov.get("disks", {}))
        out[name] = {"id": ds.rid("ecs", name), "ip": ip, "cpu": jitter(ov.get("cpu", stable(name, "cpu", 4, 48))),
                     "mem": jitter(ov.get("mem", stable(name, "mem", 28, 74)), 0.3),
                     "disks": {k: jitter(v, 0.1) for k, v in disks.items()},
                     "agent": any(name.startswith(p) for p in ds.AGENT_PREFIXES),
                     "boot": int((ds.NOW - ds.BOOT_AGO.get(name, ds.DEFAULT_UPTIME)).timestamp())}
    return out


def ces_values(ds: ModuleType) -> dict[str, dict[str, float]]:
    """CES 单维度指标：{资源ID: {指标名: 值}}（DCS、GaussDB、ECS）。"""
    vals: dict[str, dict[str, float]] = {ds.rid("dcs", d["name"]): {"cpu_usage": d["ces"][0], "connected_clients": d["ces"][1]}
                                         for d in ds.DCS}
    for n, *_x, (cpu, mem, disk), _c in ds.GAUSSDB:
        vals[gaussdb_id(ds, n)] = {"rds001_cpu_util": cpu, "rds002_mem_util": mem, "rds039_disk_util": disk}
    for u in ecs_usage(ds).values():
        vals[u["id"]] = {"cpu_util": u["cpu"], **({"mem_usedPercent": u["mem"]} if u["agent"] else {})}
    return vals


def ces_disk_metrics(ds: ModuleType) -> list[dict]:
    """AGT.ECS disk_usedPercent 的指标列表（instance_id × mount_point），只有装了 Agent 的实例才有。"""
    return [{"namespace": "AGT.ECS", "metric_name": "disk_usedPercent", "unit": "%",
             "dimensions": [{"name": "instance_id", "value": u["id"]}, {"name": "mount_point", "value": mp}],
             "_value": v} for u in ecs_usage(ds).values() if u["agent"] for mp, v in u["disks"].items()]


def prometheus_series(ds: ModuleType, key: str) -> list[dict]:
    """node_exporter 风格的查询结果（instance = "IP:9100"）。"""
    now = time.time()
    out = []
    for name, u in ecs_usage(ds).items():
        if not u["agent"]:
            continue
        inst: dict[str, str] = {"instance": f"{u['ip']}:9100", "job": "node", "nodename": name}
        if key == "up":
            out.append({"metric": {"__name__": "up"} | inst, "value": [now, "1"]})
        elif key == "disk_util":
            out += [{"metric": inst | {"mountpoint": mp, "fstype": "ext4"}, "value": [now, str(v)]}
                    for mp, v in u["disks"].items()]
        elif key == "boot_time":
            out.append({"metric": {"__name__": "node_boot_time_seconds"} | inst, "value": [now, str(u["boot"])]})
        elif key in ("cpu_util", "mem_util"):
            out.append({"metric": inst, "value": [now, str(u["cpu" if key == "cpu_util" else "mem"])]})
    return out
