export type DcsIntervalMinutes = 5 | 10 | 15 | 30 | 60
export type DcsRangePreset = '1d' | '2d' | '7d' | 'custom'
export type DcsMetricKey = 'memory_usage' | 'used_memory_mb' | 'max_memory_mb' | 'used_cpu_seconds'
  | 'input_kbps' | 'output_kbps' | 'keys' | 'cmd_get_count' | 'cmd_set_count'
export type DcsChartMetric = DcsMetricKey | 'memory_max'
/** AOM / CES 仅兼容旧响应；当前历史只返回 DCS 接口采样或无数据。 */
export type DcsHistorySource = 'AOM' | 'LOCAL' | 'NONE' | 'CES'

export interface DcsInstance {
  id: number
  resource_id: string
  name: string
  tenant_id: number | null
  tenant_name: string
  project_ref: number | null
  project_name: string | null
  region: string | null
  status_raw: string | null
  memory_usage: number | null
  used_memory_mb: number | null
  max_memory_mb: number | null
  snapshot_at: string | null
  used_cpu_seconds?: number | null
  input_kbps?: number | null
  output_kbps?: number | null
  keys?: number | null
  cmd_get_count?: number | null
  cmd_set_count?: number | null
  metrics_source?: 'DCS' | 'NONE'
  api_version?: 'v2' | null
  source_endpoint?: 'instances' | 'instances/statistic' | null
  metrics_observed_at?: string | null
  time_basis?: 'RESPONSE_RECEIVED' | null
  freshness?: 'FRESH' | 'STALE' | 'NO_DATA' | 'LEGACY'
  sample_age_sec?: number | null
  stale_after_sec?: number
  warning?: string | null
}

export interface DcsInstancesOut {
  items: DcsInstance[]
  total: number
  page: number
  page_size: number
}

export interface DcsInstancesQuery {
  keyword?: string
  page: number
  page_size: number
  tenant_id?: number
  project_ref?: number
}

export interface DcsMemoryPoint {
  at: string
  memory_usage: number | null
  memory_max: number | null
  used_memory_mb: number | null
  max_memory_mb: number | null
  sample_count: number
  used_cpu_seconds?: number | null
  input_kbps?: number | null
  output_kbps?: number | null
  keys?: number | null
  cmd_get_count?: number | null
  cmd_set_count?: number | null
  last_observed_at?: string | null
  metric_sample_counts?: Partial<Record<DcsMetricKey, number>>
}

export interface DcsMemorySeries {
  instance: DcsInstance
  source: DcsHistorySource
  status: 'AVAILABLE' | 'PARTIAL' | 'NO_DATA'
  warning: string | null
  valid_points: number
  total_points: number
  summary: {
    average_usage: number | null
    peak_usage: number | null
    latest_usage: number | null
    latest_at: string | null
  }
  summaries?: Partial<Record<DcsMetricKey, DcsMetricSummary>>
  points: DcsMemoryPoint[]
}

export interface DcsHistoryOut {
  start_at: string
  end_at: string
  interval_minutes: DcsIntervalMinutes
  timezone: string
  demo: boolean
  generated_at: string
  metrics?: DcsMetricDefinition[]
  series: DcsMemorySeries[]
}

export interface DcsHistoryQuery {
  resource_ids: number[]
  start_at: string
  end_at: string
  interval_minutes: DcsIntervalMinutes
}

export interface DcsRequestContext {
  signal: AbortSignal
  tenantId: number | null
  projectRef?: number | null
}

export type DcsInstancesFetcher = (query: DcsInstancesQuery, context: DcsRequestContext) => Promise<DcsInstancesOut>
export type DcsInstanceFetcher = (id: number, context: DcsRequestContext) => Promise<DcsInstance>
export type DcsHistoryFetcher = (query: DcsHistoryQuery, context: DcsRequestContext) => Promise<DcsHistoryOut>
export type DcsHistoryExporter = (query: DcsHistoryQuery, context: DcsRequestContext) => Promise<string>


export interface DcsMetricDefinition {
  key: DcsMetricKey
  label: string
  unit: string
  aggregation: 'mean' | 'last'
}

export interface DcsMetricSummary {
  average: number | null
  peak: number | null
  latest: number | null
  latest_at: string | null
  sample_count: number
}
