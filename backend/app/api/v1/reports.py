"""报表中心：日报 / 周报 / 月报 / 台账报表的生成、查看、导出 Excel、邮件发送。

- 报表分“全部租户合并报表”（tenant_id 为空，只有拥有全部租户权限的用户可见）和“租户报表”
- 权限：report:view 查看 / 导出，report:manage 生成 / 发送，report:delete 删除；写操作记操作审计
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, not_found, ok
from app.core.xlsx import xlsx_response
from app.models import HcsAccount, Report
from app.models.base import utcnow
from app.reports.builder import KINDS
from app.reports.excel import report_workbook
from app.reports.service import default_recipients, generate, send_report
from app.schemas.report import ReportBrief, ReportIn, ReportOut, SendIn
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/reports", tags=["报表中心"])
VIEW = Depends(require_perm("report:view"))


def _get(db: Session, scope: Scope, rid: int) -> Report:
    rpt = db.get(Report, rid)
    if rpt is None:
        raise not_found("报表")
    scope.ensure(rpt.tenant_id, "报表")
    return rpt


def _name(tm: dict[int, HcsAccount], tid: int | None) -> str:
    t = tm.get(tid) if tid is not None else None
    return (t.short_name or t.name) if t else ("全部租户" if tid is None else f"租户#{tid}")


@router.get("", response_model=ApiResponse[Page[ReportBrief]], summary="报表列表（按周期倒序）", dependencies=[VIEW])
def list_reports(kind: str | None = None, tenant_id: str | None = Query(None, description="all / global / 租户 ID"),
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
                 scope: Scope = Depends(get_scope)):
    q = scope.apply(select(Report), Report.tenant_id, include_global=True)
    if kind:
        if kind not in KINDS:
            raise BizError(ErrorCode.BAD_REQUEST, f"未知报表类型：{kind}")
        q = q.where(Report.kind == kind)
    if tenant_id == "global":
        q = q.where(Report.tenant_id.is_(None))
    elif tenant_id not in (None, "", "all"):
        try:
            q = q.where(Report.tenant_id == int(tenant_id))
        except ValueError as e:
            raise BizError(ErrorCode.BAD_REQUEST, "tenant_id 格式不正确") from e
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Report.period_start.desc(), Report.tenant_id.is_not(None), Report.tenant_id,
                                 Report.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    tm = tenant_map(db)
    items = [ReportBrief.model_validate(r).model_copy(update={"tenant_name": _name(tm, r.tenant_id)}) for r in rows]
    return ok(Page(items=items, total=total, page=page, page_size=page_size))


@router.post("", response_model=ApiResponse[ReportOut], summary="生成报表（同类型、同周期、同租户再次生成会覆盖）")
def create_report(body: ReportIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("report:manage"))):
    if body.date and body.date > utcnow().date():
        raise BizError(ErrorCode.BAD_REQUEST, "不能生成未来周期的报表")
    tenant_id = body.tenant_id
    if tenant_id is None and not scope.full:
        tenant_id = scope.single          # 只有部分租户权限的用户：默认生成自己租户的报表
        if tenant_id is None:
            raise BizError(ErrorCode.FORBIDDEN, "无权生成全部租户合并报表，请选择租户", 403)
    if tenant_id is not None and db.get(HcsAccount, tenant_id) is None:
        raise not_found("租户")
    if not scope.allows(tenant_id):
        raise BizError(ErrorCode.FORBIDDEN, "无权生成该租户的报表", 403)
    rpt = generate(db, body.kind, body.date, created_by=cu.display_name, tenant_id=tenant_id)
    audit.log("report.create", target_type="report", target_id=rpt.id, target_name=rpt.title, tenant_id=tenant_id,
              detail={"kind": body.kind, "date": body.date.isoformat() if body.date else None})
    return ok(ReportOut.model_validate(rpt).model_copy(update={"tenant_name": _name(tenant_map(db), tenant_id)}))


@router.get("/schedule", summary="定时报表配置（只读，来自环境变量 REPORT_CRON / REPORT_MAIL_TO）", dependencies=[VIEW])
def schedule(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    s = get_settings()
    tenants = [{"id": t.id, "name": t.short_name or t.name, "mail_to": t.report_mail_to or []}
               for t in tenant_map(db).values() if scope.allows(t.id)]
    return ok({"cron": s.report_cron, "mail_to": s.report_mail_to if scope.full else [], "timezone": s.timezone,
               "kinds": KINDS, "tenants": tenants,
               "note": "定时任务每个周期生成 1 份全部租户合并报表 + 每个启用租户 1 份租户报表；租户报表发给租户配置的收件人"})


@router.get("/{rid}", response_model=ApiResponse[ReportOut], summary="报表详情", dependencies=[VIEW])
def get_report(rid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    rpt = _get(db, scope, rid)
    return ok(ReportOut.model_validate(rpt).model_copy(update={
        "tenant_name": _name(tenant_map(db), rpt.tenant_id), "default_to": default_recipients(db, rpt)}))


@router.get("/{rid}/export", summary="导出报表 Excel", dependencies=[VIEW])
def export_report(rid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    rpt = _get(db, scope, rid)
    return xlsx_response(report_workbook(db, rpt.data), f"{rpt.title}.xlsx")


@router.post("/{rid}/send", summary="发送报表邮件（正文摘要 + Excel 附件）")
def send(rid: int, body: SendIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
         audit: Auditor = Depends(get_auditor), _: CurrentUser = Depends(require_perm("report:manage"))):
    rpt = _get(db, scope, rid)
    try:
        to = send_report(db, rpt, body.to)
    except BizError as e:
        audit.log("report.send", target_type="report", target_id=rpt.id, target_name=rpt.title,
                  tenant_id=rpt.tenant_id, result="FAILED", detail={"to": body.to, "error": e.message})
        raise
    audit.log("report.send", target_type="report", target_id=rpt.id, target_name=rpt.title, tenant_id=rpt.tenant_id,
              detail={"to": to})
    return ok({"sent_to": to, "sent_at": rpt.sent_at})


@router.delete("/{rid}", summary="删除报表")
def delete_report(rid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  audit: Auditor = Depends(get_auditor), _: CurrentUser = Depends(require_perm("report:delete"))):
    rpt = _get(db, scope, rid)
    title, tid = rpt.title, rpt.tenant_id
    db.delete(rpt)
    audit.log("report.delete", target_type="report", target_id=rid, target_name=title, tenant_id=tid)
    return ok()
