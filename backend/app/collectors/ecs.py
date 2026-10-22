"""ECS 云服务器巡检（任务 ecs_status，默认每 120 秒）。

1. 实例状态：GET /v1/{project_id}/cloudservers/detail（HCS 8.5.1 ECS 接口）——status、OS-EXT-STS:task_state / power_state。
   状态变化由流水线写入台账变更（类型“状态变更”），资源详情里能看到“几点几分从运行中变为关机”
2. 资源使用率（可选，来源见 app.metrics）：CES（cpu_util；内存 / 磁盘需安装 CES Agent）或 Prometheus（node_exporter）
3. 重启：轮询时看到 REBOOT / HARD_REBOOT 记一次；接了 Prometheus 时开机时间（node_boot_time_seconds）变化也记一次，
   能发现在操作系统内执行 reboot、宕机后被 HA 拉起这类不经过 ECS 接口的重启
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.hcs.pager import page_all
from app.metrics import ces as ces_metrics
from app.metrics import effective, guarded_ces, mark
from app.metrics.prometheus import PromError, prom_for
from app.metrics.prometheus import ecs_metrics as prom_metrics
from app.models import Resource, ResourceChange
from app.models.base import utcnow
from app.services.catalog import ECS_STATUS

from .base import Observation, RunContext

log = logging.getLogger(__name__)
RESOURCE_TYPE = "ecs_server"
REBOOTING = ("REBOOT", "HARD_REBOOT")
TRANSITIONAL = ("BUILD", "REBOOT", "HARD_REBOOT", "RESIZE", "VERIFY_RESIZE", "REVERT_RESIZE", "MIGRATING", "REBUILD")
POWER_STATE = {0: "NOSTATE", 1: "RUNNING", 3: "PAUSED", 4: "SHUTDOWN", 6: "CRASHED", 7: "SUSPENDED"}


def _fixed_ips(s: dict) -> list[str]:
    return [a["addr"] for lst in (s.get("addresses") or {}).values() for a in lst
            if a.get("OS-EXT-IPS:type", "fixed") == "fixed" and a.get("addr")]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class EcsStatusCollector:
    code, service, name = "ecs_status", "ecs", "ECS 云服务器状态与资源使用"

    def collect(self, ctx: RunContext) -> list[Observation]:
        servers = [s for s in page_all(lambda p: ctx.client.get("ecs", "/v1/{project_id}/cloudservers/detail",
                                                                params=p), "servers", mode="page", start=1,
                                       total_key="count") if s.get("status") != "DELETED"]
        ids = [s["id"] for s in servers]
        metrics, source = self._metrics(ctx, ids, {ip: s["id"] for s in servers for ip in _fixed_ips(s)})
        now = utcnow()
        known: dict[str, Resource] = {}
        reboots: dict[str, int] = {}
        ctx.stats.update(metrics_source=source, servers=len(servers), with_metrics=len(metrics))
        if ctx.db is not None and ids:
            known = {r.resource_id: r for r in ctx.db.scalars(select(Resource).where(
                Resource.resource_type == RESOURCE_TYPE, Resource.resource_id.in_(ids),
                Resource.tenant_id == ctx.tenant_id if ctx.tenant_id is not None else Resource.tenant_id.is_(None)))}
            for ch in ctx.db.scalars(select(ResourceChange).where(
                    ResourceChange.resource_type == RESOURCE_TYPE, ResourceChange.resource_id.in_(ids),
                    ResourceChange.change_type.in_(("STATUS", "REBOOT")),
                    ResourceChange.detected_at >= now - timedelta(hours=24))):
                if ch.change_type == "REBOOT" or any(c.get("new") in REBOOTING for c in ch.changes or []):
                    reboots[ch.resource_id] = reboots.get(ch.resource_id, 0) + 1
        obs: list[Observation] = []
        for s in servers:
            sid, name, status = s["id"], s["name"], s.get("status") or "UNKNOWN"
            old, m = known.get(sid), dict(metrics.get(sid, {}))
            count = reboots.get(sid, 0)
            if status in REBOOTING and (old is None or old.status_raw not in REBOOTING):
                count += 1                                   # 本轮新看到的重启（状态变更由流水线记录）
            ps = s.get("OS-EXT-STS:power_state")
            power = POWER_STATE.get(ps, str(ps or "-")) if isinstance(ps, int) else str(ps or "-")
            attrs: dict = {"task_state": s.get("OS-EXT-STS:task_state") or None, "power_state": power,
                           "metrics_source": source}
            boot = m.pop("boot_time", None)
            if boot:
                prev = (old.attrs or {}).get("boot_ts") if old is not None else None
                # 开机时间前移超过 1 分钟，且晚于上次观测（上次看到时机器已在运行）才算重启，避免指标源时钟漂移误报
                seen = int(old.last_seen.timestamp()) if old is not None and old.last_seen else 0
                if old is not None and prev and boot - int(prev) > 60 and boot >= seen - 60:
                    self._record_boot(ctx, old, int(prev), int(boot))
                    count += 1
                attrs["boot_time"] = _iso(int(boot))
                attrs["boot_ts"] = int(boot)
            facts = {"status": status, "status_text": ECS_STATUS.get(status, status), "power_state": power,
                     "task_state": s.get("OS-EXT-STS:task_state") or "", "transitional": status in TRANSITIONAL}
            obs.append(Observation(RESOURCE_TYPE, sid, name, "instance_status", facts, status_raw=status, attrs=attrs))
            if "cpu_util" in m:
                obs.append(Observation(RESOURCE_TYPE, sid, name, "cpu", {"cpu_util": m["cpu_util"]},
                                       metrics={"cpu_util": m["cpu_util"]}))
            if "mem_util" in m:
                obs.append(Observation(RESOURCE_TYPE, sid, name, "memory", {"mem_util": m["mem_util"]},
                                       metrics={"mem_util": m["mem_util"]}))
            if "disk_util" in m:
                d = {"disk_util": m["disk_util"], "disk_mount": m.get("disk_mount", "-")}
                obs.append(Observation(RESOURCE_TYPE, sid, name, "disk", d, metrics=d))
            obs.append(Observation(RESOURCE_TYPE, sid, name, "reboot", {"reboots_24h": count},
                                   metrics={"reboots_24h": count}))
        return obs

    @staticmethod
    def _metrics(ctx: RunContext, ids: list[str], ip_index: dict[str, str]) -> tuple[dict[str, dict], str]:
        source = effective(ctx.tenant)
        data: dict[str, dict] = {}
        if source == "CES" and ids:
            data = guarded_ces(ctx, lambda: ces_metrics.ecs_metrics(ctx.client, ids), {})
            source = effective(ctx.tenant)                   # 本轮刚探测到 CES 不可用：改用 Prometheus / 只巡检状态
        if source == "PROMETHEUS" and ids:
            prom = prom_for(ctx)
            if prom is not None:
                try:
                    data = prom_metrics(prom, ip_index, ctx.params.get("prometheus"))
                    mark(ctx.tenant, "prometheus", True, "巡检查询成功")
                except PromError as e:
                    ctx.errors.append(str(e))
                    mark(ctx.tenant, "prometheus", False, str(e)[:160])
        return data, source

    @staticmethod
    def _record_boot(ctx: RunContext, r: Resource, prev: int, boot: int) -> None:
        """开机时间变化 = 发生过一次重启（时间取新的开机时间）。"""
        ctx.db.add(ResourceChange(tenant_id=r.tenant_id, resource_ref=r.id, resource_type=r.resource_type,
                                  resource_id=r.resource_id, name=r.name, change_type="REBOOT",
                                  changes=[{"field": "boot_time", "label": "开机时间", "old": _iso(prev),
                                            "new": _iso(boot)}],
                                  source="INSPECT", operator="system", detected_at=datetime.fromtimestamp(boot, UTC)))
        log.info("ecs %s rebooted at %s", r.name, _iso(boot))
