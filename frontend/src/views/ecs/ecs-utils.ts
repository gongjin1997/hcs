import type { EcsHistoryQuery, EcsInstance, EcsMetricKey, EcsMetricState, EcsRangePreset, EcsTimestampSemantics } from '../../types/ecs'

export const ECS_METRICS = [{ key: 'cpu_util', label: 'CPU 使用率' }, { key: 'mem_util', label: '内存使用率' }, { key: 'disk_util', label: '磁盘使用率' }] as const
export const ECS_PRESETS: { value: EcsRangePreset; label: string; seconds: number; interval: EcsHistoryQuery['interval_sec'] }[] = [
  { value: '1h', label: '1 小时', seconds: 3600, interval: 60 },
  { value: '6h', label: '6 小时', seconds: 21600, interval: 300 },
  { value: '24h', label: '24 小时', seconds: 86400, interval: 300 },
  { value: '7d', label: '7 天', seconds: 604800, interval: 900 },
  { value: '30d', label: '30 天', seconds: 2592000, interval: 3600 },
]
export const ECS_STATE_LABEL: Record<EcsMetricState, string> = {
  FRESH: '新鲜', STALE: '已过期', NO_DATA: '暂无样本', DISABLED: '已关闭', NOT_SELECTED: '未纳入',
  SOURCE_UNCONFIGURED: '未配置数据源', MAPPING_MISSING: '缺少指标映射', ERROR: '采集失败',
}
export const percentText = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)}%` : '—'
export function ecsTimestampLabel(meaning: EcsTimestampSemantics | undefined) {
  return meaning === 'evaluation' ? 'PromQL 求值时间（非物理采样）' : meaning === 'source_sample' ? '配置源时间（须现场核实）' : '时间语义未知'
}
export const errorText = (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback
export const intervalText = (seconds: number) => seconds % 86400 === 0 ? `${seconds / 86400} 天` : seconds % 3600 === 0 ? `${seconds / 3600} 小时` : `${seconds / 60} 分钟`
/** 防止旧接口响应把已停用来源的历史快照显示为当前指标。 */
export function currentEcsMetric(instance: EcsInstance, key: EcsMetricKey) {
  const metric = instance.latest?.[key]
  if (instance.metrics_source === 'AOM' || metric?.source === 'AOM') {
    return { value: null, source: null, observed_at: null, collected_at: null, state: 'SOURCE_UNCONFIGURED' as const }
  }
  return metric
}
export function ecsLatestReadings(instance: EcsInstance) {
  return ECS_METRICS.map(metric => {
    const reading = currentEcsMetric(instance, metric.key)
    return { ...metric, value: reading?.value ?? null, state: reading?.state ?? 'NO_DATA',
      source: reading?.source ?? null, observed_at: reading?.observed_at ?? null, collected_at: reading?.collected_at ?? null,
      unit: reading?.unit ?? '%', timestamp_semantics: reading && 'timestamp_semantics' in reading ? reading.timestamp_semantics : 'unknown' as const }
  })
}
export function ecsRangeQuery(preset: EcsRangePreset, now = Date.now()): EcsHistoryQuery {
  const range = ECS_PRESETS.find(item => item.value === preset)!
  return { start_at: new Date(now - range.seconds * 1000).toISOString(), end_at: new Date(now).toISOString(), interval_sec: range.interval }
}
