import type { TaskExecution, TaskExecutionAdmission } from '../src/types/task-executions'

export function execution(overrides: Partial<TaskExecution> = {}): TaskExecution {
  return { id: 'execution-old', execution_id: 'execution-old', run_id: 11, task_code: 'mrs_job_status', task_name: 'MRS 作业巡检', service: 'mrs',
    tenant_id: 7, tenant_name: '租户甲', project_ref: 101, project_label: '项目 A', request_id: 'request-batch-1', status: 'RUNNING', trigger_type: 'MANUAL',
    phase: 'COLLECTING', phase_label: '采集', current: 2, total: 8, unit: '个集群', message: null,
    created_at: '2026-11-10T01:00:00Z', queued_at: '2026-11-10T01:00:00Z', started_at: '2026-11-10T01:00:01Z', finished_at: null,
    heartbeat_at: '2026-11-10T01:00:05Z', deadline_at: '2026-11-10T01:05:01Z', elapsed_sec: 4, timeout_sec: 300,
    options: { timeout_sec: 300, mrs: null },
    cancel_requested: false, api_calls: 9, resource_total: 2, abnormal_total: 0, error_summary: null, progress: {}, history: [], ...overrides }
}
export function admission(overrides: Partial<TaskExecutionAdmission> = {}): TaskExecutionAdmission {
  const queued = execution({ id: 'execution-new', execution_id: 'execution-new', run_id: 12, status: 'QUEUED', started_at: null, phase: 'QUEUED', phase_label: '排队等待' })
  return { execution_ids: [queued.id], runs: [queued], skipped: [], status: 'QUEUED', deduplicated: false, request_id: queued.request_id, ...overrides }
}
