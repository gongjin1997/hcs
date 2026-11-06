export type EcsMetricKey = 'cpu_util' | 'mem_util' | 'disk_util'
export type EcsPolicyMetricKey = EcsMetricKey | 'boot_time'
export type EcsMetricState = 'FRESH' | 'STALE' | 'NO_DATA' | 'DISABLED' | 'NOT_SELECTED' | 'SOURCE_UNCONFIGURED' | 'MAPPING_MISSING' | 'ERROR'
export type EcsRangePreset = '1h' | '6h' | '24h' | '7d' | '30d'
export type EcsInterval = 60 | 300 | 900 | 3600
export type EcsTimestampSemantics = 'source_sample' | 'evaluation' | 'unknown'
export interface EcsLatestMetric {
  value: number | null
  observed_at: string | null
  collected_at: string | null
  source: string | null
  unit?: '%'
  timestamp_semantics?: EcsTimestampSemantics
  state: EcsMetricState
}
export interface EcsInstance {
  id: number
  tenant_id: number | null
  tenant_name: string
  project_ref: number | null
  project_name: string | null
  resource_id: string
  name: string
  ip: string | null
  status_raw: string | null
  health: string
  critical: boolean
  monitored: boolean
  included: boolean
  metrics_enabled: boolean
  inspection_enabled: boolean
  metrics_source: string | null
  metrics_interval_sec: number
  inspection_interval_sec: number
  stale_after_sec: number
  latest: Partial<Record<EcsMetricKey, EcsLatestMetric>>
  diagnostic: { status: string; message: string | null; at: string | null; timestamp_semantics?: Partial<Record<EcsMetricKey, EcsTimestampSemantics>> } | null
}
export interface EcsInstancesQuery {
  keyword?: string
  project_ref?: number
  critical?: boolean
  monitored?: boolean
  page: number
  page_size: number
}
export interface EcsInstancesOut { items: EcsInstance[]; total: number; page: number; page_size: number }
export interface EcsRequestContext { tenantId: number | null; signal: AbortSignal }
export interface EcsHistoryQuery { start_at: string; end_at: string; interval_sec: EcsInterval; metrics?: EcsMetricKey[] }
export interface EcsHistoryPoint { at: string; average: number | null; maximum: number | null; minimum: number | null; sample_count: number }
export interface EcsHistorySeries { metric: EcsMetricKey; label: string; source: string; unit: '%'; timestamp_semantics?: EcsTimestampSemantics; points: EcsHistoryPoint[] }
export interface EcsHistoryOut {
  instance: EcsInstance
  start_at: string
  end_at: string
  interval_sec: EcsInterval
  retention_days: number
  generated_at: string
  timezone: string
  warnings: string[]
  series: EcsHistorySeries[]
}
export type EcsInstancesFetcher = (query: EcsInstancesQuery, context: EcsRequestContext) => Promise<EcsInstancesOut>
export type EcsHistoryFetcher = (id: number, query: EcsHistoryQuery, context: EcsRequestContext) => Promise<EcsHistoryOut>
export type EcsHistoryExporter = (id: number, query: EcsHistoryQuery, context: EcsRequestContext) => Promise<string>

export interface EcsStagePolicy { enabled: boolean; interval_sec: number }
export interface EcsPrometheusMapping {
  queries: Partial<Record<EcsPolicyMetricKey, string>>
  instance_label: string
  units?: Partial<Record<EcsPolicyMetricKey, string>>
  timestamp_queries?: Partial<Record<EcsPolicyMetricKey, string>>
  label_filters?: Record<string, string>
}
export interface EcsCollectionPolicy {
  inventory: EcsStagePolicy
  metrics: EcsStagePolicy
  inspection: EcsStagePolicy
  quota: EcsStagePolicy
  metric_keys: EcsPolicyMetricKey[]
  selection: { mode: 'all' | 'critical' | 'selected'; resource_ids: number[] }
  stale_after_sec: number
  /** 仅兼容清理旧配置；不接收可启用的 AOM 映射。 */
  aom: null
  prometheus: EcsPrometheusMapping | null
}
export type EcsPhase = 'inventory' | 'metrics' | 'inspection' | 'quota'
export interface EcsPhaseStatus {
  configured_enabled: boolean; master_enabled: boolean; task_code: string; task_enabled: boolean; supported: boolean; effective_enabled: boolean
  cron?: string | null
  schedule_source?: 'policy' | 'task' | 'default'
  interval_sec?: number
  reason?: 'unsupported' | 'master_disabled' | 'stage_disabled' | 'task_disabled' | 'invalid_schedule' | 'enabled'
}
export interface EcsPolicyOut {
  tenant_id: number
  resource_type: 'ecs_server'
  master_enabled: boolean
  policy: EcsCollectionPolicy
  phase_status: Record<EcsPhase, EcsPhaseStatus>
  closed_alarms?: number
}
export type EcsPolicyFetcher = (tenantId: number, context: EcsRequestContext) => Promise<EcsPolicyOut>
export type EcsPolicySaver = (tenantId: number, policy: EcsCollectionPolicy, context: EcsRequestContext) => Promise<EcsPolicyOut>
export interface EcsQuotaItem {
  key: string
  label: string
  unit: 'count' | 'vCPU' | 'MB' | 'byte'
  limit: number | null
  used: number | null
  limit_status: 'known' | 'unlimited' | 'unknown'
  usage_status: 'known' | 'unknown' | 'not_reported'
  remaining: number | null
  limit_field: string
  usage_field: string | null
  note: string | null
}
export interface EcsProjectQuota {
  tenant_id: number
  tenant_name: string
  project_ref: number
  project_id: string
  project_name: string
  region: string
  enabled: boolean
  source: 'ecs.native_limits'
  status: 'not_collected' | 'available' | 'partial' | 'refresh_failed'
  fetched_at: string | null
  source_observed_at: null
  last_attempt_at: string | null
  last_error: 'upstream_error' | 'invalid_response' | 'configuration_error' | null
  is_stale: boolean
  quotas: EcsQuotaItem[]
}
export interface EcsQuotasQuery { project_ref?: number; page: number; page_size: number }
export interface EcsQuotasOut { items: EcsProjectQuota[]; total: number; page: number; page_size: number }
