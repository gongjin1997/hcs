import type { InspectionTask, TaskRun } from '@/api'

/** Mirrors backend/app/schemas/task_execution.py; FINAL is a view group, not a status. */
export type TaskExecutionStatus = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'PARTIAL' | 'FAILED' | 'TIMEOUT' | 'INTERRUPTED' | 'CANCELLED' | 'SKIPPED'
export interface TaskProgressEvent {
  phase?: string
  phase_label?: string
  current?: number | null
  total?: number | null
  unit?: string | null
  message?: string | null
  at?: string | null
  timestamp?: string | null
  api_calls?: number | null
  resource_total?: number | null
  abnormal_total?: number | null
  status?: string | null
}
export interface MRSExecutionOptions {
  query_mode: 'active_and_recent' | 'active_only' | 'recent_only' | 'all_history'
  submitted_window_hours?: number | null
  submitted_time_begin?: number | null
  submitted_time_end?: number | null
  page_size: number
  max_pages: number
  max_items: number
  max_calls: number
  max_clusters: number
  job_offset_mode: 'page' | 'offset'
  elapsed_time_unit?: 'milliseconds' | 'seconds' | null
}
export interface TaskExecutionOptions {
  timeout_sec: number
  p90_min_samples?: number | null
  mrs?: MRSExecutionOptions | null
}
export interface TaskExecution {
  id: string
  execution_id: string
  run_id: number
  task_code: string
  task_name: string
  service: string
  tenant_id: number | null
  tenant_name: string | null
  project_ref: number | null
  project_label: string
  request_id: string
  status: TaskExecutionStatus
  trigger_type: string
  phase: string
  phase_label: string
  current: number | null
  total: number | null
  unit: string | null
  message: string | null
  created_at: string
  queued_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  deadline_at: string | null
  elapsed_sec: number
  timeout_sec: number
  options?: TaskExecutionOptions
  cancel_requested: boolean
  api_calls: number
  resource_total: number
  abnormal_total: number
  error_summary: string | null
  progress: TaskProgressEvent
  history: TaskProgressEvent[]
}
export interface TaskExecutionAdmission {
  execution_ids: string[]
  runs: TaskExecution[]
  skipped: string[]
  status: TaskExecutionStatus
  deduplicated: boolean
  request_id: string
}
export interface TaskExecutionTarget {
  tenant_id: number | null
  tenant_name: string | null
  project_ref: number | null
  project_label: string
  enabled: boolean
  executable: boolean
  reason: string | null
  active_execution_id: string | null
}
export interface TaskExecutionTargets {
  task_code: string
  task_enabled: boolean
  targets: TaskExecutionTarget[]
}
export interface TaskExecutionRequest {
  project_refs?: number[] | null
  idempotency_key?: string | null
}
export type TaskParameterValue = string | number | null
export interface TaskParameterField {
  type: string | string[]
  title?: string
  description?: string
  enum?: TaskParameterValue[]
  default?: TaskParameterValue
  minimum?: number
  exclusiveMinimum?: number
  maximum?: number
}
export interface TaskParameterSchema {
  type?: string
  additionalProperties?: boolean
  properties?: Record<string, TaskParameterField>
}
/** Task-list history keeps its numeric run ID; execution identity is additive. */
export interface TaskExecutionRun extends TaskRun {
  execution_id?: string | null
  run_id?: number | null
  request_id?: string | null
  phase?: string | null
  phase_label?: string | null
  current?: number | null
  total?: number | null
  unit?: string | null
  message?: string | null
  created_at?: string | null
  queued_at?: string | null
  heartbeat_at?: string | null
  deadline_at?: string | null
  elapsed_sec?: number | null
  timeout_sec?: number | null
  cancel_requested?: boolean
  progress?: TaskProgressEvent
  history?: TaskProgressEvent[]
}
export interface TaskDefinition extends InspectionTask {
  runs: TaskExecutionRun[]
  effective_params?: Record<string, TaskParameterValue>
  parameter_schema?: TaskParameterSchema
  latest_execution?: TaskExecution | null
  active_execution_count?: number
  queued_count?: number
}
export interface TaskDefinitionPatch {
  interval_sec?: number
  cron?: string | null
  enabled?: boolean
  aom?: null
  p90_min_samples?: number | null
  timeout_sec?: number | null
  /** Native MRS complete replacement: {} resets defaults; null is rejected. */
  mrs_options?: Record<string, TaskParameterValue>
}
export interface TaskExecutionQuery {
  task_code?: string
  tenant_id?: number
  project_ref?: number
  status?: TaskExecutionStatus
  page?: number
  page_size?: number
}
export interface TaskExecutionContext { tenantId: number | null; signal?: AbortSignal }
