"""Read-only Pod snapshot validity; history and alarm state are never changed here."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import InspectionTask, PodState
from app.schemas.pod import PodFreshnessOut, PodFreshnessStatus
from app.services.inspection_evidence import ScopeContext, inspection_ttl

TASK_CODE = "cce_pod_events"
KNOWN_PHASES = {"Pending", "Running", "Succeeded", "Failed"}


def freshness_ttl(db: Session, now: datetime) -> int | None:
    task = db.get(InspectionTask, TASK_CODE)
    ctx = ScopeContext(account="", project_id="", tenant_id=None, project_ref=None, params={"_task_code": TASK_CODE})
    try:
        return inspection_ttl(db, ctx, task, None, now)
    except (ValueError, KeyError):
        # An invalid schedule cannot establish how long a snapshot remains current.
        return None


def snapshot_freshness(state: PodState, now: datetime, ttl: int | None) -> PodFreshnessOut:
    at = state.last_seen
    observed = at if isinstance(at, datetime) and at.tzinfo is not None else None
    elapsed = (now - observed).total_seconds() if observed is not None else None
    # Match inspection_health's clock-skew allowance; future timestamps are not fresh evidence.
    age = max(0, int(elapsed)) if elapsed is not None and elapsed >= -30 else None
    status: PodFreshnessStatus = "UNKNOWN"
    if state.deleted:
        status = "DELETED"
    elif (age is not None and ttl is not None and state.phase in KNOWN_PHASES
          and (state.data or {}).get("status_observed") is not False):
        status = "STALE" if elapsed is not None and elapsed > ttl else "FRESH"
    return PodFreshnessOut(status=status, last_observed_at=observed, age_sec=age, stale_after_sec=ttl)


def summarize_pods(db: Session, states: list[PodState], now: datetime) -> dict:
    ttl = freshness_ttl(db, now)
    summary = {"total": 0, "fresh": 0, "stale": 0, "unknown": 0,
               "ready": 0, "not_ready": 0, "crashloop": 0, "restarts": 0}
    groups: dict[tuple[int | None, int | None, str], dict] = {}
    for state in states:
        if state.deleted:
            continue
        key = (state.tenant_id, state.project_ref, state.cluster_id)
        group = groups.setdefault(key, {"tenant_id": state.tenant_id, "project_ref": state.project_ref,
                                       "cluster_id": state.cluster_id, "cluster_name": state.cluster_name,
                                       **dict.fromkeys(summary, 0), "last_observed_at": None})
        snapshot = snapshot_freshness(state, now, ttl)
        category = snapshot.status.lower()
        group["total"] += 1
        group[category] += 1
        if snapshot.status in ("FRESH", "STALE"):
            at = snapshot.last_observed_at
            if at is not None and (group["last_observed_at"] is None or at > group["last_observed_at"]):
                group["last_observed_at"] = at
        if snapshot.status == "FRESH":
            group["ready"] += int(state.ready)
            group["not_ready"] += int(not state.ready and state.phase != "Succeeded")
            group["crashloop"] += int(state.status_reason == "CrashLoopBackOff")
            group["restarts"] += state.restarts
    scopes = []
    statuses: dict[str, PodFreshnessStatus] = {"fresh": "FRESH", "stale": "STALE", "unknown": "UNKNOWN"}
    for _key, group in sorted(groups.items(), key=lambda item: (item[0][0] or 0, item[0][1] or 0, item[0][2])):
        for field in summary:
            summary[field] += group[field]
        at = group.pop("last_observed_at")
        status: PodFreshnessStatus = "PARTIAL"
        for name, value in statuses.items():
            if group[name] == group["total"]:
                status = value
                break
        group["freshness"] = PodFreshnessOut(status=status, last_observed_at=at,
                                             age_sec=max(0, int((now - at).total_seconds())) if at else None,
                                             stale_after_sec=ttl)
        scopes.append(group)
    return {**summary, "clusters": len(scopes), "scopes": scopes}
