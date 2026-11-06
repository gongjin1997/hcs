"""内置规则恢复须有已知状态证据；保留自定义并可逆（SQLite/PostgreSQL）。"""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
MARKER = "system:evidence-recovery-v1"
PREFIX = "rule-evidence-0014:"
PATCHES = {'R-DGC-RT-003': {'match': {'name': '实时作业长时间启动中',
                            'resource_type': 'dgc_realtime_job',
                            'check_item': 'job_status',
                            'kind': 'state',
                            'expr': {'when': "status == 'STARTING' and status_duration_min > 10",
                                     'severity': 'P2',
                                     'title': '作业启动中已持续 {status_duration_min} 分钟'},
                            'for_count': 1,
                            'scope': {},
                            'enabled': True,
                            'repeat_interval_sec': None},
                  'before': {'when': "status != 'STARTING'"},
                  'after': {'when': "status in ('NORMAL', 'EXCEPTION', 'STOPPING', 'STOPPED')"}},
 'R-DGC-RT-004': {'match': {'name': '实时作业长时间停止中',
                            'resource_type': 'dgc_realtime_job',
                            'check_item': 'job_status',
                            'kind': 'state',
                            'expr': {'when': "status == 'STOPPING' and status_duration_min > 10",
                                     'severity': 'P3',
                                     'title': '作业停止中已持续 {status_duration_min} 分钟'},
                            'for_count': 1,
                            'scope': {},
                            'enabled': True,
                            'repeat_interval_sec': None},
                  'before': {'when': "status != 'STOPPING'"},
                  'after': {'when': "status in ('STARTING', 'NORMAL', 'EXCEPTION', 'STOPPED')"}},
 'R-DGC-BAT-002': {'match': {'name': '批作业运行异常',
                             'resource_type': 'dgc_batch_job',
                             'check_item': 'instance_status',
                             'kind': 'state',
                             'expr': {'when': "last_status == 'running-exception'",
                                      'severity': 'P2',
                                      'value_field': 'last_status',
                                      'title': '批作业运行异常（running-exception）'},
                             'for_count': 1,
                             'scope': {},
                             'enabled': True,
                             'repeat_interval_sec': None},
                   'before': {'when': "last_status != 'running-exception'"},
                   'after': {'when': "last_status in ('waiting', 'running', 'success', 'fail', 'pause', "
                                     "'manual-stop')"}},
 'R-DGC-BAT-003': {'match': {'name': '批作业运行超时',
                             'resource_type': 'dgc_batch_job',
                             'check_item': 'instance_status',
                             'kind': 'state',
                             'expr': {'when': "last_status == 'running' and run_minutes > max(p90_minutes * "
                                              '2, p90_minutes + 30, 30)',
                                      'severity': 'P2',
                                      'value_field': 'run_minutes',
                                      'title': '批作业已运行 {run_minutes} 分钟，超过基线'},
                             'for_count': 1,
                             'scope': {},
                             'enabled': True,
                             'repeat_interval_sec': None},
                   'before': {'when': "last_status != 'running'"},
                   'after': {'when': "last_status in ('waiting', 'success', 'fail', 'running-exception', "
                                     "'pause', 'manual-stop')"},
                   'runbook_before': '1. 对比该作业近 7 天运行时长（P90），确认是否数据量突增或资源队列拥塞；\n'
                                     '2. 查看当前运行节点是否卡在等待锁 / 等待资源，必要时联系数据库或队列管理员；\n'
                                     '3. 影响下游时间窗时，评估终止后重跑或通知下游延后。\n',
                   'runbook_after': '1. 基线仅取本轮完整24h实例列表。\n'
                                    '   按workspace和jobName隔离。\n'
                                    '   仅success且毫秒起止有效，instanceId独立。\n'
                                    '   按nearest-rank计算P90，不预先取整。\n'
                                    '2. 最少5条是本地策略；\n'
                                    '   用p90_min_samples调整。\n'
                                    '   不足不判定超时。\n'
                                    '   不使用上游p90_minutes扩展；\n'
                                    '   不使用executeTime猜单位。\n'
                                    '3. 检查当前节点日志、锁与资源队列。\n'
                                    '   核对24h真实样本和下游时间窗。\n'},
 'R-MRS-JOB-001': {'match': {'name': 'MRS 作业执行失败',
                             'resource_type': 'mrs_job',
                             'check_item': 'job_status',
                             'kind': 'state',
                             'expr': {'when': "job_state == 'FAILED' or job_result == 'FAILED'",
                                      'severity': 'P2',
                                      'critical_severity': 'P1',
                                      'value_field': 'job_result',
                                      'title': '作业执行失败（{job_result}）'},
                             'for_count': 1,
                             'scope': {},
                             'enabled': True,
                             'repeat_interval_sec': None},
                   'before': {'when': "job_result == 'SUCCEEDED'"},
                   'after': {'when': "job_state in ('FINISHED', 'SUCCEEDED') and job_result == 'SUCCEEDED'"}},
 'R-MRS-JOB-003': {'match': {'name': 'MRS 作业排队超时',
                             'resource_type': 'mrs_job',
                             'check_item': 'job_status',
                             'kind': 'state',
                             'expr': {'when': "job_state == 'ACCEPTED' and status_duration_min > 15",
                                      'severity': 'P3',
                                      'value_field': 'job_state',
                                      'title': '作业排队已 {status_duration_min} 分钟'},
                             'for_count': 1,
                             'scope': {},
                             'enabled': True,
                             'repeat_interval_sec': None},
                   'before': {'when': "job_state != 'ACCEPTED'"},
                   'after': {'when': "job_state in ('RUNNING', 'FINISHED', 'FAILED', 'SUCCEEDED', "
                                     "'KILLED')"}}}


def _tables():
    rule = sa.table("alarm_rule", sa.column("id", sa.String()), sa.column("name", sa.String()),
        sa.column("resource_type", sa.String()), sa.column("check_item", sa.String()),
        sa.column("kind", sa.String()), sa.column("runbook", sa.Text()),
        sa.column("expr", sa.JSON()), sa.column("recover", sa.JSON()),
        sa.column("for_count", sa.Integer()), sa.column("scope", sa.JSON()), sa.column("enabled", sa.Boolean()),
        sa.column("repeat_interval_sec", sa.Integer()), sa.column("version", sa.Integer()),
        sa.column("updated_by", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True)))
    setting = sa.table("sys_setting", sa.column("key", sa.String()), sa.column("value", sa.JSON()),
                       sa.column("updated_by", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True)))
    return rule, setting


def upgrade() -> None:
    connection = op.get_bind()
    rule, setting = _tables()
    rows = connection.execute(sa.select(rule).where(rule.c.id.in_(PATCHES))).mappings().all()
    for row in rows:
        patch = PATCHES[row["id"]]
        if (row["version"] != 1 or row["updated_by"] != "system" or row["recover"] != patch["before"]
                or any(row[key] != value for key, value in patch["match"].items())):
            continue
        if "runbook_after" in patch and row["runbook"] not in (None, "", patch["runbook_before"]):
            continue
        key = PREFIX + row["id"]
        if connection.execute(sa.select(setting.c.key).where(setting.c.key == key)).first() is not None:
            continue
        backup = {"before": {"recover": row["recover"], "version": row["version"], "updated_by": row["updated_by"],
                              "updated_at": row["updated_at"].isoformat()},
                  "after": patch["after"], "match": patch["match"]}
        changes = {"recover": patch["after"], "version": row["version"] + 1, "updated_by": MARKER}
        if "runbook_after" in patch:
            backup["before"]["runbook"] = row["runbook"]
            backup["runbook_after"] = patch["runbook_after"]
            changes["runbook"] = patch["runbook_after"]
        connection.execute(setting.insert().values(key=key, value=backup, updated_by=MARKER,
                                                    updated_at=datetime.now(UTC)))
        connection.execute(rule.update().where(rule.c.id == row["id"]).values(**changes))


def downgrade() -> None:
    connection = op.get_bind()
    rule, setting = _tables()
    for code in PATCHES:
        key = PREFIX + code
        backup = connection.execute(sa.select(setting.c.value).where(setting.c.key == key)).scalar_one_or_none()
        if backup is None:
            continue
        row = connection.execute(sa.select(rule).where(rule.c.id == code)).mappings().first()
        if (row is not None and row["updated_by"] == MARKER and row["version"] == backup["before"]["version"] + 1
                and row["recover"] == backup["after"]
                and ("runbook_after" not in backup or row["runbook"] == backup["runbook_after"])
                and all(row[field] == value for field, value in backup["match"].items())):
            previous = dict(backup["before"])
            if "updated_at" in previous:
                previous["updated_at"] = datetime.fromisoformat(previous["updated_at"])
            connection.execute(rule.update().where(rule.c.id == code).values(**previous))
        connection.execute(setting.delete().where(setting.c.key == key))
