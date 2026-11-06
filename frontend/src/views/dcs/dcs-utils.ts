import type { DcsHistoryOut, DcsHistoryQuery, DcsInstance, DcsIntervalMinutes, DcsMemoryPoint, DcsMemorySeries, DcsNode, DcsRangePreset,
  DcsTopology, DcsTopologyState } from '../../types/dcs'

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

// ---------------- 分片与节点（DCS API §4.3.3 批量查询实例节点信息） ----------------
export const NODE_STATUS_TEXT: Record<string, string> = {
  Creating: '创建中', Active: '运行中', Inactive: '故障', Deleting: '删除中', AddSharding: '添加分片中',
}
export const NODE_ROLE_TEXT: Record<string, string> = { master: '主节点', slave: '从节点', proxy: 'Proxy' }
export const TOPOLOGY_STATE_TEXT: Record<DcsTopologyState, string> = {
  AVAILABLE: '已获取', NOT_RETURNED: '源端未返回节点', NOT_SUPPORTED: '不支持节点查询',
  STALE: '本轮获取失败，显示上次结果', UNAVAILABLE: '节点获取失败', NOT_QUERIED: '未查询',
}
type TagType = 'success' | 'warning' | 'danger' | 'info'

export const nodeStatusType = (status: string | null): TagType =>
  status === 'Active' ? 'success' : status === 'Inactive' ? 'danger' : status && status in NODE_STATUS_TEXT ? 'warning' : 'info'

/** 一行拓扑摘要，例如“3 分片 · 3 主 3 从 · 2 Proxy”；没有节点数据时说明原因。 */
export function topologyText(topology: DcsTopology | null | undefined): string {
  if (!topology) return '—'
  if (topology.state !== 'AVAILABLE' && topology.state !== 'STALE') return TOPOLOGY_STATE_TEXT[topology.state]
  const roles = [topology.masters ? `${topology.masters} 主` : '', topology.replicas ? `${topology.replicas} 从` : ''].filter(Boolean).join(' ')
  const parts = [topology.shards != null ? `${topology.shards} 分片` : '', roles, topology.proxies ? `${topology.proxies} Proxy` : '']
  return parts.filter(Boolean).join(' · ') || `${topology.node_count ?? 0} 个节点`
}

/** 节点健康标签：主节点故障最严重；过期的结果只作参考。 */
export function topologyHealth(topology: DcsTopology | null | undefined): { type: TagType; text: string } {
  if (!topology || !['AVAILABLE', 'STALE'].includes(topology.state)) {
    return { type: 'info', text: topology?.state === 'UNAVAILABLE' ? '获取失败' : '无节点数据' }
  }
  const stale = topology.freshness !== 'FRESH'
  const result: { type: TagType; text: string } = topology.inactive_masters ? { type: 'danger', text: `${topology.inactive_masters} 个主节点故障` }
    : topology.inactive ? { type: 'warning', text: `${topology.inactive} 个节点故障` }
      : topology.unknown ? { type: 'warning', text: `${topology.unknown} 个状态未识别` }
        : topology.transitional ? { type: 'info', text: `${topology.transitional} 个变更中` }
          : { type: 'success', text: '全部运行中' }
  return stale ? { type: 'info', text: `${result.text}（已过期）` } : result
}

const ROLE_ORDER: Record<string, number> = { master: 0, slave: 1, proxy: 2 }
const naturally = (a: string, b: string) => a.localeCompare(b, 'zh-CN', { numeric: true })

/** 节点按分片排列：Redis 节点在前（同分片主节点在前），Proxy 节点在后。 */
export function sortNodes(nodes: DcsNode[]): DcsNode[] {
  return [...nodes].sort((a, b) => Number(a.kind === 'redis-proxy') - Number(b.kind === 'redis-proxy')
    || naturally(a.shard ?? '', b.shard ?? '') || (ROLE_ORDER[a.role ?? ''] ?? 3) - (ROLE_ORDER[b.role ?? ''] ?? 3)
    || naturally(a.name, b.name))
}
