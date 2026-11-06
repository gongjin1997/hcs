import type { DcsHistoryOut, DcsHistoryQuery, DcsInstance, DcsIntervalMinutes, DcsMemoryPoint, DcsMemorySeries, DcsRangePreset } from '../../types/dcs'

export const INTERVAL_MINUTES: DcsIntervalMinutes[] = [5, 10, 15, 30, 60]
export const MAX_INSTANCES = 20
export const DCS_SOURCE_LABELS: Record<DcsMemorySeries['source'], string> = {
  AOM: 'AOM（已停用，历史）', LOCAL: 'DCS 接口采样', NONE: '无数据源', CES: 'CES（历史兼容）',
}
const DAY = 86400000

export const metricText = (value: unknown, suffix = '') =>
  typeof value === 'number' && Number.isFinite(value) ? `${Number(value.toFixed(2))}${suffix}` : '—'

export function historyQuery(ids: number[], preset: DcsRangePreset, custom: [Date | null, Date | null], interval: DcsIntervalMinutes, now: number): DcsHistoryQuery {
  const unique = [...new Set(ids)]
  if (!unique.length) throw new Error('请选择实例')
  if (unique.length > MAX_INSTANCES) throw new Error('最多选择 20 个实例')
  if (unique.some(id => !Number.isSafeInteger(id) || id < 1)) throw new Error('实例 ID 无效')
  if (!INTERVAL_MINUTES.includes(interval)) throw new Error('请选择有效的聚合粒度')
  const days = { '1d': 1, '2d': 2, '7d': 7 }
  const [start, end] = preset === 'custom' ? custom : [new Date(now - days[preset] * DAY), new Date(now)]
  if (!(start instanceof Date) || !(end instanceof Date) || !Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) {
    throw new Error('请选择有效的起止时间')
  }
  if (start.getTime() >= end.getTime()) throw new Error('结束时间须晚于开始时间')
  if (end.getTime() - start.getTime() > 31 * DAY) throw new Error('查询范围不能超过 31 天')
  return { resource_ids: unique, start_at: start.toISOString(), end_at: end.toISOString(), interval_minutes: interval }
}

export const cloneHistoryQuery = (query: DcsHistoryQuery): DcsHistoryQuery => ({ ...query, resource_ids: [...query.resource_ids] })

export function assertHistoryScope(data: DcsHistoryOut, query: DcsHistoryQuery, tenantId: number | null, projectRef: number | null) {
  if (Date.parse(data.start_at) !== Date.parse(query.start_at) || Date.parse(data.end_at) !== Date.parse(query.end_at) || data.interval_minutes !== query.interval_minutes) {
    throw new Error('返回的时间范围或粒度不匹配，请重试')
  }
  const seen = new Set<number>()
  for (const series of data.series) {
    if (!query.resource_ids.includes(series.instance.id) || seen.has(series.instance.id)
      || (tenantId != null && series.instance.tenant_id !== tenantId)
      || (projectRef != null && series.instance.project_ref !== projectRef)) {
      throw new Error('返回的实例范围不匹配，请重新查询')
    }
    seen.add(series.instance.id)
  }
}

export type DcsMetricRow = DcsMemoryPoint & { key: string; instance: DcsMemorySeries['instance']; source: DcsMemorySeries['source'] }

export function metricPage(series: DcsMemorySeries[], page: number, size: number): DcsMetricRow[] {
  let skip = (page - 1) * size
  const rows: DcsMetricRow[] = []
  for (const item of series) {
    if (skip >= item.points.length) { skip -= item.points.length; continue }
    const points = item.points.slice(skip, skip + size - rows.length)
    rows.push(...points.map((point, index) => ({ ...point, key: `${item.instance.id}:${skip + index}`, instance: item.instance, source: item.source })))
    skip = 0
    if (rows.length >= size) break
  }
  return rows
}


export function freshnessState(instance: DcsInstance, now = Date.now()): NonNullable<DcsInstance['freshness']> {
  if (instance.freshness === 'LEGACY') return 'LEGACY'
  if (instance.metrics_source !== 'DCS' || instance.api_version !== 'v2'
    || instance.time_basis !== 'RESPONSE_RECEIVED' || !instance.metrics_observed_at) return 'NO_DATA'
  const age = (now - Date.parse(instance.metrics_observed_at)) / 1000
  if (!Number.isFinite(age) || age < -30) return 'NO_DATA'
  if (age > (instance.stale_after_sec ?? 900)) return 'STALE'
  return instance.freshness === 'FRESH' ? 'FRESH' : instance.freshness ?? 'NO_DATA'
}
