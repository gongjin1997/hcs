"""报表生成与发送：同一类型同一周期重复生成时覆盖旧快照（定时任务重跑也不会产生重复报表）。"""
import logging
from collections.abc import Callable
from datetime import date
from email.message import EmailMessage
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import log_context
from app.core.response import BizError, ErrorCode
from app.models import HcsAccount, Report
from app.models.base import utcnow
from app.notify.render import env
from app.notify.smtp import build_message
from app.notify.smtp import send as smtp_send
from app.services import mail_settings_service
from app.services.recipients import emails_for_users, ids_for_emails, validate_user_ids

from .builder import KINDS, build_report, period_of, summary_of
from .excel import report_workbook

XLSX = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
log = logging.getLogger(__name__)


def generate(db: Session, kind: str, ref: date | None = None, created_by: str = "system",
             tenant_id: int | None = None) -> Report:
    """tenant_id 为空 = 全部租户合并统计（含分租户对比）；否则只统计该租户。"""
    with log_context(task="report_generate", report_kind=kind, tenant_id=tenant_id):
        started = perf_counter()
        log.info("report_generation_started", extra={"event": "report_generation_started"})
        try:
            report = _generate(db, kind, ref, created_by, tenant_id)
        except Exception as exc:
            log.warning("report_generation_failed", extra={"event": "report_generation_failed",
                        "exception_type": type(exc).__name__, "cost_ms": round((perf_counter() - started) * 1000, 2)},
                        exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            raise
        log.info("report_generation_finished", extra={"event": "report_generation_finished", "report_id": report.id,
                 "cost_ms": round((perf_counter() - started) * 1000, 2)})
        return report


def _generate(db: Session, kind: str, ref: date | None, created_by: str, tenant_id: int | None) -> Report:
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
    rpt.sent_to, rpt.sent_at, rpt.send_error = [], None, None
    db.commit()
    return rpt


def render_mail(report: Report) -> tuple[str, str, str]:
    s = get_settings()
    ctx = {"r": report.data, "link": f"{s.base_url}/reports?id={report.id}"}
    return report.title, env.get_template("report.html.j2").render(**ctx), env.get_template("report.txt.j2").render(**ctx)


def default_recipient_user_ids(db: Session, report: Report) -> list[int]:
    """报表只使用用户引用；0009 已迁移租户旧邮箱，空 ID 列表明确表示不发送。"""
    from app.services.settings_service import get_report

    if report.tenant_id is not None:
        t = db.get(HcsAccount, report.tenant_id)
        if t is None:
            return []
        return list(t.report_user_ids)
    return list(get_report(db).recipient_user_ids)


def default_recipients(db: Session, report: Report) -> list[str]:
    return emails_for_users(db, default_recipient_user_ids(db, report), tenant_id=report.tenant_id)


def send_report(db: Session, report: Report, to: list[str] | None = None,
                sender: Callable[[EmailMessage], None] | None = None, *,
                recipient_user_ids: list[int] | None = None) -> list[str]:
    with log_context(task="report_send", report_id=report.id, report_kind=report.kind, tenant_id=report.tenant_id):
        started = perf_counter()
        try:
            recipients = _send_report(db, report, to, sender, recipient_user_ids=recipient_user_ids)
        except Exception as exc:
            log.warning("report_delivery_failed", extra={"event": "report_delivery_failed",
                        "exception_type": type(exc).__name__, "cost_ms": round((perf_counter() - started) * 1000, 2)},
                        exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            raise
        log.info("report_delivery_finished", extra={"event": "report_delivery_finished", "recipient_count": len(recipients),
                 "cost_ms": round((perf_counter() - started) * 1000, 2)})
        return recipients


def _send_report(db: Session, report: Report, to: list[str] | None,
                 sender: Callable[[EmailMessage], None] | None, *,
                 recipient_user_ids: list[int] | None = None) -> list[str]:
    if recipient_user_ids is not None:
        ids = validate_user_ids(db, recipient_user_ids, tenant_id=report.tenant_id)
    elif to is not None:
        ids = validate_user_ids(db, ids_for_emails(db, to, tenant_id=report.tenant_id), tenant_id=report.tenant_id)
    else:
        ids = default_recipient_user_ids(db, report)
    recipients = emails_for_users(db, ids, tenant_id=report.tenant_id)
    if not recipients:
        raise BizError(ErrorCode.BAD_REQUEST, "未指定可用收件人：请在用户管理中维护启用、通知、邮箱及租户范围，"
                       "并在租户接入或报表设置里选择人员")
    subject, html, text = render_mail(report)
    msg = build_message(subject=subject, html=html, text=text, recipients=recipients,
                        message_id=f"<report-{report.id}-{int(utcnow().timestamp())}@hcs-inspector>",
                        settings=mail_settings_service.get(db).smtp)
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
            except BizError:
                # send_report 已记录带 report_id/tenant_id 的安全异常栈；此处不重复打印摘要或正文。
                continue
    return out
