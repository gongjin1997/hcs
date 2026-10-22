"""系统管理 - 巡检任务：任务列表与最近运行记录（按租户范围过滤）、立即执行。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.core.audit import Auditor, get_auditor
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import BizError, ErrorCode, not_found, ok
from app.metrics import CAPABILITIES, SOURCES
from app.models import InspectionRun, InspectionTask
from app.services import pipeline
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/system", tags=["巡检任务"])


@router.get("/info", summary="平台信息（适配的 HCS 版本、指标来源说明）")
def info(_: object = Depends(require_perm("task:view", "tenant:view", any_of=True))):
    s = get_settings()
    return ok({"version": __version__, "hcs_version": s.hcs_version, "env": s.env, "metrics_sources": SOURCES,
               "capabilities": CAPABILITIES, "mock": bool(s.hcs_mock_url) and s.env != "prod",
               "notes": {
                   "ces": "HCS 8.5.1 的云监控 CES 为可选组件：选“自动探测”时首次巡检 / 连通性测试会调用 CES 接口，"
                          "未部署（404 / 域名无法解析）时自动降级为 Prometheus 或只巡检状态",
                   "prometheus": "可选：租户已有 Prometheus（node_exporter）时填写地址，作为 CES 的替代或补充",
                   "es": "不需要 Elasticsearch：事件 / 告警 / 台账均为结构化数据，PostgreSQL + 索引 + 定期清理足够"}})


@router.get("/tasks", summary="巡检任务与最近运行（每个租户一条运行记录）")
def tasks(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
          _: object = Depends(require_perm("task:view"))):
    tm = tenant_map(db)
    out = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        q = scope.apply(select(InspectionRun).where(InspectionRun.task_code == t.code), InspectionRun.tenant_id,
                        include_global=True)
        runs = db.scalars(q.order_by(InspectionRun.started_at.desc()).limit(12)).all()
        out.append({"code": t.code, "name": t.name, "service": t.service, "interval_sec": t.interval_sec,
                    "enabled": t.enabled, "params": t.params,
                    "runs": [{"id": r.id, "tenant_id": r.tenant_id,
                              "tenant_name": (tm[r.tenant_id].short_name or tm[r.tenant_id].name)
                              if r.tenant_id in tm else None,
                              "trigger_type": r.trigger_type, "started_at": r.started_at,
                              "finished_at": r.finished_at, "status": r.status, "resource_total": r.resource_total,
                              "abnormal_total": r.abnormal_total, "api_calls": r.api_calls,
                              "error_summary": r.error_summary, "stats": r.stats or {}} for r in runs]})
    return ok(out)


@router.post("/tasks/{code}/run", summary="立即执行（按当前租户范围逐个租户执行）")
def run_task(code: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("task:run"))):
    task = db.get(InspectionTask, code)
    if task is None:
        raise not_found("巡检任务")
    runs = pipeline.run_all(db, code, trigger="MANUAL", tenant_ids=scope.tenant_ids)
    tm = tenant_map(db)
    items = [{"run_id": r.id, "tenant_id": r.tenant_id,
              "tenant_name": (tm[r.tenant_id].short_name or tm[r.tenant_id].name) if r.tenant_id in tm else None,
              "status": r.status, "resource_total": r.resource_total, "abnormal_total": r.abnormal_total,
              "api_calls": r.api_calls, "error_summary": r.error_summary} for r in runs]
    failed = [i for i in items if i["status"] == "FAILED"]
    audit.log("task.run", target_type="task", target_id=code, target_name=task.name,
              tenant_id=scope.single, result="FAILED" if runs and len(failed) == len(runs) else "SUCCESS",
              detail={"runs": [{"tenant": i["tenant_name"], "status": i["status"]} for i in items]})
    if runs and len(failed) == len(runs):
        raise BizError(ErrorCode.HCS_UPSTREAM, "执行失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['error_summary']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "status": "PARTIAL" if failed else "SUCCESS"})
