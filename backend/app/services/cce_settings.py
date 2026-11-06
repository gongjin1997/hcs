from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import InspectionTask, SysSetting
from app.schemas.cce import CceSettings
from app.services import collect_switch

TASK_CODE = "cce_cluster_health"


def key(tenant_id: int) -> str:
    return f"cce_settings:{tenant_id}"


def effective(db: Session, tenant_id: int | None) -> CceSettings:
    row = db.scalar(select(SysSetting).where(SysSetting.key == key(tenant_id))
                    .execution_options(populate_existing=True)) if tenant_id is not None else None
    return CceSettings.model_validate(row.value if row is not None else {})


def status(db: Session, tenant_id: int) -> dict:
    task = db.get(InspectionTask, TASK_CODE)
    settings = effective(db, tenant_id)
    master = collect_switch.load(db).enabled(tenant_id, "cce_cluster")
    return {"tenant_id": tenant_id, "settings": settings.model_dump(), "master_enabled": master,
            "task_enabled": bool(task and task.enabled), "interval_sec": task.interval_sec if task else 120,
            "effective_enabled": bool(settings.enabled and master and task and task.enabled)}


def save(db: Session, tenant_id: int, value: CceSettings, operator: str) -> None:
    row = db.get(SysSetting, key(tenant_id))
    if row is None:
        row = SysSetting(key=key(tenant_id))
        db.add(row)
    row.value, row.updated_by = value.model_dump(), operator
    db.flush()
