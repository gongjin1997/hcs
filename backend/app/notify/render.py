"""告警邮件渲染：主题规范 + Jinja2 模板（table 布局 + 内联样式，兼容 Outlook / Foxmail / 手机邮箱）。"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import sign_mail_action
from app.models import AlarmEvent, AlarmRule
from app.models.base import utcnow
from app.services.catalog import RESOURCE_TYPES, SERVICES, SEVERITY_COLOR, SEVERITY_LABEL, STATUS_LABEL

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "mail"
MAX_SUBJECT = 120


def fmt_dt(v: datetime | None) -> str:
    if not v:
        return "-"
    return v.astimezone(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d %H:%M:%S")


def humanize(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    m = int(seconds // 60)
    if m < 1:
        return f"{int(seconds)} 秒"
    if m < 60:
        return f"{m} 分钟"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"
    d, h = divmod(h, 24)
    return f"{d} 天 {h} 小时"


def fmt_iso(v: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not v:
        return "-"
    return datetime.fromisoformat(v).astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def nl2br(v: str | None) -> Markup:
    return Markup("<br>".join(escape(line) for line in (v or "-").splitlines()))


env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html.j2"]),
                  trim_blocks=True, lstrip_blocks=True)
env.filters.update(dt=fmt_dt, isodt=fmt_iso, humanize=humanize, nl2br=nl2br)


def _duration(alarm: AlarmEvent, now: datetime | None = None) -> float:
    end = alarm.resolved_at or now or utcnow()
    return (end - alarm.first_seen).total_seconds()


def build_subject(alarm: AlarmEvent, kind: str, now: datetime | None = None) -> str:
    """主题规范：【级别】【环境·租户】【服务】资源名 巡检项 当前值或状态（阈值），不超过 120 字符。"""
    tenant = (alarm.detail or {}).get("tenant")
    env_tag = get_settings().env_tag + (f"·{tenant}" if tenant else "")
    tag = RESOURCE_TYPES.get(alarm.resource_type, {}).get("tag", alarm.service)
    name = alarm.resource_name if not alarm.sub_object else f"{alarm.resource_name} / {alarm.sub_object}"
    if kind == "RESOLVED":
        head, body = f"【已恢复】【{env_tag}】【{tag}】", f"{name} {alarm.title}，持续 {humanize(_duration(alarm, now))}"
    else:
        prefix = {"UPGRADE": "【升级】", "REPEAT": "【重复】", "ESCALATE": "【未处理升级】"}.get(kind, "")
        head = f"{prefix}【{alarm.severity}-{SEVERITY_LABEL[alarm.severity]}】【{env_tag}】【{tag}】"
        body = f"{name} {alarm.title}"
    subject = head + body
    return subject if len(subject) <= MAX_SUBJECT else subject[: MAX_SUBJECT - 1] + "…"


def mail_context(db: Session, alarm: AlarmEvent, kind: str, recipient: str = "", route_names: list[str] | None = None,
                 now: datetime | None = None) -> dict:
    s = get_settings()
    rule = db.get(AlarmRule, alarm.rule_id)
    rt = RESOURCE_TYPES.get(alarm.resource_type, {})
    detail = alarm.detail or {}
    resolved = kind == "RESOLVED"
    duration = _duration(alarm, now)
    name = alarm.resource_name + (f" / {alarm.sub_object}" if alarm.sub_object else "")
    summary = (f"{'【核心】' if detail.get('critical') else ''}{rt.get('name', '')} {name} "
               f"{'已恢复正常' if resolved else alarm.title}，{'累计' if resolved else '已'}持续 {humanize(duration)}。")
    facts = detail.get("facts") or {}
    details_text = "\n".join(f"{k}: {v}" for k, v in facts.items() if k not in ("critical", "expected_running"))
    color = SEVERITY_COLOR["RESOLVED" if resolved else alarm.severity]
    sign = lambda action: f"{s.base_url}/m/action/{sign_mail_action(alarm.id, action, recipient)}"  # noqa: E731
    return {
        "color": color,
        "badge": "【已恢复】" if resolved else f"【{alarm.severity}-{SEVERITY_LABEL[alarm.severity]}】",
        "alarm": {
            "id": alarm.id, "rule_id": alarm.rule_id, "title": alarm.title, "summary": summary,
            "severity_text": f"{alarm.severity}-{SEVERITY_LABEL[alarm.severity]}",
            "status_text": STATUS_LABEL["RESOLVED"] if resolved else STATUS_LABEL.get(alarm.status, alarm.status),
            "service": SERVICES.get(alarm.service, alarm.service), "resource_type_text": rt.get("name", ""),
            "resource_name": name, "resource_id": detail.get("resource_id", "-"),
            "region": detail.get("region", s.hcs_region), "project": detail.get("project", "-"),
            "workspace": detail.get("workspace"), "check_item": alarm.check_item,
            "tenant": detail.get("tenant") or "-",
            "current_value": alarm.current_value or "-", "threshold_text": alarm.threshold_text or "-",
            "first_seen": alarm.first_seen, "last_seen": alarm.last_seen, "duration": duration,
            "count": alarm.count, "owner": alarm.owner, "details": details_text,
            "runbook": (rule.runbook if rule else None) or "请参考运维手册对应章节处理。",
        },
        "links": {"detail": f"{s.base_url}/alarms?id={alarm.id}", "ack": sign("ack"), "silence": sign("silence"),
                  "console": f"https://console.{s.hcs_region}.{s.hcs_external_domain}/",
                  "subscription": f"{s.base_url}/notify"},
        "related": [], "trend_cid": None, "resolved": resolved,
        "route_name": "、".join(route_names or []) or "-",
    }


def render_alarm_mail(db: Session, alarm: AlarmEvent, kind: str, recipient: str = "",
                      route_names: list[str] | None = None) -> tuple[str, str, str]:
    ctx = mail_context(db, alarm, kind, recipient, route_names)
    return (build_subject(alarm, kind), env.get_template("alarm.html.j2").render(**ctx),
            env.get_template("alarm.txt.j2").render(**ctx))
