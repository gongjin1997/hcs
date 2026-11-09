import { fmtTime } from '../../utils/format'
import type { JobReport, JobReportRow } from '../../types/job-reports'

export function factText(value: unknown): string {
  if (value == null || value === '') return '未采集'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 原生 MRS、批实例和节点时间按毫秒展示；DataArts 实时 Date 保持源值。 */
export function detailText(key: string, value: unknown, resourceType: JobReportRow['resource_type'] = 'dgc_batch_job'): string {
  if ((resourceType !== 'dgc_realtime_job') && ['start_time', 'end_time', 'plan_time', 'submitted_time', 'window_start_ms', 'window_end_ms', 'listed_last_instance_end_time', 'duration_observed_at'].includes(key)
    && typeof value === 'number' && value > 0) {
    const date = new Date(value)
    if (!Number.isNaN(date.getTime())) return `${fmtTime(date.toISOString(), 'YYYY-MM-DD HH:mm:ss')}（本地）`
  }
  return factText(value)
}

export function durationText(row: JobReportRow): string {
  const details = row.status_details
  const unit = details.elapsed_time_unit
  if (row.resource_type === 'mrs_job' && details.elapsed_time != null
    && !(typeof unit === 'string' && ['milliseconds', 'seconds'].includes(unit.toLowerCase()))
    && !['start_end_milliseconds', 'start_now_milliseconds'].includes(String(details.duration_basis))) return '时长未知（单位未确认）'
  const value = row.metrics.run_minutes
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return `${value} 分钟`
  return row.resource_type === 'mrs_job' && row.status_details.elapsed_time != null ? '时长未知（单位未确认）' : '未采集'
}

export function jobNodes(row: JobReportRow): { name: string; status: string }[] {
  const nodes = row.status_details.nodes
  if (!Array.isArray(nodes)) return []
  return nodes.filter((node): node is Record<string, unknown> => node != null && typeof node === 'object' && !Array.isArray(node))
    .map(node => ({ name: factText(node.name), status: factText(node.status) }))
}


export function executionHint(row: JobReportRow): string {
  if (row.object_level === 'INSTANCE') return '独立实例（未匹配父定义）'
  if (row.resource_type === 'dgc_batch_job' && row.execution_state == null) {
    if (row.status_details.instance_query_status === 'EMPTY') return '近24h无实例'
    if (row.status_details.instance_query_status === 'FAILED') return '实例查询失败'
  }
  return ''
}

export function emptyReportText(report: JobReport): string {
  return report.collection_status?.message || '当前范围暂无快照，采集情况未确认'
}
