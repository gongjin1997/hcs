"""CCE 管理面健康采集与只读快照；GET 页面不向云端发请求。"""
import hashlib
import logging
from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext, log_collection_failure
from app.collectors.scope import ledger_subject
from app.core.deps import Scope
from app.core.response import not_found
from app.hcs import HcsError
from app.hcs.cce_inventory import cluster_info, clusters, freshness, node_info, nodepools, nodes, pool_info, timestamp
from app.k8s.kube import KubeApi
from app.k8s.native_capabilities import collect_resource_metrics, resolve_pod_owners
from app.models import CceClusterSnapshot, HcsAccount, HcsProject, Resource
from app.models.base import utcnow
from app.services import cce_settings, collect_switch
from app.services.catalog import LEDGER_SCOPE_DIMS

log = logging.getLogger(__name__)


def snapshot_key(tenant_id: int, project_ref: int, cid: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{project_ref}:{cid}".encode()).hexdigest()


class CceHealthCollector:
    code, service, name = cce_settings.TASK_CODE, "cce", "CCE 节点健康与原生指标"

    def collect(self, ctx: RunContext) -> list[Observation]:  # noqa: C901  # T10
        db: Session = ctx.db
        if db is None or ctx.tenant_id is None or ctx.project_ref is None:
            ctx.stats["cce_skipped"] = "scope_missing"
            return []
        config = cce_settings.effective(db, ctx.tenant_id)
        ctx.params["_cce_settings"] = config.model_dump()
        if not config.enabled or not collect_switch.load(db).enabled(ctx.tenant_id, "cce_cluster"):
            ctx.stats["cce_skipped"] = "disabled"
            return []
        now = utcnow()
        existing = {row.cluster_id: row for row in db.scalars(select(CceClusterSnapshot).where(
            CceClusterSnapshot.tenant_id == ctx.tenant_id, CceClusterSnapshot.project_ref == ctx.project_ref))}
        try:
            listed = [cluster_info(item) for item in clusters(ctx.client)]
        except HcsError as exc:
            for cached in existing.values():
                previous = cached.sources.get("cluster", {})
                cached.sources = {**cached.sources, "cluster": {**previous, "state": "error",
                               "attempted_at": now.isoformat(), "reason": "upstream_error"}}
            ctx.errors.append(f"CCE 集群清单查询失败（{exc.status}），已保留旧快照")
            log_collection_failure(log, ctx, exc, cloud_service="cce", phase="clusters")
            return []
        seen = set()
        scope = ctx.scope("cce_cluster")
        ledger_fields: dict[str, dict] = {}
        if scope.dims.intersection(LEDGER_SCOPE_DIMS):
            # 只读当前租户项目、本次清单中尚可能纳入范围的集群；不按节点或集群逐条查库。
            ids = [info["cluster_id"] for info in listed
                   if scope.keep({"cluster": (info["cluster_id"], info["name"])})]
            for start in range(0, len(ids), 500):
                rows = db.execute(select(Resource.resource_id, Resource.env_type, Resource.business,
                                         Resource.owner, Resource.critical).where(
                    Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
                    Resource.resource_type == "cce_cluster", Resource.lifecycle == "ACTIVE",
                    Resource.resource_id.in_(ids[start:start + 500])))
                ledger_fields.update((row.resource_id, ledger_subject(row.env_type, row.business, row.owner,
                                                                     row.critical)) for row in rows)
        counts = {"clusters": len(listed), "nodes": 0, "nodepools": 0, "source_failures": 0}
        for info in listed:
            cid = info["cluster_id"]
            seen.add(cid)          # 范围外的集群仍然存在，不能被标记为缺失
            if not scope.keep({"cluster": (cid, info["name"]), **ledger_fields.get(cid, ledger_subject())}):
                counts["clusters"] -= 1
                continue           # 采集范围：范围外的集群不查询节点、节点池与原生指标，旧快照原样保留
            row = existing.get(cid)
            if row is None:
                row = CceClusterSnapshot(key=snapshot_key(ctx.tenant_id, ctx.project_ref, cid),
                    tenant_id=ctx.tenant_id, project_ref=ctx.project_ref, cluster_id=cid,
                    cluster={}, nodes=[], nodepools=[], native={}, sources={}, last_seen=now, missing=False)
                db.add(row)
            row.cluster, row.last_seen, row.missing = info, now, False
            sources = {**row.sources, "cluster": {"state": "available", "attempted_at": now.isoformat(),
                                                 "succeeded_at": now.isoformat()}}
            for source, fetch, normalize in (("nodes", nodes, lambda item: node_info(item, now)),
                                             ("nodepools", nodepools, pool_info)):
                previous = sources.get(source, {})
                try:
                    raw_items = fetch(ctx.client, cid)
                    completed = utcnow()
                    values = [node_info(item, completed) if source == "nodes" else normalize(item) for item in raw_items]
                    setattr(row, source, values)
                    if source == "nodes":
                        total = info["capacity"]["total_nodes"]
                        active = info["capacity"]["active_nodes"]
                        info["capacity"].update(observed_nodes=len(values),
                            nodes_consistent=None if total is None else total == len(values),
                            active_count_consistent=None if total is None or active is None else active <= total)
                        row.cluster = {**info}
                    sources[source] = {"state": "available", "attempted_at": now.isoformat(),
                                       "succeeded_at": completed.isoformat()}
                    counts[source] += len(values)
                except HcsError as exc:
                    sources[source] = {"state": "forbidden" if exc.status == 403 else
                                                "unsupported" if exc.status == 404 else "error",
                                       "attempted_at": now.isoformat(), "succeeded_at": previous.get("succeeded_at"),
                                       "reason": "invalid_response" if exc.code.startswith("LOCAL.") else "upstream_error"}
                    counts["source_failures"] += 1
                    # Node health is the task's required source. Optional pool capability absence is informational.
                    if source == "nodes" or exc.status not in (403, 404):
                        ctx.errors.append(f"CCE {source} 查询失败（{exc.status}），已保留旧快照")
                        log_collection_failure(log, ctx, exc, cloud_service="cce", phase=source)
                    else:
                        log.info("cce_capability_unavailable", extra={"event": "cce_capability_unavailable",
                                 "phase": source, "reason": sources[source]["state"],
                                 "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref})
            row.sources = sources
            row.native = self._native(ctx, cid, config, utcnow()) if info["phase"] == "Available" else {
                "metrics": {"status": "disabled", "reason": "cluster_unavailable"},
                "owners": {"status": "disabled", "reason": "cluster_unavailable"}}
        # Only a validated complete cluster list can mark absence; do not delete historical snapshots.
        for cid, row in existing.items():
            if cid not in seen:
                row.missing = True
        ctx.stats["cce_health"] = counts
        log.info("cce_health_completed", extra={"event": "cce_health_completed", "tenant_id": ctx.tenant_id,
                 "project_ref": ctx.project_ref, **counts})
        db.flush()
        return []

    @staticmethod
    def _native(ctx: RunContext, cid: str, config, now) -> dict:
        # Capability helpers are deliberately opt-in; no requests while disabled.
        kube = KubeApi(ctx.client, cid)
        metrics = collect_resource_metrics(kube, enabled=config.resource_metrics, now=now)
        owners = {"status": "disabled", "reason": "disabled", "items": [], "sources": {}}
        if config.resolve_owners:
            try:
                owners = resolve_pod_owners(kube, kube.pods(), enabled=True)
            except HcsError as exc:
                owners = {"status": "forbidden" if exc.status == 403 else "unsupported" if exc.status == 404 else "error",
                          "reason": "pod_list_unavailable", "items": [], "sources": {}}
                log.info("cce_capability_unavailable", extra={"event": "cce_capability_unavailable",
                    "phase": "owners", "reason": owners["status"], "tenant_id": ctx.tenant_id,
                    "project_ref": ctx.project_ref})
        return {"metrics": metrics, "owners": owners, "collected_at": now.isoformat()}


def _query(scope: Scope, tenant_id: int | None, project_ref: int | None, cluster_id: str | None):
    q = scope.apply(select(CceClusterSnapshot), CceClusterSnapshot.tenant_id)
    if tenant_id is not None:
        scope.ensure(tenant_id, "CCE 集群")
        q = q.where(CceClusterSnapshot.tenant_id == tenant_id)
    if project_ref is not None:
        q = q.where(CceClusterSnapshot.project_ref == project_ref)
    if cluster_id:
        q = q.where(CceClusterSnapshot.cluster_id == cluster_id)
    return q


def serialize(db: Session, row: CceClusterSnapshot, *, detail: bool = True,
              names: tuple[str, str] | None = None) -> dict:
    now = utcnow()
    if names is None:
        tenant, project = db.get(HcsAccount, row.tenant_id), db.get(HcsProject, row.project_ref)
        names = ((tenant.short_name or tenant.name) if tenant else "",
                 (project.project_name or project.project_id) if project else "")
    state = freshness(row.last_seen.isoformat(), now)
    if row.sources.get("cluster", {}).get("state") != "available":
        state = "refresh_failed"
    sources = deepcopy(row.sources)
    for source in sources.values():
        source["freshness"] = freshness(source.get("succeeded_at"), now)
    result: dict = {"key": row.key, "tenant_id": row.tenant_id, "tenant_name": names[0],
              "project_ref": row.project_ref, "project_name": names[1], "cluster_id": row.cluster_id,
              "cluster": row.cluster, "last_seen": row.last_seen.isoformat(), "freshness": state,
              "missing": row.missing, "sources": sources, "nodes": [], "nodepools": [], "native": {}}
    if not detail:
        return result
    server_ids = sorted({n["server_id"] for n in row.nodes if n.get("server_id")})
    servers = {}
    for start in range(0, len(server_ids), 500):
        servers.update({r.resource_id: r.id for r in db.scalars(select(Resource).where(
            Resource.tenant_id == row.tenant_id, Resource.project_ref == row.project_ref,
            Resource.resource_type == "ecs_server", Resource.lifecycle == "ACTIVE",
            Resource.resource_id.in_(server_ids[start:start + 500])))})
    for raw in row.nodes:
        node = {**raw, "ecs_resource_ref": servers.get(raw.get("server_id"))}
        node["freshness"] = freshness(node.get("last_probe_at"), now)
        if (row.missing or state != "fresh" or sources.get("nodes", {}).get("state") != "available"
                or sources.get("nodes", {}).get("freshness") != "fresh"
                or node["freshness"] != "fresh"):
            node["health"] = "unknown"
        result["nodes"].append(node)
    result["nodepools"] = row.nodepools
    result["native"] = deepcopy(row.native)
    result["native"]["freshness"] = state
    metrics = result["native"].get("metrics", {})
    for kind in ("pods", "nodes"):
        source = metrics.get(kind, {})
        for sample in source.get("items", []):
            at = timestamp(sample.get("timestamp"))
            age = (now - at).total_seconds() if at else None
            sample["age_seconds"] = age
            if row.missing or age is None or not -20 <= age <= 300 or state != "fresh":
                sample.update(fresh=False, status="partial", reason="sample_stale")
                if source.get("status") == "ok":
                    source.update(status="partial", reason="metrics_samples_partial")
                if metrics.get("status") == "ok":
                    metrics.update(status="partial", reason="metrics_partial")
    return result


def list_clusters(db: Session, scope: Scope, *, tenant_id: int | None = None, project_ref: int | None = None,
                  cluster_id: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    q = _query(scope, tenant_id, project_ref, cluster_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(db.scalars(q.order_by(CceClusterSnapshot.tenant_id, CceClusterSnapshot.project_ref,
                                      CceClusterSnapshot.cluster_id).offset((page - 1) * page_size).limit(page_size)))
    tenants = {t.id: t.short_name or t.name for t in db.scalars(select(HcsAccount).where(
        HcsAccount.id.in_({row.tenant_id for row in rows})))}
    projects = {p.id: p.project_name or p.project_id for p in db.scalars(select(HcsProject).where(
        HcsProject.id.in_({row.project_ref for row in rows})))}
    return {"items": [serialize(db, row, detail=False, names=(tenants.get(row.tenant_id, ""),
                                                            projects.get(row.project_ref, ""))) for row in rows],
            "total": total,
            "page": page, "page_size": page_size}


def cluster_detail(db: Session, scope: Scope, key: str) -> dict:
    row = db.scalar(scope.apply(select(CceClusterSnapshot).where(CceClusterSnapshot.key == key),
                                CceClusterSnapshot.tenant_id))
    if row is None:
        raise not_found("CCE 集群快照")
    return serialize(db, row)
