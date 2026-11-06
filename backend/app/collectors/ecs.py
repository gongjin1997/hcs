"""ECS 云服务器巡检（任务 ecs_status，默认每 120 秒）。

1. 实例状态：GET /v1/{project_id}/cloudservers/detail（HCS 8.5.1 ECS 接口）——status、OS-EXT-STS:task_state / power_state。
   状态变化由流水线写入台账变更（类型“状态变更”），资源详情里能看到“几点几分从运行中变为关机”
2. 规则使用独立 ecs_metrics 任务保存的有效采样，不重复查询指标源
3. 重启：轮询时看到 REBOOT / HARD_REBOOT 记一次；指标任务发现开机时间变化另记一次
"""
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select

from app.hcs import HcsError
from app.hcs.pager import page_all
from app.metrics.ecs_readings import ecs_source
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
        from app.services.ecs_monitoring import parse_at

        policy = None
        selected = None
        if ctx.db is not None:
            from app.services import collection_policy
            from app.services.ecs_monitoring import policy_dict

            policy = ctx.params.get("_collection_policy") or policy_dict(ctx.db, ctx.tenant_id)
            if not collection_policy.stage_enabled(ctx.db, ctx.tenant_id, "inspection", policy):
                return []
            selected = collection_policy.select_resources(ctx.db, ctx.tenant_id, ctx.project_ref, policy)
            if not selected and policy["selection"]["mode"] != "all":
                ctx.stats.update(servers=0, with_metrics=0, inspection_skipped="no_selected_resources")
                return []

        def fetch_page(params: dict) -> dict:
            if policy is not None:
                params = {**params, "expect_fields": "basic"}
            data = ctx.client.get("ecs", "/v1/{project_id}/cloudservers/detail", params=params)
            count = data.get("count")
            # ECS 5.2.5 表 5-70 定义整数总数，不能退回通用分页器的短页结束推断。
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "ECS 列表 count 缺失或格式不正确，未使用不完整清单")
            return data

        if policy is not None and policy["selection"]["mode"] != "all" and len(selected or []) <= 3:
            servers = []
            for resource in selected or []:
                try:
                    body = ctx.client.get("ecs", f"/v1/{{project_id}}/cloudservers/{quote(resource.resource_id, safe='')}",
                                          params={"expect_fields": "basic"})
                    server = body.get("server")
                    if not isinstance(server, dict) or server.get("id") != resource.resource_id:
                        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "ECS 单机状态响应与请求实例不一致")
                    servers.append(server)
                except HcsError:
                    ctx.errors.append("所选 ECS 实例的状态查询失败，请核对实例是否存在及项目权限")
                    servers.append({"id": resource.resource_id, "name": resource.name, "_state_unavailable": True})
        else:
            try:
                servers = [s for s in page_all(fetch_page, "servers", mode="page", start=1,
                                               total_key="count") if s.get("status") != "DELETED"]
            except HcsError:
                if ctx.db is None or not selected:
                    raise
                ctx.errors.append("ECS 状态查询失败，本轮仅使用仍有效的独立指标，不推断机器状态")
                servers = [{"id": resource.resource_id, "name": resource.name, "_state_unavailable": True}
                           for resource in selected or []]
        if policy is not None and policy["selection"]["mode"] != "all":
            allowed = {resource.resource_id for resource in selected or []}
            servers = [server for server in servers if server["id"] in allowed]
        scope = ctx.scope(RESOURCE_TYPE)     # 采集范围：按 ID / 名称先裁剪；环境、业务系统等台账字段由流水线判定
        if not scope.open:
            servers = [s for s in servers if scope.keep({"id": s["id"], "name": s.get("name") or ""})]
        ids = [s["id"] for s in servers]
        metrics, source = self._metrics(ctx, ids, {ip: s["id"] for s in servers for ip in _fixed_ips(s)})
        now = utcnow()
        known: dict[str, Resource] = {}
        reboots: dict[str, int] = {}
        ctx.stats.update(metrics_source=source, servers=len(servers), with_metrics=len(metrics))
        if ctx.db is not None and ids:
            known = {r.resource_id: r for r in ctx.db.scalars(select(Resource).where(
                Resource.resource_type == RESOURCE_TYPE, Resource.resource_id.in_(ids),
                Resource.project_ref == ctx.project_ref,
                Resource.tenant_id == ctx.tenant_id if ctx.tenant_id is not None else Resource.tenant_id.is_(None)))}
            for ch in ctx.db.scalars(select(ResourceChange).where(
                    ResourceChange.resource_ref.in_([r.id for r in known.values()]),
                    ResourceChange.change_type.in_(("STATUS", "REBOOT")),
                    ResourceChange.detected_at >= now - timedelta(hours=24))):
                if ch.change_type == "REBOOT" or any(c.get("new") in REBOOTING for c in ch.changes or []):
                    reboots[ch.resource_id] = reboots.get(ch.resource_id, 0) + 1
        obs: list[Observation] = []
        for s in servers:
            sid, name, status = s["id"], s["name"], s.get("status") or "UNKNOWN"
            old, m = known.get(sid), dict(metrics.get(sid, {}))
            if policy is not None and (policy.get("metrics") or {}).get("enabled", True):
                for key in policy.get("metric_keys", ("cpu_util", "mem_util", "disk_util")):
                    m.setdefault(key, None)
            count = reboots.get(sid, 0)
            if status in REBOOTING and (old is None or old.status_raw not in REBOOTING):
                count += 1                                   # 本轮新看到的重启（状态变更由流水线记录）
            ps = s.get("OS-EXT-STS:power_state")
            power = POWER_STATE.get(ps, str(ps or "-")) if isinstance(ps, int) else str(ps or "-")
            attrs: dict = {"task_state": s.get("OS-EXT-STS:task_state") or None, "power_state": power,
                           "metrics_source": source, "ecs_state_checked_at": now.isoformat()}
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
            known_status = status in ECS_STATUS and status != "UNKNOWN"
            if not known_status and not s.get("_state_unavailable"):
                ctx.errors.append("ECS 实例状态缺失或未识别，本次不判定为正常")
            facts = {"status": status if known_status else None, "status_text": ECS_STATUS.get(status, status),
                     "power_state": power,
                     "task_state": s.get("OS-EXT-STS:task_state") or "", "transitional": status in TRANSITIONAL}
            if not s.get("_state_unavailable"):
                obs.append(Observation(RESOURCE_TYPE, sid, name, "instance_status", facts, status_raw=status, attrs=attrs,
                                       metrics={"cpu_util": None, "mem_util": None, "disk_util": None, "disk_mount": None}))
            if "cpu_util" in m:
                obs.append(Observation(RESOURCE_TYPE, sid, name, "cpu", {"cpu_util": m["cpu_util"]},
                    metrics={"cpu_util": m["cpu_util"]}, metric_observed_at=parse_at(m.get("cpu_util_observed_at")),
                    metric_config_signature=m.get("metric_config_signature")))
            if "mem_util" in m:
                obs.append(Observation(RESOURCE_TYPE, sid, name, "memory", {"mem_util": m["mem_util"]},
                    metrics={"mem_util": m["mem_util"]}, metric_observed_at=parse_at(m.get("mem_util_observed_at")),
                    metric_config_signature=m.get("metric_config_signature")))
            if "disk_util" in m:
                d = {"disk_util": m["disk_util"], "disk_mount": m.get("disk_mount", "-")}
                obs.append(Observation(RESOURCE_TYPE, sid, name, "disk", d, metrics=d,
                    metric_observed_at=parse_at(m.get("disk_util_observed_at")),
                    metric_config_signature=m.get("metric_config_signature")))
            obs.append(Observation(RESOURCE_TYPE, sid, name, "reboot", {"reboots_24h": count},
                                   metrics={"reboots_24h": count}))
        return obs

    @staticmethod
    def _metrics(ctx: RunContext, ids: list[str], ip_index: dict[str, str]) -> tuple[dict[str, dict], str]:
        """只读独立采样的缓存；同一个指标点由规则流水线去重评估。"""
        from app.services.ecs_monitoring import config_signature, included, number, parse_at, policy_dict

        policy = ctx.params
        if ctx.db is not None:
            policy = ctx.params.get("_collection_policy") or policy_dict(ctx.db, ctx.tenant_id)
        source = ecs_source(ctx.tenant, policy, client=ctx.client)
        data: dict[str, dict] = {}
        if ctx.db is None or source == "NONE":
            return data, source
        if not policy["metrics"]["enabled"]:
            return data, source
        now, signature = utcnow(), config_signature(ctx.tenant, policy, source)
        for offset in range(0, len(ids), 500):
            rows = ctx.db.scalars(select(Resource).where(Resource.tenant_id == ctx.tenant_id,
                Resource.project_ref == ctx.project_ref, Resource.resource_type == RESOURCE_TYPE,
                Resource.resource_id.in_(ids[offset:offset+500])))
            for resource in rows:
                meta = (resource.attrs or {}).get("ecs_monitoring") or {}
                if (not included(resource, policy) or not isinstance(meta, dict)
                        or meta.get("config_signature") != signature or meta.get("source") != source):
                    continue
                for key, point in (meta.get("latest") or {}).items():
                    if (key not in policy["metric_keys"] or not isinstance(point, dict)
                            or point.get("source") != source):
                        continue
                    at, value = parse_at(point.get("observed_at")), number(point.get("value"))
                    if (at is None or value is None or not now-timedelta(seconds=policy["stale_after_sec"]) <= at
                            <= now+timedelta(seconds=30)):
                        continue
                    current = data.setdefault(resource.resource_id, {})
                    current["metric_config_signature"] = signature
                    current[key], current[f"{key}_observed_at"] = value, at.isoformat()
                    if key == "disk_util":
                        dims = point.get("dimensions") or {}
                        current["disk_mount"] = dims.get("mountpoint", dims.get("mount_point", "-"))
        return data, source

    @staticmethod
    def _record_boot(ctx: RunContext, r: Resource, prev: int, boot: int) -> None:
        """开机时间变化 = 发生过一次重启（时间取新的开机时间）。"""
        ctx.db.add(ResourceChange(tenant_id=r.tenant_id, resource_ref=r.id, resource_type=r.resource_type,
                                  resource_id=r.resource_id, name=r.name, change_type="REBOOT",
                                  changes=[{"field": "boot_time", "label": "开机时间", "old": _iso(prev),
                                            "new": _iso(boot)}],
                                  source="INSPECT", operator="system", detected_at=datetime.fromtimestamp(boot, UTC)))
        log.debug("ecs_reboot_observed", extra={"event": "ecs_reboot_observed", "cloud_service": "ecs",
                  "tenant_id": ctx.tenant_id, "project_id": ctx.project_id, "resource_ref": r.id, "boot_time": _iso(boot)})
