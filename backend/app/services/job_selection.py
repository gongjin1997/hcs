"""DataArts 监控名单：复用 collect_scope，按项目/工作空间保存显式选择。

sys_setting 的独立项目键避免通用 rules=[] 的“全部”语义吞掉 selected/jobs=[]。
未配置的工作空间沿用原采集范围；已配置但无效的名单只会收紧，不退回全量。
仅发现作业定义（GET）；不启停云任务，不写采集开关，不在保存时请求云端。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizError, ErrorCode
from app.hcs import HcsError, client_for
from app.hcs.pager import response_items, response_total
from app.models import HcsAccount, HcsProject, SysSetting
from app.models.base import utcnow
from app.schemas.job_selection import JobSelectionIn

KEY_PREFIX = "job_selection"
TYPE_CODES = {"dgc_realtime_job": "realtime", "dgc_batch_job": "batch"}
JOB_TYPES = {"dgc_realtime_job": "REAL_TIME", "dgc_batch_job": "BATCH"}


def setting_key(tenant_id: int, project_ref: int, resource_type: str) -> str:
    return f"{KEY_PREFIX}:{tenant_id}:{project_ref}:{TYPE_CODES[resource_type]}"


def parse_key(key: str) -> tuple[int, int, str] | None:
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != KEY_PREFIX or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    resource_type = next((kind for kind, code in TYPE_CODES.items() if code == parts[3]), None)
    return (int(parts[1]), int(parts[2]), resource_type) if resource_type else None


def load(db: Session) -> dict[tuple[int, int, str], dict]:
    return {parsed: value if isinstance(value, dict) else {} for key, value in db.execute(
        select(SysSetting.key, SysSetting.value).where(SysSetting.key.startswith(f"{KEY_PREFIX}:", autoescape=True)))
        if (parsed := parse_key(key)) is not None}


def policy(value: object, workspace: str) -> dict:
    """未配置沿用旧范围；显式指定空/畸形配置收紧，不从缺失 include 推断全量。"""
    if value is None:
        return {"mode": "inherit", "jobs": [], "configured": False}
    workspaces = value.get("workspaces") if isinstance(value, Mapping) and value.get("version") == 1 else None
    if isinstance(workspaces, Mapping) and workspace not in workspaces:
        return {"mode": "inherit", "jobs": [], "configured": False}
    stored = workspaces.get(workspace) if isinstance(workspaces, Mapping) else None
    if not isinstance(stored, dict) or stored.get("mode") not in ("all", "selected"):
        return {"mode": "selected", "jobs": [], "configured": True, "invalid": True}
    jobs = stored.get("jobs")
    if (not isinstance(jobs, list) or len(jobs) > 10000
            or any(not isinstance(name, str) or not name.strip() or name != name.strip()
                   or len(name) > 256 or any(ord(c) < 32 for c in name) for name in jobs)
            or stored["mode"] == "all" and jobs):
        return {"mode": "selected", "jobs": [], "configured": True, "invalid": True}
    return {"mode": stored["mode"], "jobs": sorted(set(jobs)), "configured": True}


@dataclass(frozen=True)
class JobSelectionScope:
    base: Any
    value: object

    @property
    def open(self) -> bool:
        return self.value is None and self.base.open

    @property
    def dims(self) -> frozenset[str]:
        return self.base.dims | frozenset(("workspace", "job"))

    def check(self, subject: Mapping) -> bool | None:
        common = self.base.check(subject)
        if common is False:
            return False
        if "workspace" not in subject:
            return None
        workspace = subject["workspace"]
        if not isinstance(workspace, str):
            return False
        selected = policy(self.value, workspace)
        if selected["mode"] in ("all", "inherit"):
            return common
        if not selected["jobs"]:
            return False
        if "job" not in subject:
            return None
        return common if subject["job"] in selected["jobs"] else False

    def keep(self, subject: Mapping) -> bool:
        return self.check(subject) is not False


def scope_for_context(ctx, resource_type: str) -> JobSelectionScope:
    """collector 接入点：列表前检查 workspace，详情/状态前检查 workspace+job。"""
    if resource_type not in TYPE_CODES:
        raise ValueError("仅支持 DataArts 实时/批作业监控名单")
    scopes = getattr(ctx, "scopes", None)
    entries = getattr(scopes, "job_selections", None)
    if entries is None:
        entries = load(ctx.db) if getattr(ctx, "db", None) is not None else {}
    key = (ctx.tenant_id, ctx.project_ref, resource_type)
    value = entries.get(key)
    ctx.params.setdefault("_job_selection_signatures", {})[resource_type] = _signature(value)
    workspaces = list(dict.fromkeys(ctx.workspaces or [""]))
    details = []
    for workspace in workspaces:
        configured = policy(value, workspace)
        reason = ("invalid_selection" if configured.get("invalid") else "no_selected_jobs"
                  if configured["mode"] == "selected" and not configured["jobs"] else "inherited_scope"
                  if configured["mode"] == "inherit" else "selected_jobs"
                  if configured["mode"] == "selected" else "all_jobs")
        details.append({"workspace": workspace, "mode": configured["mode"],
                        "selected_count": len(configured["jobs"]), "reason": reason})
    ctx.stats["job_selection"] = {"resource_type": resource_type, "workspaces": details}
    return JobSelectionScope(ctx.scope(resource_type), value)


def _signature(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _lock_tenant(db: Session, tenant_id: int) -> bool:
    """与 ECS 相同的锁顺序：租户父行 → 设置 → 资源 → 评估/告警。

    首次保存时设置行不存在，必须锁稳定父行串行化插入；调用方事务提交前不释放。
    """
    return db.scalar(select(HcsAccount.id).where(HcsAccount.id == tenant_id).with_for_update()) is not None


def selection_changed(ctx) -> bool:
    """最终 guard 锁租户直至 pipeline 提交，防止校验与写入之间被保存请求穿透。"""
    expected = ctx.params.get("_job_selection_signatures", {})
    if not expected or getattr(ctx, "db", None) is None:
        return False
    if ctx.tenant_id is not None and not _lock_tenant(ctx.db, ctx.tenant_id):
        return True
    current = load(ctx.db)
    return any(_signature(current.get((ctx.tenant_id, ctx.project_ref, resource_type))) != signature
               for resource_type, signature in expected.items())


def project_target(db: Session, tenant_id: int, project_ref: int, workspace: str | None = None):
    tenant = db.get(HcsAccount, tenant_id)
    project = db.scalar(select(HcsProject).where(HcsProject.id == project_ref, HcsProject.account_id == tenant_id))
    if tenant is None or project is None:
        raise BizError(ErrorCode.NOT_FOUND, "租户或所属项目不存在", 404)
    if workspace is not None and workspace not in (project.workspaces or [""]):
        raise BizError(ErrorCode.BAD_REQUEST, "工作空间不属于该项目的已配置工作空间；请先核对工作空间 ID")
    return tenant, project


def read(db: Session, tenant_id: int, resource_type: str, project_ref: int, workspace: str) -> dict:
    project_target(db, tenant_id, project_ref, workspace)
    row = db.scalar(select(SysSetting).where(SysSetting.key == setting_key(tenant_id, project_ref, resource_type))
                    .execution_options(populate_existing=True))
    return {"tenant_id": tenant_id, "resource_type": resource_type, "project_ref": project_ref,
            "workspace": workspace, **policy(row.value if row else None, workspace),
            "updated_by": row.updated_by if row else None, "updated_at": row.updated_at if row else None}


def save(db: Session, tenant_id: int, resource_type: str, body: JobSelectionIn, operator: str) -> dict:
    from . import collect_scope

    if not _lock_tenant(db, tenant_id):
        raise BizError(ErrorCode.NOT_FOUND, "租户不存在", 404)
    project_target(db, tenant_id, body.project_ref, body.workspace)
    key = setting_key(tenant_id, body.project_ref, resource_type)
    row = db.scalar(select(SysSetting).where(SysSetting.key == key).with_for_update()
                    .execution_options(populate_existing=True))
    before = policy(row.value if row else None, body.workspace)
    after = {"mode": body.mode, "jobs": body.jobs}
    changed = not before["configured"] or bool(before.get("invalid")) or any(before[k] != after[k] for k in after)
    if changed:
        raw = row.value if row and isinstance(row.value, dict) and row.value.get("version") == 1 else {}
        raw_workspaces = raw.get("workspaces")
        workspaces = dict(raw_workspaces) if isinstance(raw_workspaces, dict) else {}
        workspaces[body.workspace] = after
        stored = {"version": 1, "revision": uuid4().hex, "workspaces": workspaces}
        if row is None:
            db.add(SysSetting(key=key, value=stored, updated_by=operator))
        else:
            row.value, row.updated_by, row.updated_at = stored, operator, utcnow()
        db.flush()
    impact = collect_scope.sync_flags(db, tenant_id, [resource_type], project_ref=body.project_ref, operator=operator)
    return {"changed": changed, "before": before, "after": {**after, "configured": True}, **impact}


def targets(db: Session, scope) -> list[dict]:
    rows = db.execute(scope.apply(select(HcsAccount.id, HcsAccount.name, HcsAccount.short_name,
        HcsAccount.enabled, HcsProject.id.label("project_ref"), HcsProject.project_id,
        HcsProject.project_name, HcsProject.workspaces, HcsProject.enabled.label("project_enabled"))
        .join(HcsProject, HcsProject.account_id == HcsAccount.id), HcsAccount.id)
        .order_by(HcsAccount.id, HcsProject.id))
    result: dict[int, dict] = {}
    for row in rows:
        tenant = result.setdefault(row.id, {"id": row.id, "name": row.short_name or row.name,
                                           "enabled": row.enabled, "projects": []})
        tenant["projects"].append({"id": row.project_ref, "project_id": row.project_id,
            "name": row.project_name or row.project_id, "enabled": row.project_enabled,
            "workspaces": list(dict.fromkeys(row.workspaces or [""]))})
    return list(result.values())


def discover(db: Session, tenant_id: int, resource_type: str, project_ref: int, workspace: str,
             *, page: int, page_size: int, keyword: str = "", client=None) -> dict:
    tenant, project = project_target(db, tenant_id, project_ref, workspace)
    owned = client is None
    client = client or client_for(tenant, project)
    params: dict = {"jobType": JOB_TYPES[resource_type], "offset": page - 1, "limit": page_size}
    if keyword.strip():
        params["jobName"] = keyword.strip()
    try:
        data = client.get("dataarts", "/v1/{project_id}/jobs", params=params,
                          headers={"workspace": workspace} if workspace else None)
        items = response_items(data, "jobs")
        total = response_total(data, "total", required=True)
        if total is None or len(items) > min(total, page_size) or not items and total > (page - 1) * page_size:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "作业清单记录数与 total 不一致，未将失败清单视为空")
        names = [job.get("name") for job in items]
        if (any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)
                or any(job.get("jobType") not in (None, JOB_TYPES[resource_type]) for job in items)):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "作业清单名称或类型不符合查询条件")
        return {"items": [{"name": job["name"], "job_type": job.get("jobType"),
                           "status": job.get("status") if isinstance(job.get("status"), str) else None}
                          for job in items], "total": total, "page": page, "page_size": page_size}
    finally:
        if owned:
            client.http.close()
