"""手工执行一次任务（按租户逐个执行；可用 --tenant 指定租户代码）：
python -m app.workers.run_once ecs_status                    # 巡检任务（全部启用租户）
python -m app.workers.run_once cce_pod_events --tenant tenant-a
python -m app.workers.run_once ledger_sync                   # 台账同步
python -m app.workers.run_once data_cleanup                  # 数据清理（按“系统设置 → 数据保留”删除过期数据）
python -m app.workers.run_once report DAILY [--tenant tenant-a]   # 生成上一周期报表（DAILY / WEEKLY / MONTHLY / LEDGER）
python -m app.workers.run_once check [--tenant tenant-a]     # 租户连通性 / 能力探测（CES、Prometheus、CCE API）
"""
import argparse
import logging
import sys
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select

from app.collectors.registry import TASKS
from app.core.db import SessionLocal
from app.core.logging import bind_log_context, log_context, setup_logging
from app.ledger.sync import LEDGER_TASK
from app.models import HcsAccount
from app.reports.builder import KINDS
from app.reports.service import generate
from app.services.cleanup import CLEANUP_TASK
from app.services.task_execution import enqueue
from app.services.tenant_check import check_tenant

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging(service="run_once")
    with log_context(operation_id=uuid4().hex, trigger="MANUAL"):
        started = perf_counter()
        log.info("manual_operation_started", extra={"event": "manual_operation_started"})
        try:
            _execute()
        except SystemExit as exc:
            log.log(logging.WARNING if exc.code else logging.DEBUG, "manual_operation_exited",
                    extra={"event": "manual_operation_exited", "exit_code": exc.code,
                           "cost_ms": round((perf_counter() - started) * 1000, 2)})
            raise
        except Exception as exc:
            log.error("manual_operation_failed", extra={"event": "manual_operation_failed",
                      "exception_type": type(exc).__name__, "cost_ms": round((perf_counter() - started) * 1000, 2)},
                      exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            raise
        log.info("manual_operation_finished", extra={"event": "manual_operation_finished",
                                                     "cost_ms": round((perf_counter() - started) * 1000, 2)})


def _execute() -> None:
    p = argparse.ArgumentParser(prog="python -m app.workers.run_once")
    p.add_argument("task", help=f"{'|'.join([*TASKS, LEDGER_TASK, CLEANUP_TASK])}|report|check")
    p.add_argument("kind", nargs="?", help=f"报表类型：{'|'.join(KINDS)}")
    p.add_argument("--tenant", help="租户代码（默认全部启用租户）")
    a = p.parse_args()
    bind_log_context(task=a.task if a.task in {*TASKS, LEDGER_TASK, CLEANUP_TASK, "check", "report"} else "invalid")
    with SessionLocal() as db:
        tenant = None
        if a.tenant:
            tenant = db.scalar(select(HcsAccount).where(HcsAccount.code == a.tenant))
            if tenant is None:
                print(f"租户不存在：{a.tenant}")
                sys.exit(2)
            bind_log_context(tenant_id=tenant.id, tenant_code=tenant.code)
        if a.task == "report":
            if a.kind not in KINDS:
                p.error(f"报表类型应为 {'|'.join(KINDS)}")
            rpt = generate(db, a.kind, created_by="cli", tenant_id=tenant.id if tenant else None)
            print(f"report #{rpt.id}: {rpt.title} {rpt.summary}")
            return
        if a.task == "check":
            tenants = [tenant] if tenant else list(db.scalars(select(HcsAccount).where(HcsAccount.enabled.is_(True))))
            for t in tenants:
                started = perf_counter()
                with log_context(tenant_id=t.id, tenant_code=t.code, task="tenant_check"):
                    res = check_tenant(db, t)
                    failed = sum(1 for check in res["checks"] if not check["ok"])
                    log.log(logging.WARNING if failed else logging.INFO, "tenant_check_finished", extra={
                        "event": "tenant_check_finished", "failed_count": failed, "check_count": len(res["checks"]),
                        "cost_ms": round((perf_counter() - started) * 1000, 2)})
                print(f"[{t.code}] ok={res['ok']} metrics_source={res['metrics_source']}")
                for c in res["checks"]:
                    print(f"  {'✔' if c['ok'] else '✘'} {c['name']}")
            return
        if a.task not in TASKS and a.task not in (LEDGER_TASK, CLEANUP_TASK):
            p.error(f"未知任务：{a.task}")
        result = enqueue(db, a.task, trigger="MANUAL", requested_by="cli",
                         tenant_ids=[tenant.id] if tenant else None)
        for run in result["runs"]:
            print(f"{run['task_code']}[tenant={run['tenant_id']}]: {run['status']} "
                  f"execution_id={run['execution_id']} run_id={run['run_id']}")
        if not result["runs"]:
            print("当前范围内没有可执行目标")
        else:
            print("已加入队列，由 scheduler 执行；可在巡检任务页面查看进度。")


if __name__ == "__main__":
    main()
