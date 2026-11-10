from fastapi import APIRouter

from .v1 import (
    alarms,
    auth,
    cce,
    collection_policies,
    dashboard,
    dcs,
    diagnostics,
    ecs,
    ecs_quotas,
    executions,
    gaussdb,
    guides,
    job_reports,
    manageone,
    monitoring,
    notify,
    pod_events,
    reports,
    resources,
    rules,
    silences,
    sms,
    stream,
    system,
    tenants,
    users,
)

api_router = APIRouter(prefix="/api/v1")
for r in (auth.router, dashboard.router, silences.quick_router, alarms.router, alarms.mail_router, stream.router,
          silences.router, resources.router, pod_events.router, rules.router, notify.router, reports.router,
          sms.router, system.router, tenants.router, users.router, job_reports.router, guides.router,
          gaussdb.router, dcs.router, ecs.router, ecs_quotas.router, collection_policies.router, diagnostics.router,
          monitoring.router, cce.router, manageone.router, executions.router):
    api_router.include_router(r)
