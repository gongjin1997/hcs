"""报表生成与发送：同一类型同一周期重复生成时覆盖旧快照（定时任务重跑也不会产生重复报表）。"""
import logging
from collections.abc import Callable
from datetime import date
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BizError, ErrorCode
from app.models import HcsAccount, Report
from app.models.base import utcnow
from app.notify.render import env
from app.notify.smtp import build_message
from app.notify.smtp import send as smtp_send

from .builder import KINDS, build_report, period_of, summary_of
from .excel import report_workbook

XLSX = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
log = logging.getLogger(__name__)


def generate(db: Session, kind: str, ref: date | None = None, created_by: str = "system",
             tenant_id: int | None = None) -> Report:
    """tenant_id 为空 = 全部租户合并统计（含分租户对比）；否则只统计该租户。"""
    if kind not in KINDS:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知报表类型：{kind}")
    if tenant_id is not None and db.get(HcsAccount, tenant_id) is None:
        raise BizError(ErrorCode.BAD_REQUEST, "租户不存在")
    start, end = period_of(kind, ref)
    data = build_report(db, kind, start, end, tenant_id=tenant_id)
    rpt = None
    if kind != "LEDGER":
        tq = Report.tenant_id == tenant_id if tenant_id is not None else Report.tenant_id.is_(None)
        rpt = db.scalar(select(Report).where(Report.kind == kind, Report.period_start == start, tq))
    if rpt is None:
        rpt = Report(kind=kind, period_start=start, period_end=end, tenant_id=tenant_id)
        db.add(rpt)
    rpt.title, rpt.data, rpt.summary = data["title"], data, summary_of(data)
    rpt.created_by, rpt.created_at = created_by, utcnow()
    db.commit()
    return rpt


def render_mail(report: Report) -> tuple[str, str, str]:
    s = get_settings()
    ctx = {"r": report.data, "link": f"{s.base_url}/reports?id={report.id}", "env_tag": s.env_tag}
    subject = f"【{s.env_tag}】{report.title}"
    return subject, env.get_template("report.html.j2").render(**ctx), env.get_template("report.txt.j2").render(**ctx)


def default_recipients(db: Session, report: Report) -> list[str]:
    """租户报表发给租户配置的收件人（未配置时不发送）；合并报表发给“报表设置”里的收件人（未设置时取 REPORT_MAIL_TO）。"""
    from app.services.settings_service import get_report

    if report.tenant_id is not None:
        t = db.get(HcsAccount, report.tenant_id)
        return list(t.report_mail_to) if t is not None and t.report_mail_to else []
    return list(get_report(db).mail_to)


def send_report(db: Session, report: Report, to: list[str] | None = None,
                sender: Callable[[EmailMessage], None] | None = None) -> list[str]:
    recipients = [x.strip() for x in (to or default_recipients(db, report)) if x and x.strip()]
    if not recipients:
        raise BizError(ErrorCode.BAD_REQUEST, "未指定收件人：租户报表请在租户接入里配置报表收件人，合并报表请在报表设置里配置")
    subject, html, text = render_mail(report)
    msg = build_message(subject=subject, html=html, text=text, recipients=recipients,
                        message_id=f"<report-{report.id}-{int(utcnow().timestamp())}@hcs-inspector>")
    msg.add_attachment(report_workbook(db, report.data), maintype=XLSX[0], subtype=XLSX[1],
                       filename=f"{report.title}.xlsx")
    try:
        (sender or smtp_send)(msg)
    except Exception as e:  # noqa: BLE001  记录失败原因后抛给调用方
        report.send_error = f"{type(e).__name__}: {e}"[:500]
        db.commit()
        raise BizError(ErrorCode.INTERNAL, f"报表邮件发送失败：{report.send_error}", 502) from e
    report.sent_to, report.sent_at, report.send_error = recipients, utcnow(), None
    db.commit()
    return recipients


def scheduled(db: Session, kind: str) -> list[Report]:
    """定时任务入口：生成上一个完整周期的合并报表 + 每个启用租户的报表；配置了收件人的自动发送。"""
    out = [generate(db, kind)]
    for t in db.scalars(select(HcsAccount).where(HcsAccount.enabled.is_(True)).order_by(HcsAccount.id)):
        out.append(generate(db, kind, tenant_id=t.id))
    for rpt in out:
        if default_recipients(db, rpt):
            try:
                send_report(db, rpt)
            except BizError as e:
                log.error("report %s send failed: %s", rpt.id, e.message)
    return out
