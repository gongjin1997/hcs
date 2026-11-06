from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_anon_auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, ok
from app.core.security import verify_mail_action
from app.core.xlsx import filename_stamp, xlsx_response
from app.models import AlarmEvent
from app.models.base import utcnow
from app.reports.alarm_excel import alarm_workbook
from app.schemas.alarm import AlarmDetailOut, AlarmOut, AlarmStats
from app.schemas.common import NoteIn
from app.services import alarm_service

from .auth import display_name

router = APIRouter(prefix="/alarms", tags=["告警"])


class BatchAckIn(BaseModel):
    ids: list[int]
    note: str | None = None


def _scoped(db: Session, scope: Scope, alarm_id: int) -> AlarmEvent:
    alarm = alarm_service.get_alarm(db, alarm_id)
    scope.ensure(alarm.tenant_id, "告警")
    return alarm


def _audit(audit: Auditor, action: str, a: AlarmEvent, note: str | None = None, **detail: object) -> None:
    audit.log(action, target_type="alarm", target_id=a.id, target_name=f"{a.resource_name} · {a.title}",
              tenant_id=a.tenant_id, detail={"rule_id": a.rule_id, "severity": a.severity,
                                             **({"note": note} if note else {}), **detail})


@router.get("", response_model=ApiResponse[Page[AlarmOut]], summary="告警列表（按租户范围过滤）")
def list_alarms(status_group: str = "active", severity: list[str] | None = Query(None), service: str | None = None,
                resource_type: str | None = None, resource_ref: int | None = None, keyword: str | None = None,
                page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                _: CurrentUser = Depends(require_perm("alarm:view"))):
    items, total = alarm_service.query_alarms(db, status_group=status_group, severities=severity, service=service,
                                              resource_type=resource_type, resource_ref=resource_ref,
                                              keyword=keyword, page=page, page_size=page_size,
                                              tenant_ids=scope.tenant_ids)
    return ok(Page(items=[AlarmOut.model_validate(a) for a in items], total=total, page=page, page_size=page_size))


@router.get("/export", summary="导出告警 Excel（当前筛选全量）")
def export_alarms(status_group: str = "active", severity: list[str] | None = Query(None), service: str | None = None,
                  resource_type: str | None = None, resource_ref: int | None = None, keyword: str | None = None,
                  db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  _: CurrentUser = Depends(require_perm("alarm:view"))):
    items, _total = alarm_service.query_alarms(
        db, status_group=status_group, severities=severity, service=service, resource_type=resource_type,
        resource_ref=resource_ref, keyword=keyword, page_size=None, tenant_ids=scope.tenant_ids,
    )
    return xlsx_response(alarm_workbook(db, items), f"告警列表_{filename_stamp(utcnow())}.xlsx")


@router.get("/stats", response_model=ApiResponse[AlarmStats], summary="活动告警统计")
def alarm_stats(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                _: CurrentUser = Depends(require_perm("alarm:view"))):
    return ok(alarm_service.stats(db, scope.tenant_ids))


@router.post("/batch-ack", summary="批量认领（只处理当前范围内、告警中的告警）")
def batch_ack(body: BatchAckIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
              audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("alarm:handle"))):
    done = alarm_service.batch_ack(db, body.ids, display_name(db, cu), body.note, tenant_ids=scope.tenant_ids)
    if done:
        audit.log("alarm.batch_ack", target_type="alarm", target_id=",".join(str(a.id) for a in done)[:64],
                  target_name=f"{len(done)} 条告警", tenant_id=scope.single,
                  detail={"ids": [a.id for a in done], "note": body.note})
    return ok({"acked": len(done)})


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmDetailOut], summary="告警详情（含处理时间线、通知记录）")
def get_alarm(alarm_id: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
              _: CurrentUser = Depends(require_perm("alarm:view"))):
    return ok(alarm_service.alarm_detail(db, _scoped(db, scope, alarm_id)))


@router.post("/{alarm_id}/ack", response_model=ApiResponse[AlarmOut], summary="认领告警")
def ack(alarm_id: int, body: NoteIn | None = None, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
        audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("alarm:handle"))):
    _scoped(db, scope, alarm_id)
    note = body.note if body else None
    alarm = alarm_service.ack(db, alarm_id, display_name(db, cu), note)
    _audit(audit, "alarm.ack", alarm, note)
    return ok(AlarmOut.model_validate(alarm))


@router.post("/{alarm_id}/close", response_model=ApiResponse[AlarmOut], summary="关闭告警")
def close(alarm_id: int, body: NoteIn | None = None, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
          audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("alarm:handle"))):
    _scoped(db, scope, alarm_id)
    note = body.note if body else None
    alarm = alarm_service.close(db, alarm_id, display_name(db, cu), note)
    _audit(audit, "alarm.close", alarm, note)
    return ok(AlarmOut.model_validate(alarm))


# ---- 邮件快捷操作（签名链接，免登录，H5 落地页调用）----
mail_router = APIRouter(prefix="/mail-actions", tags=["邮件快捷操作"])


def _verify(token: str) -> dict:
    info = verify_mail_action(token)
    if info is None:
        raise BizError(ErrorCode.UNAUTHORIZED, "链接无效或已过期", 401)
    return info


def _mail_alarm(db: Session, info: dict) -> AlarmEvent:
    alarm = alarm_service.get_alarm(db, info["alarm_id"])
    if info.get("reopen_count", 0) != alarm.reopen_count:
        raise BizError(ErrorCode.CONFLICT, "告警已重新触发，请使用本次通知中的链接", 409)
    return alarm


@mail_router.get("/{token}", summary="解析邮件操作链接")
def mail_action_info(token: str, db: Session = Depends(get_db)):
    info = _verify(token)
    alarm = _mail_alarm(db, info)
    return ok({**info, "alarm": AlarmOut.model_validate(alarm)})


@mail_router.post("/{token}", summary="执行邮件操作（认领）")
def mail_action_do(token: str, db: Session = Depends(get_db), audit: Auditor = Depends(get_anon_auditor)):
    info = _verify(token)
    if info["action"] != "ack":
        raise BizError(ErrorCode.BAD_REQUEST, "该操作需登录控制台完成", 400)
    alarm = _mail_alarm(db, info)
    if alarm.status == "ACKED":
        return ok(AlarmOut.model_validate(alarm))
    operator = info["recipient"] or "邮件用户"
    try:
        alarm = alarm_service.ack(db, info["alarm_id"], operator, "通过邮件链接认领",
                                  expected_reopen_count=info.get("reopen_count", 0))
    except BizError as exc:
        if exc.code == ErrorCode.CONFLICT:
            alarm = _mail_alarm(db, info)
            if alarm.status == "ACKED":
                return ok(AlarmOut.model_validate(alarm))
        raise
    audit.log("alarm.mail_ack", target_type="alarm", target_id=alarm.id,
              target_name=f"{alarm.resource_name} · {alarm.title}", tenant_id=alarm.tenant_id, operator=operator,
              username=None, detail={"rule_id": alarm.rule_id, "via": "邮件链接"})
    return ok(AlarmOut.model_validate(alarm))
