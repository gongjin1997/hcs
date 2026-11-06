/** 与 backend/app/schemas/gaussdb.py 对齐；原生诊断内容不换算、不补值。 */
export type GaussNativeStatus = 'AVAILABLE' | 'NO_DATA' | 'PARTIAL' | 'UNAVAILABLE' | 'ERROR' | 'DISABLED' | 'UNSUPPORTED'
export type GaussPanel = 'overview' | 'performance' | 'slow-sql' | 'sessions'
export type GaussStatusGroup = 'abnormal' | 'transitional' | 'stopped' | 'normal'
export type GaussFreshness = 'FRESH' | 'STALE' | 'NO_DATA'
export type GaussExportPanel = 'overview' | 'slow-sql' | 'slow-sql-detail' | 'sessions' | 'inspections' | 'inspection-detail'
export type GaussNativeRow = Record<string, unknown>

export interface GaussInstance {
  id: number
  resource_id: string
  name: string
  tenant_id: number | null
  tenant_name: string
  project_ref: number | null
  project_name: string | null
  region: string | null
  status_raw: string | null
  health: string
  scope_excluded?: boolean
  vcpus: number | null
  memory_size: number | null
  memory_unit: string | null
  storage_size: number | null
  storage_unit: string | null
  snapshot_at: string | null
  // 以下来自本地巡检记录（最近一次采集），不请求云
  status_group?: GaussStatusGroup
  status_since?: string | null
  deploy_type?: string | null
  version?: string | null
  disk_usage?: number | null
  disk_used_gb?: number | null
  disk_total_gb?: number | null
  disk_state?: 'AVAILABLE' | 'PARTIAL' | 'ERROR' | 'UNAVAILABLE' | null
  /** 本地收到 volume-usage 响应的时间 */
  disk_observed_at?: string | null
  disk_freshness?: GaussFreshness
  node_count?: number | null
  components?: GaussComponentSummary | null
  active_alarms?: number
  /** 只在单实例详情返回 */
  nodes?: GaussNode[] | null
  component_items?: GaussComponent[] | null
}
export interface GaussComponentSummary {
  state: 'AVAILABLE' | 'NO_DATA' | 'STALE' | 'ERROR'
  observed_at: string | null
  freshness: GaussFreshness
  total: number | null
  down: number | null
  unknown: number | null
  by_type: Record<string, number>
  warning: string | null
}
export interface GaussNode { name: string | null; role: string | null; status: string | null }
export interface GaussSummary {
  total: number; abnormal: number; transitional: number; stopped: number; normal: number; scope_excluded: number
  disk_high: number; components_down: number; active_alarms: number; last_collected_at: string | null
}
export interface GaussInstancesOut { items: GaussInstance[]; total: number; page: number; page_size: number }
export interface GaussInstancesQuery { keyword?: string; project_ref?: number; status_group?: GaussStatusGroup; page: number; page_size: number }
export interface GaussProjectOption { project_ref: number; project_name: string; tenant_id: number; tenant_name: string; region: string | null }
export interface GaussDirectoryApi {
  instance: (id: number, context: GaussRequestContext) => Promise<GaussInstance>
  projects: (context: GaussRequestContext) => Promise<GaussProjectOption[]>
}
export interface GaussComponent {
  node_id: string; node_name: string | null; component_id: string; component_type: string
  role: string | null; status: string | null
  availability_zone_id?: string | null; distributed_id?: string | null
}
export interface GaussDisk {
  status: GaussNativeStatus
  used_gb: number | null
  total_gb: number | null
  usage_percent: number | null
  observed_at: string | null
  collected_at?: string | null
  time_basis?: 'RESPONSE_RECEIVED' | null
  warning: string | null
}
export interface GaussOverviewOut {
  instance: GaussInstance
  source: 'GAUSSDB'
  queried_at: string
  disk: GaussDisk
  components: GaussComponent[]
  components_status: GaussNativeStatus
  components_warning: string | null
  cpu_usage: null
  memory_usage: null
  performance_note: string
}
export interface GaussNativeRows {
  source: 'GAUSSDB'
  status: GaussNativeStatus
  queried_at: string
  items: GaussNativeRow[]
  total: number | null
  truncated: boolean
  warning: string | null
  units: Record<string, string>
  start_at: string | null
  end_at: string | null
  record_id: string | null
  metric_series?: GaussMetricSeries[] | null
}
export interface GaussMetricSeries {
  metric: 'cpu' | 'memory' | 'other'
  check_name: string | null
  check_item: string | null
  check_status: string | null
  name: string
  unit: string | null
  node_name: string | null
  node_ip: string | null
  min: number | null
  max: number | null
  average: number | null
  points: { observed_at: string; value: number | null }[]
}
export interface GaussDiskHistoryQuery { start_at: string; end_at: string; period_sec: number }
export interface GaussDiskHistoryOut extends GaussDiskHistoryQuery {
  instance: GaussInstance
  source: 'GAUSSDB'
  sampling_source: 'LOCAL_COLLECTOR'
  time_basis?: 'RESPONSE_RECEIVED'
  status: GaussNativeStatus
  queried_at: string
  warning: string | null
  points: { timestamp: string; observed_at: string | null; collected_at?: string | null; used_gb: number | null; total_gb: number | null; usage_percent: number | null }[]
}
export interface GaussDiskApi {
  history: (id: number, query: GaussDiskHistoryQuery, context: GaussRequestContext) => Promise<GaussDiskHistoryOut>
  export: (id: number, query: GaussDiskHistoryQuery, context: GaussRequestContext) => Promise<string>
}
export interface GaussTimeQuery { start_at?: string; end_at?: string }
export interface GaussSlowQuery {
  start_at: string; end_at: string; node_ids: string[]; threshold: number; sql_id?: string
}
export type GaussSlowDetailQuery = Pick<GaussSlowQuery, 'start_at' | 'end_at' | 'node_ids'> & { sql_id: string }
export interface GaussSessionsQuery {
  node_id: string; component_id: string; limit: number
  database_name?: string; user_name?: string; client_ip?: string; state?: string; wait?: 'f' | 't'
}
export interface GaussInspectionsQuery extends GaussTimeQuery { limit: number; offset: number }
export type GaussExportQuery = { panel: GaussExportPanel; record_id?: string } & Partial<GaussSlowQuery & GaussSessionsQuery & GaussInspectionsQuery>
export interface GaussRequestContext { tenantId: number | null; signal: AbortSignal }
export interface GaussApi {
  instances: (query: GaussInstancesQuery, context: GaussRequestContext) => Promise<GaussInstancesOut>
  /** 当前范围的本地概况；不请求云 */
  summary: (query: { project_ref?: number }, context: GaussRequestContext) => Promise<GaussSummary>
  overview: (id: number, context: GaussRequestContext) => Promise<GaussOverviewOut>
  slowNodes: (id: number, context: GaussRequestContext) => Promise<GaussNativeRows>
  slowSql: (id: number, query: GaussSlowQuery, context: GaussRequestContext) => Promise<GaussNativeRows>
  slowDetail: (id: number, query: GaussSlowDetailQuery, context: GaussRequestContext) => Promise<GaussNativeRows>
  sessions: (id: number, query: GaussSessionsQuery, context: GaussRequestContext) => Promise<GaussNativeRows>
  inspections: (id: number, query: GaussInspectionsQuery, context: GaussRequestContext) => Promise<GaussNativeRows>
  inspectionDetail: (id: number, recordId: string, context: GaussRequestContext) => Promise<GaussNativeRows>
  export: (id: number, query: GaussExportQuery, context: GaussRequestContext) => Promise<string>
}
