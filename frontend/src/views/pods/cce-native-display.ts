import { fmtTime } from '../../utils/format'

type JsonRecord = Record<string, unknown>
export interface NativeMetricRow {
  key: string
  name: string
  namespace: string
  cpu: string
  memory: string
  observed: string
  observedRaw: string | undefined
  window: string
  state: string
  historical: boolean
}
export interface NativeMetricSection {
  kind: 'pods' | 'nodes'
  label: string
  status: string
  hint: string
  rows: NativeMetricRow[]
}
export interface NativeOwnerRow {
  key: string
  name: string
  namespace: string
  direct: string
  top: string
  state: string
}

const labels: Record<string, string> = {
  ok: '可用', partial: '部分可用', disabled: '未启用', unsupported: '不支持',
  forbidden: '无权限', error: '查询失败', malformed: '数据无效', unknown: '未知',
}
const hints: Record<string, string> = {
  disabled: '当前租户未启用', unsupported: '集群未提供此能力', forbidden: '缺少集群只读权限',
  error: '本轮查询未成功', malformed: '响应格式无效', unknown: '尚无采集结果',
}
const blockedTimeReasons = new Set([
  'timestamp_invalid', 'window_invalid', 'window_excessive', 'timestamp_future',
  'metric_identity_invalid', 'metric_identity_duplicate', 'metric_type_invalid',
])
const ownerReasons: Record<string, string> = {
  rbac_forbidden: '无权限，顶层归属未知', api_unsupported: '不支持归属查询',
  request_failed: '查询失败，顶层归属未知', authentication_failed: '认证失败，顶层归属未知',
  incomplete_list: '列表不完整，顶层归属未知', owner_uid_mismatch: 'UID 不匹配，顶层归属未知',
  owner_not_found: '未找到对应 UID，顶层归属未知', owner_ambiguous: '归属不唯一',
  multiple_controllers: '控制器引用不唯一', owner_cycle: '归属引用循环',
  owner_chain_unsupported: '归属链不支持',
}

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}
function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}
function status(value: unknown): string {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(labels, value) ? value : 'unknown'
}
function readable(value: unknown): boolean {
  return value === 'ok' || value === 'partial'
}
function number(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined
}
function displayNumber(value: number): string {
  if (value > 0 && value < 0.000000001) return value.toExponential(3)
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 9 })
}
function observedTime(value: unknown): string | undefined {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    || !Number.isFinite(Date.parse(value))) return undefined
  return value
}

function metricSection(native: JsonRecord, metrics: JsonRecord, kind: 'pods' | 'nodes'): NativeMetricSection {
  const raw = record(metrics[kind])
  const source = Object.keys(raw).length ? raw : metrics
  const sourceStatus = status(source.status)
  const complete = source.complete === true && Array.isArray(source.items)
  const accessible = readable(metrics.status) && readable(source.status) && complete
  const rows: NativeMetricRow[] = accessible ? (source.items as unknown[]).map((item, index) => {
    const sample = record(item)
    const identity = record(sample.identity)
    const name = text(identity.name)
    const namespace = text(identity.namespace)
    const identityValid = !!name && (kind === 'nodes' ? !namespace : !!namespace)
    const observed = observedTime(sample.timestamp)
    const window = number(sample.window_seconds)
    const reason = text(sample.reason) ?? ''
    const evidence = identityValid && !!observed && window !== undefined && window > 0
      && readable(sample.status) && !blockedTimeReasons.has(reason)
    const snapshotFresh = native.freshness === undefined || native.freshness === 'fresh'
    const sourceFresh = source.freshness === undefined || source.freshness === 'fresh'
    const stale = reason === 'sample_stale' || native.freshness === 'stale' || source.freshness === 'stale'
    const current = evidence && sample.fresh === true && snapshotFresh && sourceFresh && !stale
    const historical = evidence && stale && (reason === 'sample_stale' || sample.fresh === true)
    const usable = current || historical
    const cpu = usable ? number(sample.cpu_cores) : undefined
    const memory = usable ? number(sample.memory_bytes) : undefined
    const prefix = historical ? '上次 ' : ''
    const bothValid = cpu !== undefined && memory !== undefined
    const state = historical ? '已过期（上次样本）' : !current ? '时效或样本未知'
      : bothValid ? '有效' : '部分数值未知'
    return {
      key: JSON.stringify([namespace, name, identity.uid, index]), name: name ?? '未知', namespace: namespace ?? '未知',
      cpu: cpu === undefined ? '未知' : `${prefix}${displayNumber(cpu)}`,
      memory: memory === undefined ? '未知' : `${prefix}${displayNumber(memory / 1048576)}`,
      observed: observed ? fmtTime(observed, 'YYYY-MM-DD HH:mm:ss') : '未知', observedRaw: observed,
      window: window !== undefined && window > 0 ? `${displayNumber(window)} 秒` : '未知', state, historical,
    }
  }) : []
  let state = readable(metrics.status) ? sourceStatus : status(metrics.status)
  if (readable(source.status) && !complete && state === 'ok') state = 'malformed'
  if (rows.some(row => row.state !== '有效') && state === 'ok') state = 'partial'
  const hint = !complete && readable(source.status) && readable(metrics.status) ? '列表不完整，数值未知' : hints[state] ?? (!complete ? '列表不完整，数值未知'
    : !rows.length ? '暂无指标样本' : state === 'partial' ? '含过期或未知样本' : '')
  return { kind, label: kind === 'pods' ? 'Pod 指标' : 'Node 指标', status: labels[state], hint, rows }
}

interface Identity { kind: string; namespace: string; name: string; uid: string | undefined; apiVersion: string | undefined }
function identity(value: unknown): Identity | undefined {
  const raw = record(value)
  const kind = text(raw.kind), namespace = text(raw.namespace), name = text(raw.name)
  if (!kind || !namespace || !name) return undefined
  const uid = text(raw.uid)
  return { kind, namespace, name, uid: uid && uid.length <= 128 && !/[\s\x00-\x1f\x7f]/.test(uid) ? uid : undefined,
    apiVersion: text(raw.api_version) }
}
function identityLabel(value: Identity | undefined): string {
  return value ? `${value.kind} · ${value.namespace}/${value.name}${value.uid ? '' : '（UID 缺失）'}` : '未知'
}
function apiVersionValid(value: Identity | undefined): boolean {
  if (!value?.apiVersion) return false
  const groups: Record<string, string> = { Pod: '', ReplicaSet: 'apps', Deployment: 'apps',
    StatefulSet: 'apps', DaemonSet: 'apps', Job: 'batch', CronJob: 'batch' }
  const group = groups[value.kind]
  if (group === '') return value.apiVersion === 'v1'
  const parts = value.apiVersion.split('/')
  return parts.length === 2 && (!group || parts[0] === group) && /^[a-z0-9.-]+$/.test(parts[0])
    && /^v\d+(?:(?:alpha|beta)\d+)?$/.test(parts[1])
}
function ownerRows(native: JsonRecord, owners: JsonRecord): NativeOwnerRow[] {
  if (!readable(owners.status) || !Array.isArray(owners.items)) return []
  return owners.items.map((item, index) => {
    const row = record(item)
    const pod = identity(row.pod), direct = identity(row.direct_owner), top = identity(row.top_owner)
    const reason = text(row.reason) ?? ''
    const namespace = pod?.namespace
    const uidComplete = !!pod?.uid && !!top?.uid && (!direct || !!direct.uid)
    const scopeVerified = !!namespace && top?.namespace === namespace && (!direct || direct.namespace === namespace)
    const source = direct ? record(record(owners.sources)[direct.kind]) : {}
    const intermediate = direct?.kind === 'ReplicaSet' || direct?.kind === 'Job'
    const sourceVerified = !intermediate || readable(source.status) && source.complete === true
    const expectedKind = direct?.kind === 'ReplicaSet' ? 'Deployment' : 'CronJob'
    const chainVerified = reason === 'owner_chain_verified' && intermediate && top?.kind === expectedKind
      && top.uid !== direct?.uid && top.uid !== pod?.uid
    const standalone = reason === 'owner_standalone' && intermediate && top?.uid === direct?.uid
      && top?.kind === direct?.kind && top?.name === direct?.name
    const directVerified = reason === 'direct_owner' && !intermediate && !!direct
      && top?.uid === direct.uid && top?.kind === direct.kind && top?.name === direct.name
    const unowned = reason === 'pod_unowned' && row.direct_owner === null && top?.kind === 'Pod'
      && top.uid === pod?.uid && top.name === pod?.name
    const verified = pod?.kind === 'Pod' && row.status === 'ok' && uidComplete && scopeVerified && sourceVerified
      && apiVersionValid(pod) && apiVersionValid(top) && (!direct || apiVersionValid(direct) && direct.uid !== pod?.uid)
      && (chainVerified || standalone || directVerified || unowned)
    const uidMissing = pod && !pod.uid || direct && !direct.uid || top && !top.uid
    const scopeMismatch = !!pod && (!!direct && direct.namespace !== namespace || !!top && top.namespace !== namespace)
    const oldSnapshot = native.freshness !== undefined && native.freshness !== 'fresh'
    const state = uidMissing ? 'UID 链缺失，顶层归属未知' : scopeMismatch ? '命名空间不匹配，顶层归属未知'
      : !verified ? ownerReasons[reason] ?? '归属链未核实，顶层归属未知'
        : native.freshness === 'stale' ? '上次核对（快照已过期）'
          : native.freshness === 'refresh_failed' ? '上次核对（刷新失败）'
          : native.freshness !== undefined && native.freshness !== 'fresh' ? '上次核对（快照时效未知）'
            : unowned ? '无控制器' : chainVerified ? 'UID 链已核对' : '直接引用已核对'
    return {
      key: JSON.stringify([namespace, pod?.name, pod?.uid, index]), name: pod?.name ?? '未知', namespace: namespace ?? '未知',
      direct: direct && direct.namespace === namespace ? identityLabel(direct) : direct ? '未知（命名空间不匹配）' : unowned ? '无控制器' : '未知',
      top: verified ? `${oldSnapshot ? '上次 ' : ''}${identityLabel(top)}` : '未知', state,
    }
  })
}

/** Pure presentation boundary for the optional native JSON. No fetching, clock thresholds or mutation. */
export function nativePanelData(value: Record<string, unknown>) {
  const native = record(value), metrics = record(native.metrics), owners = record(native.owners)
  const hasMetrics = Object.keys(metrics).length > 0, hasOwners = Object.keys(owners).length > 0
  const ownersStatus = status(owners.status)
  const rows = ownerRows(native, owners)
  const ownerSnapshotOld = native.freshness !== undefined && native.freshness !== 'fresh'
  const collectedAt = observedTime(native.collected_at)
  return {
    hasMetrics, hasOwners,
    metrics: hasMetrics ? [metricSection(native, metrics, 'pods'), metricSection(native, metrics, 'nodes')] : [],
    owners: {
      status: labels[ownersStatus], rows,
      hint: hints[ownersStatus] ?? (!Array.isArray(owners.items) ? '归属列表无效' : !rows.length ? '暂无 Pod 归属记录'
        : ownerSnapshotOld ? '上次归属快照，当前关系未知' : ''),
      collectedAt: collectedAt ? fmtTime(collectedAt, 'YYYY-MM-DD HH:mm:ss') : undefined,
      sources: Object.entries(record(owners.sources)).filter(([kind]) => kind === 'ReplicaSet' || kind === 'Job')
        .map(([kind, source]) => `${kind}：${labels[status(record(source).status)]}`).join(' · '),
    },
  }
}
