"""手工执行一次任务（按租户逐个执行；可用 --tenant 指定租户代码）：
python -m app.workers.run_once ecs_status                    # 巡检任务（全部启用租户）
python -m app.workers.run_once cce_pod_events --tenant tenant-a
python -m app.workers.run_once ledger_sync                   # 台账同步
python -m app.workers.run_once report DAILY [--tenant tenant-a]   # 生成上一周期报表（DAILY / WEEKLY / MONTHLY / LEDGER）
python -m app.workers.run_once check [--tenant tenant-a]     # 租户连通性 / 能力探测（CES、Prometheus、CCE API）
"""
import argparse
import sys

from sqlalchemy import select

from app.collectors.registry import TASKS
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.ledger.sync import LEDGER_TASK
from app.models import HcsAccount
from app.reports.builder import KINDS
from app.reports.service import generate
from app.services.pipeline import run_all
from app.services.tenant_check import check_tenant


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(prog="python -m app.workers.run_once")
    p.add_argument("task", help=f"{'|'.join([*TASKS, LEDGER_TASK])}|report|check")
    p.add_argument("kind", nargs="?", help=f"报表类型：{'|'.join(KINDS)}")
    p.add_argument("--tenant", help="租户代码（默认全部启用租户）")
    a = p.parse_args()
    with SessionLocal() as db:
        tenant = None
        if a.tenant:
            tenant = db.scalar(select(HcsAccount).where(HcsAccount.code == a.tenant))
            if tenant is None:
                print(f"租户不存在：{a.tenant}")
                sys.exit(2)
        if a.task == "report":
            if a.kind not in KINDS:
                p.error(f"报表类型应为 {'|'.join(KINDS)}")
            rpt = generate(db, a.kind, created_by="cli", tenant_id=tenant.id if tenant else None)
            print(f"report #{rpt.id}: {rpt.title} {rpt.summary}")
            return
        if a.task == "check":
            tenants = [tenant] if tenant else list(db.scalars(select(HcsAccount).where(HcsAccount.enabled.is_(True))))
            for t in tenants:
                res = check_tenant(db, t)
                print(f"[{t.code}] ok={res['ok']} metrics_source={res['metrics_source']}")
                for c in res["checks"]:
                    print(f"  {'✔' if c['ok'] else '✘'} {c['name']}: {c.get('detail') or c.get('message') or ''}")
            return
        if a.task not in TASKS and a.task != LEDGER_TASK:
            p.error(f"未知任务：{a.task}")
        for run in run_all(db, a.task, trigger="MANUAL", tenant_ids=[tenant.id] if tenant else None):
            print(f"{run.task_code}[tenant={run.tenant_id}]: {run.status} resources={run.resource_total} "
                  f"abnormal={run.abnormal_total} api_calls={run.api_calls} {run.error_summary or ''}")


if __name__ == "__main__":
    main()
