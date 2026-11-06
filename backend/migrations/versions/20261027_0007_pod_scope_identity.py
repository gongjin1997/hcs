"""Pod 状态、事件按租户与项目隔离；未知项目不猜测，碰撞时拒绝有损降级。"""
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

META = "_pod_scope_0007"
INDEXES = (
    ("idx_pod_state_scope_pod", "pod_state", ["tenant_id", "project_ref", "cluster_id", "namespace", "pod"]),
    ("idx_pod_event_scope_dedup", "pod_event", ["tenant_id", "project_ref", "cluster_id", "dedup_key"]),
    ("idx_pod_event_scope_pod", "pod_event", ["tenant_id", "project_ref", "cluster_id", "namespace", "pod"]),
)


def _hash(prefix, values):
    raw = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode()).hexdigest()


def _state_table():
    return sa.table("pod_state", sa.column("key", sa.String(320)), sa.column("tenant_id", sa.Integer()),
                    sa.column("project_ref", sa.Integer()), sa.column("cluster_id", sa.String(64)),
                    sa.column("namespace", sa.String(128)), sa.column("pod", sa.String(255)),
                    sa.column("data", sa.JSON()))


def _event_table():
    return sa.table("pod_event", sa.column("id", sa.Integer()), sa.column("tenant_id", sa.Integer()),
                    sa.column("project_ref", sa.Integer()), sa.column("cluster_id", sa.String(64)),
                    sa.column("dedup_key", sa.String(160)), sa.column("detail", sa.JSON()))


def _pack(data, field, value):
    if isinstance(data, dict):
        metadata = {field: value}
        if META in data:
            metadata["previous"] = data[META]
        return {**data, META: metadata}
    return {META: {field: value, "wrapped": data}}


def _unpack(data, field, fallback):
    metadata = data.get(META) if isinstance(data, dict) else None
    if isinstance(metadata, dict) and field in metadata:
        value = metadata[field]
        if not isinstance(value, str):
            raise RuntimeError("无法安全降级：Pod 身份元数据不完整，请先检查并备份数据")
        if "wrapped" in metadata:
            restored = metadata["wrapped"]
        else:
            restored = {k: v for k, v in data.items() if k != META}
            if "previous" in metadata:
                restored[META] = metadata["previous"]
        return value, restored
    return fallback, data


def _project_candidates(connection):
    resource = sa.table("resource", sa.column("resource_type"), sa.column("resource_id"),
                        sa.column("tenant_id", sa.Integer()), sa.column("project_ref", sa.Integer()))
    project = sa.table("hcs_project", sa.column("id", sa.Integer()), sa.column("account_id", sa.Integer()))
    owners = dict(connection.execute(sa.select(project.c.id, project.c.account_id)).all())
    candidates = {}
    for tenant, cid, ref in connection.execute(sa.select(
            resource.c.tenant_id, resource.c.resource_id, resource.c.project_ref,
    ).where(resource.c.resource_type == "cce_cluster")):
        # 无项目或归属错误的候选也保留为歧义，不能只选某个看似可用的项目。
        safe_ref = ref if ref is not None and tenant is not None and owners.get(ref) == tenant else None
        candidates.setdefault((tenant, cid), set()).add(safe_ref)
    return {key: next(iter(values)) for key, values in candidates.items()
            if len(values) == 1 and None not in values}


def _events(connection, table):
    last = -1
    while True:
        rows = connection.execute(sa.select(table).where(table.c.id > last).order_by(table.c.id).limit(500)).mappings().all()
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]


def upgrade() -> None:
    connection = op.get_bind()
    candidates = _project_candidates(connection)
    old_state = _state_table()
    # 0006 尚无 project_ref；仅选择原有列，保留 data 中的探针计时状态。
    states = connection.execute(sa.select(
        old_state.c.key, old_state.c.tenant_id, old_state.c.cluster_id, old_state.c.namespace,
        old_state.c.pod, old_state.c.data,
    )).mappings().all()
    targets = []
    new_keys = set()
    occupied = {row["key"] for row in states}
    for row in states:
        ref = candidates.get((row["tenant_id"], row["cluster_id"]))
        key = _hash("pod:", [row["tenant_id"], ref, row["cluster_id"], row["namespace"], row["pod"]])
        temporary = _hash("_0007:", [row["key"]])
        if key in new_keys or temporary in occupied:
            raise RuntimeError("无法升级：存在同作用域重复 Pod 状态或身份键冲突，请先备份并处理")
        new_keys.add(key)
        targets.append((row, ref, key, temporary))

    op.add_column("pod_state", sa.Column("project_ref", sa.Integer(), nullable=True))
    op.add_column("pod_event", sa.Column("project_ref", sa.Integer(), nullable=True))
    state, event = _state_table(), _event_table()
    # 两阶段改主键，避免旧键恰好与另一条记录的新键相同导致中途覆盖或约束失败。
    for row, _ref, _key, temporary in targets:
        connection.execute(state.update().where(state.c.key == row["key"]).values(key=temporary))
    for row, ref, key, temporary in targets:
        connection.execute(state.update().where(state.c.key == temporary).values(
            key=key, project_ref=ref, data=_pack(row["data"], "key", row["key"])))
    for row in _events(connection, event):
        ref = candidates.get((row["tenant_id"], row["cluster_id"]))
        key = _hash("event:", [row["tenant_id"], ref, row["cluster_id"], row["dedup_key"]])
        connection.execute(event.update().where(event.c.id == row["id"]).values(
            project_ref=ref, dedup_key=key, detail=_pack(row["detail"], "dedup", row["dedup_key"])))
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    connection = op.get_bind()
    state, event = _state_table(), _event_table()
    states = connection.execute(sa.select(state)).mappings().all()
    clusters = {}
    public_keys = set()
    targets = []
    occupied = {row["key"] for row in states}
    for row in states:
        identity = (row["tenant_id"], row["project_ref"])
        clusters.setdefault(row["cluster_id"], set()).add(identity)
        public_key = f'{row["cluster_id"]}/{row["namespace"]}/{row["pod"]}'
        key, data = _unpack(row["data"], "key", public_key)
        temporary = _hash("_0007down:", [row["key"]])
        if key in public_keys or len(key) > 320 or temporary in occupied:
            raise RuntimeError("无法安全降级：Pod 状态存在旧主键碰撞或超长键，请先备份并处理")
        public_keys.add(key)
        targets.append((row["key"], key, data, temporary))
    dedup_scopes = {}
    for row in _events(connection, event):
        identity = (row["tenant_id"], row["project_ref"])
        clusters.setdefault(row["cluster_id"], set()).add(identity)
        dedup, _detail = _unpack(row["detail"], "dedup", row["dedup_key"])
        metadata = row["detail"].get(META) if isinstance(row["detail"], dict) else None
        has_original = isinstance(metadata, dict) and "dedup" in metadata
        if len(dedup) > 160 or (dedup.startswith("event:") and not has_original):
            raise RuntimeError("无法安全降级：事件原始去重键超长或缺失，不能截断或丢弃历史")
        dedup_scopes.setdefault(dedup, set()).add((*identity, row["cluster_id"]))
    # 旧采集器仅按 cid/去重键查询，即使 Pod 名不同也可能结束另一作用域的事件。
    if any(len(values) > 1 for values in clusters.values()) or any(len(values) > 1 for values in dedup_scopes.values()):
        raise RuntimeError("无法安全降级：存在跨租户或项目的同集群/事件身份，旧版本无法隔离，请先备份并处理")

    for old_key, _key, _data, temporary in targets:
        connection.execute(state.update().where(state.c.key == old_key).values(key=temporary))
    for _old_key, key, data, temporary in targets:
        connection.execute(state.update().where(state.c.key == temporary).values(key=key, data=data))
    for row in _events(connection, event):
        dedup, detail = _unpack(row["detail"], "dedup", row["dedup_key"])
        connection.execute(event.update().where(event.c.id == row["id"]).values(dedup_key=dedup, detail=detail))
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
    with op.batch_alter_table("pod_event") as batch:
        batch.drop_column("project_ref")
    with op.batch_alter_table("pod_state") as batch:
        batch.drop_column("project_ref")
