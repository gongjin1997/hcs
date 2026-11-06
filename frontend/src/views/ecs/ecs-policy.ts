import type { EcsCollectionPolicy, EcsPhase, EcsPhaseStatus, EcsPolicyMetricKey, EcsPrometheusMapping } from '../../types/ecs'

export const ECS_PHASES: { key: EcsPhase; label: string }[] = [
  { key: 'inventory', label: '台账同步' }, { key: 'metrics', label: '指标采集' },
  { key: 'inspection', label: '巡检判定' }, { key: 'quota', label: '项目配额' },
]
export function ecsPhaseReason(status: EcsPhaseStatus | undefined): string {
  if (!status) return '服务端尚未返回执行状态'
  if (status.effective_enabled) return '已生效'
  const reasons: string[] = []
  if (!status.master_enabled) reasons.push('采集总开关关闭')
  if (!status.task_enabled) reasons.push(`全局任务关闭（${status.task_code}）`)
  if (!status.configured_enabled) reasons.push('本阶段关闭')
  if (!status.supported) reasons.push('任务暂不支持')
  if (status.reason === 'invalid_schedule') reasons.push('定时配置无效')
  return reasons.join('；') || '服务端未启用'
}
export const ECS_POLICY_METRICS = [{ key: 'cpu_util', label: 'CPU 使用率' }, { key: 'mem_util', label: '内存使用率' }, { key: 'disk_util', label: '磁盘使用率' }, { key: 'boot_time', label: '启动时间' }] as const
// 与 provider 的明确百分比/秒内置表达式一致；其他表达式必须由用户声明单位。
const defaultPromQueries: Record<EcsPolicyMetricKey, string> = {
  cpu_util: '100 - avg without (cpu, mode) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
  mem_util: '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
  disk_util: '(1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|iso9660"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|iso9660"}) * 100',
  boot_time: 'node_boot_time_seconds',
}
const metricKeys = new Set<string>(ECS_POLICY_METRICS.map(item => item.key))
export function blankEcsPolicy(): EcsCollectionPolicy {
  return { inventory: { enabled: true, interval_sec: 86400 }, metrics: { enabled: true, interval_sec: 300 },
    inspection: { enabled: true, interval_sec: 120 }, quota: { enabled: false, interval_sec: 3600 },
    metric_keys: ['cpu_util', 'mem_util', 'disk_util', 'boot_time'], selection: { mode: 'all', resource_ids: [] },
    stale_after_sec: 900, aom: null, prometheus: null }
}
function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value) }
function keysOnly(value: Record<string, unknown>, allowed: string[]) { return Object.keys(value).every(key => allowed.includes(key)) }
function integer(value: unknown, min: number, max: number) { return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max }
function text(value: unknown, max = 255): value is string { return typeof value === 'string' && value.trim().length > 0 && value.length <= max }
function metricMapping(value: unknown, min = 0, max = 255): value is Partial<Record<EcsPolicyMetricKey, string>> {
  return record(value) && Object.keys(value).length >= min && Object.entries(value).every(([key, name]) => metricKeys.has(key) && text(name, max))
}
export function parseEcsPrometheus(json: string): EcsPrometheusMapping | null {
  const value: unknown = JSON.parse(json.trim() || 'null')
  if (value === null) return null
  const safeQueries = (queries: unknown): queries is Partial<Record<EcsPolicyMetricKey, string>> => metricMapping(queries, 0, 4096)
    && !Object.values(queries).some(query => /[\x00-\x1f\x7f]|https?:\/\//i.test(query))
  if (!record(value) || !keysOnly(value, ['queries', 'instance_label', 'units', 'timestamp_queries', 'label_filters'])
    || !safeQueries(value.queries ?? {}) || !safeQueries(value.timestamp_queries ?? {})
    || !metricMapping(value.units ?? {})
    || value.instance_label !== undefined && (typeof value.instance_label !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(value.instance_label))) {
    throw new Error('Prometheus 映射无效，请核对指标键、只读查询和 instance_label')
  }
  const queries = Object.fromEntries(Object.entries(value.queries ?? {}).map(([key, query]) => [key, query.trim()])) as EcsPrometheusMapping['queries']
  const timestamps = Object.fromEntries(Object.entries(value.timestamp_queries ?? {}).map(([key, query]) => [key, query.trim()]))
  const units = (value.units ?? {}) as NonNullable<EcsPrometheusMapping['units']>
  for (const [key, unit] of Object.entries(units)) {
    const allowed = key === 'boot_time' ? ['s', 'sec', 'second', 'seconds'] : ['%', 'percent', 'percentage', '百分比']
    if (!allowed.includes(unit.trim().toLowerCase())) throw new Error('Prometheus 单位无效：使用率为百分比，开机时间为秒；不自动换算')
  }
  if (Object.entries(queries).some(([key, query]) => query !== defaultPromQueries[key as EcsPolicyMetricKey] && !units[key as EcsPolicyMetricKey])) {
    throw new Error('自定义 PromQL 必须显式声明单位（使用率为 %，开机时间为 s）')
  }
  const labels = value.label_filters ?? {}
  if (!record(labels) || Object.keys(labels).length > 32 || Object.entries(labels).some(([key, label]) =>
    !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key) || !text(label, 1024) || /[\x00-\x1f\x7f]/.test(label))) {
    throw new Error('Prometheus 标签过滤须为合法标签名到非空字符串的精确匹配')
  }
  return { queries, instance_label: typeof value.instance_label === 'string' ? value.instance_label : 'instance',
    units: Object.fromEntries(Object.entries(units).map(([key, unit]) => [key, unit.trim()])),
    timestamp_queries: timestamps, label_filters: labels as Record<string, string> }
}
export function ecsPolicyPayload(policy: EcsCollectionPolicy, prometheusJson: string): EcsCollectionPolicy {
  for (const phase of ECS_PHASES) {
    if (!integer(policy[phase.key].interval_sec, 60, 2592000)) throw new Error(`${phase.label}间隔须为 60 至 2592000 的整数秒`)
  }
  if (!integer(policy.stale_after_sec, 60, 2592000)) throw new Error('过期时间须为 60 至 2592000 的整数秒')
  if (policy.metric_keys.some(key => !metricKeys.has(key)) || new Set(policy.metric_keys).size !== policy.metric_keys.length) throw new Error('指标选择无效')
  if (!['all', 'critical', 'selected'].includes(policy.selection.mode)) throw new Error('请选择有效的实例范围')
  const ids = policy.selection.resource_ids
  if (ids.length > 20000 || ids.some(id => !Number.isSafeInteger(id) || id <= 0) || new Set(ids).size !== ids.length) throw new Error('实例须使用不重复的本地资源 ID')
  let prometheus: EcsPrometheusMapping | null
  try { prometheus = parseEcsPrometheus(prometheusJson) } catch (error) { throw new Error(error instanceof SyntaxError ? 'Prometheus 映射须为有效 JSON' : (error as Error).message) }
  return { ...policy, inventory: { ...policy.inventory }, metrics: { ...policy.metrics }, inspection: { ...policy.inspection }, quota: { ...policy.quota },
    metric_keys: [...policy.metric_keys], selection: { ...policy.selection, resource_ids: [...ids] }, aom: null, prometheus }
}
