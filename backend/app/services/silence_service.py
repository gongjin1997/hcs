"""维护窗口与告警静默：匹配、生效判断（含每周重复的维护窗口）、告警抑制与静默结束后的补发通知。

告警状态流转：
- 新告警（或恢复后重开）命中生效中的静默 → SUPPRESSED（记录 silence_id，写 SILENCE 处理记录，不发通知）
- 静默生效时已经在“告警中”的匹配告警 → 由 sweep() 转为 SUPPRESSED（创建静默时可选择立即生效）
- 静默结束（到期 / 取消 / 每周窗口结束）时告警仍未恢复 → 转回 FIRING，写 UNSILENCE 处理记录并补发告警通知
- 已认领（ACKED）的告警不会被静默：已经有人在处理，保持原状态
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BizError, ErrorCode
from app.models import AlarmEvent, AlarmEventLog, Silence
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

MATCH_KEYS = ("tenant_ids", "services", "resource_types", "rule_ids", "severities", "resource_refs", "fingerprints",
              "name_contains")
KIND_LABEL = {"SILENCE": "静默", "MAINTENANCE": "维护窗口"}
MAX_DAYS = 31                                   # 单个静默最长 31 天；每周重复的维护窗口有效期最长 366 天
MAX_RECURRING_DAYS = 366


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _hm(v: str) -> time:
    try:
        h, m = v.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError) as e:
        raise BizError(ErrorCode.BAD_REQUEST, f"时间格式应为 HH:MM：{v}") from e


def normalize_matchers(m: dict) -> dict:
    out: dict = {}
    for k in MATCH_KEYS:
        v = m.get(k)
        if k == "name_contains":
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()[:128]
        elif v:
            if not isinstance(v, list):
                raise BizError(ErrorCode.BAD_REQUEST, f"匹配条件 {k} 应为数组")
            out[k] = sorted({int(x) for x in v}) if k in ("tenant_ids", "resource_refs") else sorted({str(x) for x in v})
    if not out:
        raise BizError(ErrorCode.BAD_REQUEST, "至少需要一个匹配条件（例如租户、服务、资源或规则），防止误把全部告警静默")
    return out


def normalize_recurrence(r: dict | None) -> dict:
    if not r:
        return {}
    days = sorted({int(d) for d in r.get("weekdays") or []})
    if not days or any(d < 1 or d > 7 for d in days):
        raise BizError(ErrorCode.BAD_REQUEST, "每周重复需要选择星期（1=周一 … 7=周日）")
    start, end = _hm(r.get("start", "")), _hm(r.get("end", ""))
    if start == end:
        raise BizError(ErrorCode.BAD_REQUEST, "维护窗口的开始和结束时间不能相同")
    return {"weekdays": days, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}


def validate_window(starts_at: datetime, ends_at: datetime, recurrence: dict) -> None:
    if ends_at <= starts_at:
        raise BizError(ErrorCode.BAD_REQUEST, "结束时间必须晚于开始时间")
    limit = MAX_RECURRING_DAYS if recurrence else MAX_DAYS
    if ends_at - starts_at > timedelta(days=limit):
        raise BizError(ErrorCode.BAD_REQUEST, f"{'每周维护窗口的有效期' if recurrence else '静默时长'}最长 {limit} 天")


def in_window(s: Silence, now: datetime) -> bool:
    """每周重复的维护窗口：当前时间是否落在某个窗口内（支持跨零点，例如周四 22:00 至周五 02:00）。"""
    rec = s.recurrence or {}
    if not rec:
        return True
    local = now.astimezone(_tz())
    start, end, days, t = _hm(rec["start"]), _hm(rec["end"]), rec["weekdays"], local.time()
    if start < end:
        return local.isoweekday() in days and start <= t < end
    yesterday = (local.isoweekday() - 2) % 7 + 1
    return (local.isoweekday() in days and t >= start) or (yesterday in days and t < end)


def is_active(s: Silence, now: datetime) -> bool:
    return s.cancelled_at is None and s.starts_at <= now < s.ends_at and in_window(s, now)


def status_of(s: Silence, now: datetime) -> str:
    """ACTIVE 生效中 / WAITING 有效期内、等待下一个窗口 / PENDING 未开始 / EXPIRED 已结束 / CANCELLED 已取消"""
    if s.cancelled_at is not None:
        return "CANCELLED"
    if now < s.starts_at:
        return "PENDING"
    if now >= s.ends_at:
        return "EXPIRED"
    return "ACTIVE" if in_window(s, now) else "WAITING"


def next_window(s: Silence, now: datetime) -> tuple[datetime, datetime] | None:
    """下一个（或当前）生效时段，用于列表展示。"""
    if s.cancelled_at is not None or now >= s.ends_at:
        return None
    rec = s.recurrence or {}
    if not rec:
        return max(now, s.starts_at), s.ends_at
    tz = _tz()
    base = max(now, s.starts_at).astimezone(tz)
    start, end = _hm(rec["start"]), _hm(rec["end"])
    for i in range(-1, 8):
        day = (base + timedelta(days=i)).date()
        if day.isoweekday() not in rec["weekdays"]:
            continue
        a = datetime.combine(day, start, tz)
        b = datetime.combine(day + timedelta(days=0 if start < end else 1), end, tz)
        if b > base and a < s.ends_at:
            return max(a, s.starts_at), min(b, s.ends_at)
    return None


def matches(m: dict, alarm: AlarmEvent) -> bool:
    name = (m.get("name_contains") or "").lower()
    return all([
        not m.get("tenant_ids") or alarm.tenant_id in m["tenant_ids"],
        not m.get("services") or alarm.service in m["services"],
        not m.get("resource_types") or alarm.resource_type in m["resource_types"],
        not m.get("rule_ids") or alarm.rule_id in m["rule_ids"],
        not m.get("severities") or alarm.severity in m["severities"],
        not m.get("resource_refs") or alarm.resource_ref in m["resource_refs"],
        not m.get("fingerprints") or alarm.fingerprint in m["fingerprints"],
        not name or name in (alarm.resource_name or "").lower(),
    ])


def active_silences(db: Session, now: datetime) -> list[Silence]:
    rows = db.scalars(select(Silence).where(Silence.cancelled_at.is_(None), Silence.starts_at <= now,
                                            Silence.ends_at > now).order_by(Silence.id))
    return [s for s in rows if in_window(s, now)]


def find_silence(db: Session, alarm: AlarmEvent, now: datetime) -> Silence | None:
    return next((s for s in active_silences(db, now) if matches(s.matchers or {}, alarm)), None)


def suppress(db: Session, alarm: AlarmEvent, s: Silence, now: datetime) -> None:
    prev = alarm.status
    alarm.status, alarm.silence_id = "SUPPRESSED", s.id
    alarm.suppress_reason = f"{KIND_LABEL.get(s.kind, '静默')}：{s.name}"[:64]
    s.suppressed_total = (s.suppressed_total or 0) + 1
    until = next_window(s, now)
    db.add(AlarmEventLog(alarm_id=alarm.id, action="SILENCE", from_status=prev, to_status="SUPPRESSED",
                         operator=s.created_by, at=now,
                         note=f"命中{KIND_LABEL.get(s.kind, '静默')}“{s.name}”（#{s.id}），"
                              f"{until[1].astimezone(_tz()).strftime('%m-%d %H:%M') + ' 前' if until else ''}不发通知"))


def unsuppress(db: Session, alarm: AlarmEvent, now: datetime, note: str) -> None:
    from . import alarm_service

    alarm.status = "FIRING"
    alarm.suppress_reason = None
    db.add(AlarmEventLog(alarm_id=alarm.id, action="UNSILENCE", from_status="SUPPRESSED", to_status="FIRING",
                         operator="system", note=note, at=now))
    alarm_service.enqueue_notification(db, alarm, "FIRING", now)


def sweep(db: Session, now: datetime | None = None, only: Silence | None = None) -> dict:
    """每分钟执行（通知进程）：静默结束的告警恢复为 FIRING 并补发通知；新进入生效期的静默抑制匹配的告警。"""
    now = now or utcnow()
    actives = [only] if only is not None and is_active(only, now) else active_silences(db, now)
    active_ids = {s.id for s in actives}
    released = suppressed = 0
    if only is None:
        for a in db.scalars(select(AlarmEvent).where(AlarmEvent.status == "SUPPRESSED",
                                                     AlarmEvent.silence_id.is_not(None))).all():
            if a.silence_id in active_ids:
                continue
            other = next((s for s in actives if matches(s.matchers or {}, a)), None)
            if other is not None:                      # 原静默结束，但又命中了另一个生效中的静默
                a.silence_id = other.id
                a.suppress_reason = f"{KIND_LABEL.get(other.kind, '静默')}：{other.name}"[:64]
                continue
            unsuppress(db, a, now, "静默 / 维护窗口已结束，告警仍未恢复，补发告警通知")
            released += 1
    for a in db.scalars(select(AlarmEvent).where(AlarmEvent.status == "FIRING")).all():
        s = next((s for s in actives if matches(s.matchers or {}, a)), None)
        if s is not None:
            suppress(db, a, s, now)
            suppressed += 1
    db.commit()
    return {"released": released, "suppressed": suppressed}


def release(db: Session, s: Silence, now: datetime, operator: str) -> int:
    """取消 / 修改静默后调用：它抑制的告警如果已不再被它覆盖（已取消、已不生效或条件不再匹配），
    命中其他生效中的静默时转过去，否则转回 FIRING 并补发通知。返回补发通知的告警数。"""
    others = [x for x in active_silences(db, now) if x.id != s.id]
    still = is_active(s, now)
    n = 0
    for a in db.scalars(select(AlarmEvent).where(AlarmEvent.status == "SUPPRESSED",
                                                 AlarmEvent.silence_id == s.id)).all():
        if still and matches(s.matchers or {}, a):
            continue
        other = next((x for x in others if matches(x.matchers or {}, a)), None)
        if other is not None:
            a.silence_id = other.id
            a.suppress_reason = f"{KIND_LABEL.get(other.kind, '静默')}：{other.name}"[:64]
            continue
        verb = "取消了" if s.cancelled_at is not None else "修改了"
        unsuppress(db, a, now, f"{operator} {verb}{KIND_LABEL.get(s.kind, '静默')}“{s.name}”，告警仍未恢复，补发告警通知")
        n += 1
    return n


def describe(m: dict, tenant_names: dict[int, str] | None = None) -> str:
    """匹配条件 → 中文描述（列表展示、审计记录）。"""
    from .catalog import SERVICES, type_name

    parts = []
    if m.get("tenant_ids"):
        parts.append("租户：" + "、".join((tenant_names or {}).get(t, f"#{t}") for t in m["tenant_ids"]))
    if m.get("services"):
        parts.append("服务：" + "、".join(SERVICES.get(x, x) for x in m["services"]))
    if m.get("resource_types"):
        parts.append("类型：" + "、".join(type_name(x) for x in m["resource_types"]))
    if m.get("severities"):
        parts.append("级别：" + "、".join(m["severities"]))
    if m.get("rule_ids"):
        parts.append("规则：" + "、".join(m["rule_ids"]))
    if m.get("resource_refs"):
        parts.append(f"指定资源 {len(m['resource_refs'])} 个")
    if m.get("fingerprints"):
        parts.append(f"指定告警 {len(m['fingerprints'])} 条")
    if m.get("name_contains"):
        parts.append(f"名称包含“{m['name_contains']}”")
    return "；".join(parts)


def preview(db: Session, matchers: dict, tenant_ids: list[int] | None, limit: int = 100) -> dict:
    """保存前预览：当前会被命中的活动告警（防止误伤核心告警）。"""
    m = normalize_matchers(matchers)
    q = select(AlarmEvent).where(AlarmEvent.status.in_(ACTIVE_STATUSES))
    if tenant_ids is not None:
        q = q.where(AlarmEvent.tenant_id.in_(tenant_ids))
    hits = [a for a in db.scalars(q.order_by(AlarmEvent.severity, AlarmEvent.first_seen.desc())) if matches(m, a)]
    return {"total": len(hits), "critical": sum(1 for a in hits if (a.detail or {}).get("critical")),
            "by_severity": {s: sum(1 for a in hits if a.severity == s) for s in ("P1", "P2", "P3", "P4")},
            "items": hits[:limit]}
