from fastapi import APIRouter

from .v1 import (
    alarms,
    auth,
    dashboard,
    notify,
    pod_events,
    reports,
    resources,
    rules,
    silences,
    stream,
    system,
    tenants,
    users,
)

api_router = APIRouter(prefix="/api/v1")
for r in (auth.router, dashboard.router, silences.quick_router, alarms.router, alarms.mail_router, stream.router,
          silences.router, resources.router, pod_events.router, rules.router, notify.router, reports.router,
          system.router, tenants.router, users.router):
    api_router.include_router(r)
