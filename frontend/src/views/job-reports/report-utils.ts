import { fmtTime } from '../../utils/format'
import type { JobReportRow } from '../../types/job-reports'

export function factText(value: unknown): string {
  if (value == null || value === '') return '未采集'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 批实例与 MRS 节点的时间字段为毫秒时间戳（DataArts 表 5-178 / 5-182）；实时 Date 保持源值。 */
export function detailText(key: string, value: unknown, resourceType: JobReportRow['resource_type'] = 'dgc_batch_job'): string {
  if ((resourceType === 'dgc_batch_job' || resourceType === 'mrs_job') && ['start_time', 'end_time', 'plan_time', 'submitted_time'].includes(key)
    && typeof value === 'number' && value > 0) {
    const date = new Date(value)
    if (!Number.isNaN(date.getTime())) return `${fmtTime(date.toISOString(), 'YYYY-MM-DD HH:mm:ss')}（本地）`
  }
  return factText(value)
}

export function durationText(row: JobReportRow): string {
  const value = row.metrics.run_minutes
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${value} 分钟` : '未采集'
}

export function jobNodes(row: JobReportRow): { name: string; status: string }[] {
  const nodes = row.status_details.nodes
  if (!Array.isArray(nodes)) return []
  return nodes.filter((node): node is Record<string, unknown> => node != null && typeof node === 'object' && !Array.isArray(node))
    .map(node => ({ name: factText(node.name), status: factText(node.status) }))
}
