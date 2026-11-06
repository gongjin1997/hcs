"""下线 RDS / Kafka / CSS；MRS 作业改由 DataArts 作业实例详情（MRS 类节点）采集。

- 关闭这三类资源与旧口径 MRS 作业（V2 job-executions，attrs 含 cluster_id）上的活动告警（留处理记录），
  删除其资源、变更记录、评估状态与采集开关。旧 MRS 作业资源 ID 为“集群/作业名”，新口径为“[工作空间/]作业名/节点名”。
- 内置规则：未经人工修改的 R-MRS-JOB-001 改为 DataArts 节点状态口径；R-MRS-JOB-003（YARN 排队）在 DataArts
  接口中没有对应字段，停用。修改前的规则备份在 sys_setting，可 downgrade 还原。
- downgrade 只还原规则；删除的资源和告警历史不可恢复，升级前由部署脚本自动备份（deploy.sh update / backup.sh）。
"""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

RETIRED_TYPES = ("rds_instance", "kafka_instance", "css_cluster")
MARKER = "system:dataarts-mrs-v1"
PREFIX = "rule-dataarts-mrs-0018:"
SYSTEM_EDITORS = ("system", "system:evidence-recovery-v1")
OLD_JOB_001 = {"when": "job_state == 'FAILED' or job_result == 'FAILED'", "severity": "P2",
               "critical_severity": "P1", "value_field": "job_result", "title": "作业执行失败（{job_result}）"}
OLD_JOB_003 = {"when": "job_state == 'ACCEPTED' and status_duration_min > 15", "severity": "P3",
               "value_field": "job_state", "title": "作业排队已 {status_duration_min} 分钟"}
NEW_JOB_001 = {
    "expr": {"when": "node_status == 'fail'", "severity": "P2", "critical_severity": "P1",
             "value_field": "node_status", "title": "MRS 作业节点执行失败（{node_status}）"},
    "recover": {"when": "node_status == 'success'"},
    "runbook": "1. 在 DataArts Studio 实例监控中打开该作业实例，查看失败 MRS 节点的日志（logPath）；\n"
               "2. 跳转 MRS Manager / YARN 查看对应 application 的错误与资源队列，资源不足时调整队列配额；\n"
               "3. 修复后在 DataArts 重跑失败实例，下一次实例中该节点成功即自动恢复。\n",
}


def _tables() -> dict[str, sa.TableClause]:
    return {
        "rule": sa.table("alarm_rule", sa.column("id", sa.String()), sa.column("expr", sa.JSON()),
                         sa.column("recover", sa.JSON()), sa.column("runbook", sa.Text()),
                         sa.column("enabled", sa.Boolean()), sa.column("version", sa.Integer()),
                         sa.column("updated_by", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True))),
        "setting": sa.table("sys_setting", sa.column("key", sa.String()), sa.column("value", sa.JSON()),
                            sa.column("updated_by", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True))),
        "resource": sa.table("resource", sa.column("id", sa.Integer()), sa.column("resource_type", sa.String()),
                             sa.column("attrs", sa.JSON())),
        "change": sa.table("resource_change", sa.column("resource_ref", sa.Integer()),
                           sa.column("resource_type", sa.String())),
        "alarm": sa.table("alarm_event", sa.column("id", sa.Integer()), sa.column("resource_ref", sa.Integer()),
                          sa.column("resource_type", sa.String()), sa.column("status", sa.String()),
                          sa.column("resolved_at", sa.DateTime(timezone=True))),
        "log": sa.table("alarm_event_log", sa.column("alarm_id", sa.Integer()), sa.column("action", sa.String()),
                        sa.column("from_status", sa.String()), sa.column("to_status", sa.String()),
                        sa.column("operator", sa.String()), sa.column("note", sa.Text()),
                        sa.column("at", sa.DateTime(timezone=True))),
        "state": sa.table("eval_state", sa.column("fingerprint", sa.String()), sa.column("data", sa.JSON())),
        "switch": sa.table("collect_switch", sa.column("resource_type", sa.String())),
    }


def _retire_data(connection: sa.Connection, t: dict[str, sa.TableClause], now: datetime) -> None:
    refs: list[int] = []
    for rid, rtype, attrs in connection.execute(sa.select(t["resource"].c.id, t["resource"].c.resource_type,
                                                          t["resource"].c.attrs).where(
            t["resource"].c.resource_type.in_((*RETIRED_TYPES, "mrs_job")))):
        attrs = attrs if isinstance(attrs, dict) else {}
        if rtype in RETIRED_TYPES or attrs.get("source") != "dataarts_instance_detail":
            refs.append(rid)
    old_jobs = set(refs)
    active = ("FIRING", "ACKED", "SUPPRESSED")
    alarm = t["alarm"]
    rows = connection.execute(sa.select(alarm.c.id, alarm.c.status, alarm.c.resource_ref, alarm.c.resource_type).where(
        alarm.c.status.in_(active), alarm.c.resource_type.in_((*RETIRED_TYPES, "mrs_job")))).all()
    for aid, status, ref, rtype in rows:
        if rtype == "mrs_job" and ref not in old_jobs:
            continue
        note = ("MRS 作业改由 DataArts 作业实例详情采集，旧口径对象已下线，告警自动关闭" if rtype == "mrs_job"
                else "该云服务已从平台下线（RDS / Kafka / CSS 不再采集），告警自动关闭")
        connection.execute(alarm.update().where(alarm.c.id == aid).values(status="CLOSED", resolved_at=now))
        connection.execute(t["log"].insert().values(alarm_id=aid, action="CLOSE", from_status=status,
                                                    to_status="CLOSED", operator="system", note=note, at=now))
    for start in range(0, len(refs), 500):
        chunk = refs[start:start + 500]
        connection.execute(t["change"].delete().where(t["change"].c.resource_ref.in_(chunk)))
        connection.execute(t["resource"].delete().where(t["resource"].c.id.in_(chunk)))
    connection.execute(t["change"].delete().where(t["change"].c.resource_type.in_(RETIRED_TYPES)))
    connection.execute(t["switch"].delete().where(t["switch"].c.resource_type.in_(RETIRED_TYPES)))
    gone = set(refs)
    stale = []
    for fp, data in connection.execute(sa.select(t["state"].c.fingerprint, t["state"].c.data)):
        meta = (data or {}).get("_inspection_evidence") if isinstance(data, dict) else None
        if isinstance(meta, dict) and (meta.get("resource_type") in RETIRED_TYPES or meta.get("resource_ref") in gone):
            stale.append(fp)
    for start in range(0, len(stale), 500):
        connection.execute(t["state"].delete().where(t["state"].c.fingerprint.in_(stale[start:start + 500])))


def _patch_rules(connection: sa.Connection, t: dict[str, sa.TableClause], now: datetime) -> None:
    rule, setting = t["rule"], t["setting"]
    for code, old_expr in (("R-MRS-JOB-001", OLD_JOB_001), ("R-MRS-JOB-003", OLD_JOB_003)):
        row = connection.execute(sa.select(rule).where(rule.c.id == code)).mappings().first()
        if row is None or row["updated_by"] not in SYSTEM_EDITORS or row["expr"] != old_expr:
            continue  # 已人工修改的规则不覆盖；其旧字段在新口径下无数据，不会误触发
        key = PREFIX + code
        if connection.execute(sa.select(setting.c.key).where(setting.c.key == key)).first() is not None:
            continue
        before = {k: row[k] for k in ("expr", "recover", "runbook", "enabled", "version", "updated_by")}
        before["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
        changes = ({**NEW_JOB_001} if code == "R-MRS-JOB-001" else {"enabled": False})
        changes.update(version=row["version"] + 1, updated_by=MARKER, updated_at=now)
        connection.execute(setting.insert().values(key=key, value={"before": before}, updated_by=MARKER,
                                                    updated_at=now))
        connection.execute(rule.update().where(rule.c.id == code).values(**changes))


def upgrade() -> None:
    connection = op.get_bind()
    t = _tables()
    now = datetime.now(UTC)
    _retire_data(connection, t, now)
    _patch_rules(connection, t, now)


def downgrade() -> None:
    connection = op.get_bind()
    t = _tables()
    rule, setting = t["rule"], t["setting"]
    for code in ("R-MRS-JOB-001", "R-MRS-JOB-003"):
        key = PREFIX + code
        backup = connection.execute(sa.select(setting.c.value).where(setting.c.key == key)).scalar_one_or_none()
        if backup is None:
            continue
        row = connection.execute(sa.select(rule).where(rule.c.id == code)).mappings().first()
        before = dict(backup["before"])
        if row is not None and row["updated_by"] == MARKER and row["version"] == before["version"] + 1:
            at = before.pop("updated_at", None)
            before["updated_at"] = datetime.fromisoformat(at) if at else datetime.now(UTC)
            connection.execute(rule.update().where(rule.c.id == code).values(**before))
        connection.execute(setting.delete().where(setting.c.key == key))
