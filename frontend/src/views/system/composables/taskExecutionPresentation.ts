import type { TaskExecution, TaskExecutionStatus, TaskProgressEvent } from '@/types/task-executions'

const statuses = new Map<string, { label: string; type: 'info' | 'primary' | 'success' | 'warning' | 'danger' }>([
  ['QUEUED', { label: '排队中', type: 'info' }], ['RUNNING', { label: '巡检中', type: 'primary' }],
  ['SUCCESS', { label: '成功', type: 'success' }], ['PARTIAL', { label: '部分完成', type: 'warning' }],
  ['FAILED', { label: '失败', type: 'danger' }], ['TIMEOUT', { label: '超时', type: 'danger' }],
  ['INTERRUPTED', { label: '已中断', type: 'danger' }], ['CANCELLED', { label: '已取消', type: 'info' }],
  ['SKIPPED', { label: '已跳过', type: 'info' }],
])
const activeStatuses = new Set<string>(['QUEUED', 'RUNNING'])
const finalStatuses = new Set<string>(['SUCCESS', 'PARTIAL', 'FAILED', 'TIMEOUT', 'INTERRUPTED', 'CANCELLED', 'SKIPPED'])
const retryableStatuses = new Set<string>(['FAILED', 'PARTIAL', 'TIMEOUT', 'INTERRUPTED', 'CANCELLED'])
export const executionStatus = (status: string) => statuses.get(status) ?? { label: status, type: 'info' as const }
export const isActiveExecution = (status: string): boolean => activeStatuses.has(status)
export const isFinalExecution = (status: string): status is TaskExecutionStatus => finalStatuses.has(status)
export const isRetryableExecution = (status: string): boolean => retryableStatuses.has(status)
export function executionPercentage(execution: Pick<TaskExecution, 'current' | 'total'>): number | null {
  const { current, total } = execution
  if (typeof current !== 'number' || !Number.isFinite(current) || current < 0 || typeof total !== 'number' || !Number.isFinite(total) || total <= 0) return null
  return Math.min(100, Math.round(current / total * 100))
}
export function executionCount(value: unknown): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : '—'
}
export function executionSeconds(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${Math.round(value * 10) / 10} 秒` : '—'
}
export function executionProgressContext(progress: Pick<TaskProgressEvent, 'cluster_id' | 'query'> | null | undefined): string {
  if (!progress) return ''
  const parts: string[] = []
  if (progress.cluster_id) parts.push(`集群 ${progress.cluster_id}`)
  if (progress.query) {
    const label = progress.query === 'recent' ? '近期提交作业'
      : progress.query === 'all_history' ? '全历史作业'
        : progress.query.startsWith('active_') ? `活动作业：${progress.query.slice(7)}` : progress.query
    parts.push(label)
  }
  return parts.join(' · ')
}
