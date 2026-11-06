/** 与 backend/app/schemas/job_report.py 对齐；状态、指标均由后端已保存事实提供。 */
export type JobPanel = 'dgc' | 'flink' | 'realtime' | 'batch' | 'mrs'
export type JobResourceType = 'dgc_realtime_job' | 'dgc_batch_job' | 'mrs_job'

export interface JobReportRow {
  id: number
  resource_id: string
  name: string
  service: string
  resource_type: JobResourceType
  job_type: string | null
  engine: string | null
  status_raw: string | null
  execution_state: string | null
  display_status: string
  status_label: string
  status_known: boolean
  health: string
  lifecycle: string
  tenant_id: number | null
  tenant_name: string | null
  project_ref: number | null
  project_id: string | null
  project_name: string | null
  region: string | null
  updated_at: string
  status_since: string | null
  status_details: Record<string, unknown>
  metrics: Record<string, unknown>
  active_alarms: number
}

export interface JobReportSummary {
  total: number
  status_counts: Record<string, number>
  unknown_total: number
  no_data_total: number
  active_alarms: number
  latest_update_at: string | null
  data_status: 'AVAILABLE' | 'NO_DATA'
  by_source: Record<string, number>
  by_type: Record<string, number>
  by_engine: Record<string, number>
}

export interface JobReport {
  panel: JobPanel
  summary: JobReportSummary
  items: JobReportRow[]
  total: number
  page: number
  page_size: number
  data_notes: string[]
}

export interface JobReportQuery {
  keyword?: string
  status?: string[]
  status_raw?: string[]
  engine?: string
  resource_type?: JobResourceType
  job_type?: string
  project_ref?: number
  lifecycle?: 'ACTIVE' | 'DELETED' | 'ALL'
  has_alarms?: boolean
  page: number
  page_size: number
}

export interface JobReportRequest {
  signal: AbortSignal
  tenantId: number | null
}

export type JobReportFetcher = (panel: JobPanel, query: JobReportQuery, request: JobReportRequest) => Promise<JobReport>

export type JobReportExporter = (panel: JobPanel, query: JobReportQuery, request: JobReportRequest) => Promise<string>
