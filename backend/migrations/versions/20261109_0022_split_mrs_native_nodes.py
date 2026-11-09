"""恢复 MRS 原生任务，非破坏性分离 DataArts MRS 节点。

保留资源主键、告警状态/历史；系统原版节点规则仅迁移类型，人工规则不动。
旧 workspace/job/node 范围复制给节点，原生采集关闭直到重新授权，任务启停/周期不改。
活动告警身份/指纹不能验证或目标冲突时事务中止，禁止丢历史或猜测归属。
降级只允许本迁移后的配置/记录未变化；有新采集或人工变更则要求先备份复核。
"""
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha1, sha256
import json

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None
MARKER = "system:mrs-split-0022"
JOURNAL = "migration:mrs-split-0022"
OLD, NODE = "mrs_job", "dgc_mrs_node"
OLD_TASK, NODE_TASK = "mrs_job_status", "dgc_mrs_node_status"
SYSTEM_EDITORS = {"system", "system:evidence-recovery-v1", "system:dataarts-mrs-v1"}
NODE_EXPR = {"when": "node_status == 'fail'", "severity": "P2", "critical_severity": "P1",
             "value_field": "node_status", "title": "MRS 作业节点执行失败（{node_status}）"}
NODE_RUNBOOK = ("1. 在 DataArts Studio 实例监控中打开该作业实例，查看失败 MRS 节点的日志（logPath）；\n"
                "2. 跳转 MRS Manager / YARN 查看对应 application 的错误与资源队列，资源不足时调整队列配额；\n"
                "3. 修复后在 DataArts 重跑失败实例，下一次实例中该节点成功即自动恢复。")
ACTIVE = ("FIRING", "ACKED", "SUPPRESSED")


def _plain(value):
    if isinstance(value, datetime):
        return {"__datetime__": value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.astimezone(UTC).isoformat()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _restore(value):
    if isinstance(value, dict):
        if set(value) == {"__datetime__"}:
            return datetime.fromisoformat(value["__datetime__"])
        return {k: _restore(v) for k, v in value.items()}
    return value


def _tables(connection):
    meta = sa.MetaData()
    return {name: sa.Table(name, meta, autoload_with=connection) for name in (
        "resource", "resource_change", "alarm_event", "eval_state", "alarm_rule", "sys_setting",
        "inspection_task", "collect_switch", "hcs_account", "hcs_project", "sys_user")}


def _mrs_scope_dims(value) -> set[str] | None:
    rules = value.get("rules") if isinstance(value, dict) else None
    if not isinstance(rules, list):
        return None
    dims = set()
    for rule in rules:
        if (not isinstance(rule, dict) or rule.get("effect") not in ("include", "exclude")
                or not isinstance(rule.get("match"), dict) or not rule["match"]):
            return None
        for key, patterns in rule["match"].items():
            if (not isinstance(key, str) or not isinstance(patterns, list) or not patterns
                    or any(not isinstance(pattern, str) or not pattern for pattern in patterns)):
                return None
            dims.add(key)
    return dims


def _scope(tenant, project, task):
    value = [tenant, project, task, None, None]
    return sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _fingerprint(account, project, kind, rid, rule, sub=""):
    return sha1("|".join((account, project, kind, rid, rule, sub or "")).encode()).hexdigest()


def upgrade():
    connection = op.get_bind()
    t = _tables(connection)
    setting = t["sys_setting"]
    if connection.execute(sa.select(setting.c.key).where(setting.c.key == JOURNAL)).first():
        return
    # 空库由 bootstrap 完整初始化；不能预建任务行，使生产首次安装误判为旧环境并漏关全局开关。
    if not any(connection.execute(sa.select(sa.literal(1)).select_from(t[name]).limit(1)).first() is not None
               for name in ("sys_user", "hcs_account", "resource", "inspection_task")):
        return
    now = datetime.now(UTC)
    journal = []

    def patch(table, key, values, *, insert=False):
        target = t[table]
        where = sa.and_(*(target.c[k] == v for k, v in key.items()))
        before = connection.execute(sa.select(target).where(where)).mappings().first()
        if before is not None and insert:
            return
        changed = values if before is None else {k: v for k, v in values.items() if before[k] != v}
        if not changed and before is not None:
            return
        if before is None:
            connection.execute(target.insert().values(**key, **values))
        else:
            connection.execute(target.update().where(where).values(**changed))
        journal.append({"table": table, "key": key,
                        "before": None if before is None else _plain({k: before[k] for k in changed}),
                        "after": _plain(changed)})

    resources = [dict(row) for row in connection.execute(sa.select(t["resource"]).where(
        t["resource"].c.resource_type == OLD)).mappings()
        if isinstance(row["attrs"], dict) and row["attrs"].get("source") == "dataarts_instance_detail"]
    refs = {row["id"]: row for row in resources}
    accounts = dict(connection.execute(sa.select(t["hcs_account"].c.id, t["hcs_account"].c.code)).all())
    projects = {row.id: row for row in connection.execute(sa.select(
        t["hcs_project"].c.id, t["hcs_project"].c.account_id, t["hcs_project"].c.project_id))}

    def identity(row):
        project = projects.get(row["project_ref"])
        account = accounts.get(row["tenant_id"])
        return (account, project.project_id) if account and project and project.account_id == row["tenant_id"] else None

    # Preflight all unique identities before writing anything.
    for row in resources:
        conflict = connection.execute(sa.select(t["resource"].c.id).where(
            t["resource"].c.resource_type == NODE, t["resource"].c.resource_id == row["resource_id"],
            t["resource"].c.tenant_id == row["tenant_id"],
            t["resource"].c.project_ref == row["project_ref"])).first()
        if conflict:
            raise RuntimeError("0022: 节点目标资源已存在；请先备份并核对冲突，不会合并或删除记录")
    alarm_moves = []
    for row in connection.execute(sa.select(t["alarm_event"]).where(
            t["alarm_event"].c.resource_type == OLD)).mappings():
        resource = refs.get(row["resource_ref"])
        if resource is None:
            continue
        owner = identity(resource)
        fp = None
        if owner:
            old_fp = _fingerprint(*owner, OLD, resource["resource_id"], row["rule_id"], row["sub_object"])
            if old_fp == row["fingerprint"]:
                fp = _fingerprint(*owner, NODE, resource["resource_id"], row["rule_id"], row["sub_object"])
        if fp is None and row["status"] in ACTIVE:
            raise RuntimeError("0022: 活动节点告警的租户/项目或旧指纹无法验证；请先备份核对，迁移未修改数据")
        if fp and row["status"] in ACTIVE and connection.execute(sa.select(t["alarm_event"].c.id).where(
                t["alarm_event"].c.fingerprint == fp, t["alarm_event"].c.status.in_(ACTIVE))).first():
            raise RuntimeError("0022: 目标节点活动告警指纹已存在；迁移拒绝覆盖历史")
        alarm_moves.append((dict(row), fp))

    old_task = connection.execute(sa.select(t["inspection_task"]).where(
        t["inspection_task"].c.code == OLD_TASK)).mappings().first()
    schedule = ({k: old_task[k] for k in ("interval_sec", "cron", "enabled", "params")} if old_task else
                {"interval_sec": 300, "cron": None, "enabled": False, "params": {}})
    patch("inspection_task", {"code": NODE_TASK}, {"name": "DataArts MRS 节点状态", "service": "dataarts",
                                                   **schedule}, insert=True)
    # 不改旧 native task 的 enabled / interval / cron / params。
    switches = {row.tenant_id: row.enabled for row in connection.execute(sa.select(
        t["collect_switch"].c.tenant_id, t["collect_switch"].c.enabled).where(
            t["collect_switch"].c.resource_type == OLD))}
    patch("collect_switch", {"tenant_id": 0, "resource_type": NODE},
          {"enabled": switches.get(0, old_task is not None), "updated_by": MARKER, "updated_at": now}, insert=True)
    for tid, enabled in switches.items():
        patch("collect_switch", {"tenant_id": tid, "resource_type": NODE},
              {"enabled": enabled, "updated_by": MARKER, "updated_at": now}, insert=True)
    if old_task is None:
        patch("collect_switch", {"tenant_id": 0, "resource_type": OLD},
              {"enabled": False, "updated_by": MARKER, "updated_at": now}, insert=True)

    scopes = {}
    for row in connection.execute(sa.select(setting).where(setting.c.key.like("collect_scope:%:mrs_job"))).mappings():
        parts = row["key"].split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        tid, value = int(parts[1]), row["value"]
        dims = _mrs_scope_dims(value)
        scopes[tid] = {
            "native_block": dims is None or bool(dims) and (
                dims <= {"job"} or bool(dims - {"cluster", "job", "id"})),
            "node_block": dims is None or bool(dims - {"workspace", "job", "node"}),
        }
        # 完整复制原范围，包括不能映射的维度；禁止丢弃维度后意外变成全量。
        patch("sys_setting", {"key": f"collect_scope:{tid}:{NODE}"},
              {"value": value, "updated_by": row["updated_by"], "updated_at": row["updated_at"]}, insert=True)
    # 目标类型已有的范围是独立授权，优先于从旧类型复制的范围；不能由源类型的不兼容维度误关。
    node_scopes = {}
    for row in connection.execute(sa.select(setting.c.key, setting.c.value).where(
            setting.c.key.like("collect_scope:%:dgc_mrs_node"))):
        parts = row.key.split(":")
        if len(parts) == 3 and parts[1].isdigit():
            dims = _mrs_scope_dims(row.value)
            node_scopes[int(parts[1])] = dims is None or bool(dims - {"workspace", "job", "node"})
    for tid in {0, *accounts, *switches, *scopes, *node_scopes}:
        native_block = scopes.get(tid, scopes.get(0, {})).get("native_block", False)
        node_block = node_scopes.get(tid, node_scopes.get(0, False))
        for kind, blocked in ((OLD, native_block), (NODE, node_block)):
            if blocked:
                patch("collect_switch", {"tenant_id": tid, "resource_type": kind},
                      {"enabled": False, "updated_by": MARKER, "updated_at": now})

    for row in resources:
        attrs = deepcopy(row["attrs"])
        monitoring = attrs.get("inspection_monitoring")
        if isinstance(monitoring, dict) and monitoring.get("task_code") == OLD_TASK:
            monitoring["task_code"] = NODE_TASK
        patch("resource", {"id": row["id"]}, {"resource_type": NODE, "service": "dataarts", "attrs": attrs})
        for change in connection.execute(sa.select(t["resource_change"].c.id).where(
                t["resource_change"].c.resource_ref == row["id"], t["resource_change"].c.resource_type == OLD)):
            patch("resource_change", {"id": change.id}, {"resource_type": NODE})
    fp_moves = {}
    for row, fp in alarm_moves:
        patch("alarm_event", {"id": row["id"]},
              {"resource_type": NODE, "service": "dataarts", **({"fingerprint": fp} if fp else {})})
        if fp:
            fp_moves[row["fingerprint"]] = fp
    for row in list(connection.execute(sa.select(t["eval_state"])).mappings()):
        data = deepcopy(row["data"])
        meta = data.get("_inspection_evidence") if isinstance(data, dict) else None
        resource = refs.get(meta.get("resource_ref")) if isinstance(meta, dict) else None
        fp = fp_moves.get(row["fingerprint"])
        if resource is None and fp is None:
            continue
        if resource:
            owner = identity(resource)
            if owner and meta.get("rule_id"):
                old = _fingerprint(*owner, OLD, resource["resource_id"], meta["rule_id"], meta.get("sub_object", ""))
                if old == row["fingerprint"]:
                    fp = _fingerprint(*owner, NODE, resource["resource_id"], meta["rule_id"], meta.get("sub_object", ""))
            meta["resource_type"], meta["task_code"] = NODE, NODE_TASK
            data["_evidence_scope"] = _scope(resource["tenant_id"], resource["project_ref"], NODE_TASK)
        if fp and connection.execute(sa.select(t["eval_state"].c.fingerprint).where(
                t["eval_state"].c.fingerprint == fp)).first():
            raise RuntimeError("0022: 节点评估证据指纹冲突，迁移拒绝覆盖")
        patch("eval_state", {"fingerprint": row["fingerprint"]},
              {"data": data, **({"fingerprint": fp} if fp else {})})
    for row in connection.execute(sa.select(t["alarm_rule"]).where(
            t["alarm_rule"].c.id == "R-MRS-JOB-001")).mappings():
        if (row["resource_type"] == OLD and row["updated_by"] in SYSTEM_EDITORS and row["expr"] == NODE_EXPR
                and row["recover"] == {"when": "node_status == 'success'"} and row["scope"] == {}
                and row["check_item"] == "job_status" and row["kind"] == "state" and row["for_count"] == 1
                and row["repeat_interval_sec"] is None and (row["runbook"] or "").strip() == NODE_RUNBOOK):
            patch("alarm_rule", {"id": row["id"]}, {"resource_type": NODE})
    consolidated = {}
    for change in journal:
        token = (change["table"], json.dumps(change["key"], sort_keys=True))
        if token not in consolidated:
            consolidated[token] = change
        else:
            previous = consolidated[token]
            if previous["before"] is not None:
                for key, value in (change["before"] or {}).items():
                    previous["before"].setdefault(key, value)
            previous["after"].update(change["after"])
    journal = list(consolidated.values())
    connection.execute(setting.insert().values(key=JOURNAL, value={"changes": journal, "node_refs": list(refs)},
                                                updated_by=MARKER, updated_at=now))


def downgrade():
    connection = op.get_bind()
    t = _tables(connection)
    setting = t["sys_setting"]
    journal = connection.execute(sa.select(setting.c.value).where(setting.c.key == JOURNAL)).scalar_one_or_none()
    if journal is None:
        # 空库升级不产生迁移日志；升级后若已采集新节点，仍必须保护新数据，不能静默降级。
        if connection.execute(sa.select(t["resource"].c.id).where(t["resource"].c.resource_type == NODE).limit(1)).first():
            raise RuntimeError("0022: 已有新节点采集，不能安全自动降级；请备份后复核")
        initialized = (
            ("inspection_task", "code", NODE_TASK), ("alarm_rule", "resource_type", NODE),
            ("collect_switch", "resource_type", NODE), ("alarm_event", "resource_type", NODE),
        )
        if any(connection.execute(sa.select(sa.literal(1)).select_from(t[name]).where(
                t[name].c[column] == value).limit(1)).first() is not None for name, column, value in initialized):
            raise RuntimeError("0022: 空库升级后已有节点初始化配置，缺少可逆快照；请备份复核后降级")
        return
    if connection.execute(sa.select(t["resource"].c.id).where(
            t["resource"].c.resource_type == NODE, t["resource"].c.id.not_in(journal["node_refs"]))).first():
        raise RuntimeError("0022: 已有新节点采集，不能安全自动降级；请备份后复核")
    # First validate every change; never partially roll back edited config/history.
    for change in journal["changes"]:
        table = t[change["table"]]
        key = {**change["key"], **{k: v for k, v in change["after"].items() if k in change["key"]}}
        row = connection.execute(sa.select(table).where(sa.and_(*(table.c[k] == v for k, v in key.items())))).mappings().first()
        if row is None or any(_plain(row[k]) != v for k, v in change["after"].items()):
            raise RuntimeError("0022: 迁移后记录/配置已变更，拒绝自动降级覆盖人工或新采集结果")
    for change in reversed(journal["changes"]):
        table = t[change["table"]]
        key = {**change["key"], **{k: v for k, v in change["after"].items() if k in change["key"]}}
        where = sa.and_(*(table.c[k] == v for k, v in key.items()))
        if change["before"] is None:
            connection.execute(table.delete().where(where))
        else:
            connection.execute(table.update().where(where).values(**{k: _restore(v) for k, v in change["before"].items()}))
    connection.execute(setting.delete().where(setting.c.key == JOURNAL))
