"""Prometheus 指标：GET /metrics（文本格式）。

- 配置 INSPECTOR_METRICS_TOKEN 时需带 Authorization: Bearer <token>；未配置时不校验（生产 gen-env.sh 会生成令牌）
- docker compose 部署：nginx 的 /metrics 默认拒绝；
  在 deploy/nginx/snippets/metrics-allow.conf 放行 Prometheus 服务器 IP 后经控制台地址抓取
- 指标覆盖：活动告警、通知积压 / 失败、任务最近执行状态与连续失败次数、后台进程心跳、台账资源数、静默数、HTTP 请求
"""
import hmac
from datetime import timedelta

from fastapi import APIRouter, Header
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from app import __version__
from app.core import metrics as m
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.events import broadcaster
from app.core.response import BizError, ErrorCode
from app.models import AlarmEvent, HcsAccount, InspectionRun, Notification, Resource, Silence, WorkerHeartbeat
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

router = APIRouter()


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            self.lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def collect() -> str:
    s = get_settings()
    now = utcnow()
    out = _Out()
    out.metric("hcs_inspector_info", "gauge", "平台版本", [({"version": __version__, "env": s.env}, 1)])
    with SessionLocal() as db:
        codes = {t.id: t.code for t in db.scalars(select(HcsAccount))}
        tn = lambda tid: codes.get(tid, "-") if tid is not None else "-"  # noqa: E731
        rows = db.execute(select(AlarmEvent.tenant_id, AlarmEvent.service, AlarmEvent.severity, AlarmEvent.status,
                                 func.count()).where(AlarmEvent.status.in_(ACTIVE_STATUSES))
                          .group_by(AlarmEvent.tenant_id, AlarmEvent.service, AlarmEvent.severity,
                                    AlarmEvent.status)).all()
        out.metric("hcs_inspector_alarms_active", "gauge", "活动告警数（告警中 / 已认领 / 已抑制）",
                   [({"tenant": tn(t), "service": sv, "severity": sev, "status": st}, n) for t, sv, sev, st, n in rows])
        day = now - timedelta(hours=24)
        fired = db.execute(select(AlarmEvent.severity, func.count()).where(AlarmEvent.first_seen >= day)
                           .group_by(AlarmEvent.severity)).all()
        out.metric("hcs_inspector_alarms_fired_24h", "gauge", "近 24 小时新产生的告警数",
                   [({"severity": sev}, n) for sev, n in fired])
        backlog = db.execute(select(Notification.status, func.count()).where(
            Notification.status.in_(("PENDING", "RETRYING"))).group_by(Notification.status)).all()
        out.metric("hcs_inspector_notifications_backlog", "gauge", "待发送 / 重试中的通知",
                   [({"status": st}, n) for st, n in backlog] or [({"status": "PENDING"}, 0)])
        recent = db.execute(select(Notification.status, func.count()).where(Notification.created_at >= day)
                            .group_by(Notification.status)).all()
        out.metric("hcs_inspector_notifications_24h", "gauge", "近 24 小时通知（按状态）",
                   [({"status": st}, n) for st, n in recent])
        oldest = db.scalar(select(func.min(Notification.created_at)).where(
            Notification.status.in_(("PENDING", "RETRYING"))))
        out.metric("hcs_inspector_notification_oldest_pending_seconds", "gauge", "最早一条未发送通知的等待时长",
                   [({}, round((now - oldest).total_seconds(), 1) if oldest else 0)])
        last: dict[tuple, dict] = {}
        for code, tid, status, started, finished in db.execute(
                select(InspectionRun.task_code, InspectionRun.tenant_id, InspectionRun.status, InspectionRun.started_at,
                       InspectionRun.finished_at).where(InspectionRun.started_at >= now - timedelta(hours=26),
                                                        InspectionRun.status != "RUNNING")
                .order_by(InspectionRun.started_at.desc())):
            k = (code, tn(tid))
            st = last.setdefault(k, {"status": status, "at": started, "dur": (finished - started).total_seconds()
                                     if finished else 0, "fails": 0, "counting": True, "ok_at": None})
            if st["counting"] and status == "FAILED":
                st["fails"] += 1
            elif status != "FAILED":
                st["counting"] = False
                st["ok_at"] = st["ok_at"] or started
        lab = lambda k: {"task": k[0], "tenant": k[1]}  # noqa: E731
        out.metric("hcs_inspector_task_last_run_timestamp_seconds", "gauge", "任务最近一次执行时间（Unix 秒）",
                   [(lab(k), int(v["at"].timestamp())) for k, v in last.items()])
        out.metric("hcs_inspector_task_last_success_timestamp_seconds", "gauge", "任务最近一次成功时间（近 26 小时内）",
                   [(lab(k), int(v["ok_at"].timestamp())) for k, v in last.items() if v["ok_at"]])
        out.metric("hcs_inspector_task_last_duration_seconds", "gauge", "任务最近一次执行耗时",
                   [(lab(k), round(v["dur"], 2)) for k, v in last.items()])
        out.metric("hcs_inspector_task_last_status", "gauge", "任务最近一次执行状态（值恒为 1，看 status 标签）",
                   [({**lab(k), "status": v["status"]}, 1) for k, v in last.items()])
        out.metric("hcs_inspector_task_consecutive_failures", "gauge", "任务连续失败次数",
                   [(lab(k), v["fails"]) for k, v in last.items()])
        out.metric("hcs_inspector_worker_heartbeat_age_seconds", "gauge", "后台进程距上次心跳的秒数",
                   [({"worker": h.name, "host": h.host or "-"}, round((now - h.beat_at).total_seconds(), 1))
                    for h in db.scalars(select(WorkerHeartbeat))])
        res = db.execute(select(Resource.tenant_id, Resource.resource_type, Resource.health, func.count()).where(
            Resource.lifecycle == "ACTIVE").group_by(Resource.tenant_id, Resource.resource_type, Resource.health)).all()
        out.metric("hcs_inspector_resources", "gauge", "在用资源数（按租户 / 类型 / 健康状态）",
                   [({"tenant": tn(t), "type": rt, "health": h}, n) for t, rt, h, n in res])
        silences = sum(1 for x in db.scalars(select(Silence).where(Silence.cancelled_at.is_(None),
                                                                     Silence.starts_at <= now, Silence.ends_at > now)))
        out.metric("hcs_inspector_silences_active", "gauge", "有效期内的静默 / 维护窗口数", [({}, silences)])
    out.metric("hcs_inspector_sse_subscribers", "gauge", "本进程的实时推送连接数", [({}, broadcaster.count)])
    reqs, buckets, total, count = m.snapshot()
    out.metric("hcs_inspector_http_requests_total", "counter", "HTTP 请求数（本进程）",
               [({"method": k[0], "code": k[1]}, v) for k, v in sorted(reqs.items())])
    hist, acc = [], 0
    for b, n in zip((*m.BUCKETS, "+Inf"), buckets, strict=True):
        acc += n
        hist.append(({"le": b}, acc))
    out.lines += ["# HELP hcs_inspector_http_request_duration_seconds HTTP 请求耗时（本进程，不含实时推送长连接）",
                  "# TYPE hcs_inspector_http_request_duration_seconds histogram"]
    out.lines += [f'hcs_inspector_http_request_duration_seconds_bucket{{le="{lab_["le"]}"}} {v}' for lab_, v in hist]
    out.lines += [f"hcs_inspector_http_request_duration_seconds_sum {round(total, 4)}",
                  f"hcs_inspector_http_request_duration_seconds_count {count}"]
    return out.text()


@router.get("/metrics", include_in_schema=False)
def metrics(authorization: str | None = Header(default=None)):
    token = get_settings().metrics_token
    if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise BizError(ErrorCode.UNAUTHORIZED, "需要 /metrics 访问令牌", 401)
    return PlainTextResponse(collect(), media_type="text/plain; version=0.0.4; charset=utf-8")
