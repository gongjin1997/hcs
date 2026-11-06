"""台账配置容量汇总：部分未知不冒充全量，全部未知不显示零容量。"""
from collections.abc import Sequence

from app.metrics.dcs_samples import finite_number
from app.models import Resource


def capacity_totals(resources: Sequence[Resource]) -> dict:
    ecs = [row for row in resources if row.resource_type == "ecs_server"]
    volumes = [row for row in resources if row.resource_type == "evs_volume"]
    totals: dict = {"coverage": {}}
    for key, rows in (("vcpus", ecs), ("memory_gb", ecs), ("storage_gb", volumes)):
        values = [number for row in rows if (number := finite_number(getattr(row, key))) is not None]
        totals["coverage"][key] = {"known": len(values), "total": len(rows)}
        total = finite_number(sum(values)) if values else None if rows else 0
        totals[key] = (int(total) if key == "vcpus" else round(total, 1)) if total is not None else None
    return totals
