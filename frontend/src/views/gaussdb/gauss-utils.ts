import type { GaussNativeRow, GaussNativeStatus, GaussPanel } from '../../types/gaussdb'

export const GAUSS_PANELS: { key: GaussPanel; label: string }[] = [
  { key: 'overview', label: '概览' }, { key: 'disk', label: '磁盘' },
  { key: 'performance', label: '性能与巡检' }, { key: 'slow-sql', label: '慢 SQL' }, { key: 'sessions', label: '会话' },
]
export const NATIVE_STATUS: Record<GaussNativeStatus, string> = {
  AVAILABLE: '已获取', NO_DATA: '暂无数据', PARTIAL: '部分数据', UNAVAILABLE: '暂不可用',
  ERROR: '查询失败', DISABLED: '未启用', UNSUPPORTED: '上游不支持',
}
export const nativeObject = (value: unknown): GaussNativeRow | null =>
  value != null && typeof value === 'object' && !Array.isArray(value) ? value as GaussNativeRow : null
export const nativeText = (value: unknown): string => {
  if (value == null || value === '') return '—'
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}
export const quantityText = (value: number | null | undefined, unit: string | null | undefined): string =>
  value == null ? '—' : `${value} ${unit || '（单位未确认）'}`
/** 仅转换原生数字毫秒；UTC 日期字符串和其他字符串保留原值。 */
export function inspectionTime(value: unknown): string {
  if (typeof value !== 'number') return nativeText(value)
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : nativeText(value)
}
const TIME_LABEL: Record<string, string> = { start_time: '开始时间', finish_time: '结束时间', end_time: '结束时间', inspection_time: '巡检时间' }
export const isNativeTimeKey = (key: string): boolean => Object.prototype.hasOwnProperty.call(TIME_LABEL, key)
const isMilliseconds = (value: unknown): boolean => typeof value === 'number' && Number.isFinite(new Date(value).getTime())
export const nativeTimeTitle = (value: unknown): string => isMilliseconds(value)
  ? `原始时间：${nativeText(value)}（UTC 毫秒）` : `原生时间：${nativeText(value)}`
export function rangeQuery(range: [Date, Date] | null, required = true): { start_at?: string; end_at?: string } {
  if (!range && !required) return {}
  if (!range || range.some(date => !(date instanceof Date) || !Number.isFinite(date.getTime()))) throw new Error('请选择开始和结束时间')
  if (range[0].getTime() >= range[1].getTime()) throw new Error('结束时间须晚于开始时间')
  return { start_at: range[0].toISOString(), end_at: range[1].toISOString() }
}
/** PDF 实例为对象，字段表允许数组：只按同一下标配对，不插值或补零。 */
export function inspectionPoints(data: unknown): { time: unknown; value: unknown; block: number; index: number }[] {
  const blocks = Array.isArray(data) ? data : [data]
  return blocks.flatMap((entry, block) => {
    const item = nativeObject(entry)
    if (!item) return []
    const times = Array.isArray(item.time) ? item.time : []
    const values = Array.isArray(item.metrics) ? item.metrics : []
    return Array.from({ length: Math.max(times.length, values.length) }, (_, index) => ({
      time: times[index] ?? null, value: values[index] ?? null, block, index,
    }))
  })
}
export function inspectionIndicators(item: GaussNativeRow): GaussNativeRow[] {
  return Array.isArray(item.indicators) ? item.indicators.map(nativeObject).filter((value): value is GaussNativeRow => value !== null) : []
}
export function indicatorMetrics(item: GaussNativeRow): GaussNativeRow[] {
  return Array.isArray(item.metrics) ? item.metrics.map(nativeObject).filter((value): value is GaussNativeRow => value !== null) : []
}
export function errorText(cause: unknown, fallback: string): string { return cause instanceof Error ? cause.message : fallback }

const COLUMN_LABEL: Record<string, string> = {
  sql_id: 'SQL ID', user_name: '用户', node_id: '节点 ID', node_name: '节点', role: '角色', component_type: '组件类型',
  sql_text: 'SQL 模板', query_plan: '执行计划', calls: '执行次数', avg_exec_time: '平均执行耗时',
  avg_cpu_time: '平均 CPU 耗时', avg_io_time: '平均 I/O 耗时', avg_returned_rows: '平均返回行数',
  avg_fetched_rows: '平均扫描行数', buffer_hit_ratio: '缓冲命中率', sql_hit_ratio: 'SQL 命中率',
  record_id: '巡检记录 ID', instance_id: '实例 ID', status: '状态', session_id: '会话 ID', database_name: '数据库',
  client_ip: '客户端 IP', state: '状态', wait: '等待', query: 'SQL', query_id: '查询 ID', pid: '进程 ID',
  exec_time: '会话建立时长', query_runtime: '语句执行时长（原始字符串）',
  fetched_pages: '扫描页数', hit_pages: '命中页数', total_time: '总耗时', cpu_time: 'CPU 耗时',
  plan_time: '计划耗时', io_time: 'I/O 耗时', lock_time: '加锁耗时', lock_count: '加锁次数',
  total_count: '检查项数', normal_count: '正常项数', abnormal_count: '异常项数', failed_count: '失败项数',
  progress: '进度', instance_name: '实例', inspection_policy: '巡检策略', inspection_scope: '巡检方式', task_name: '任务',
  ...TIME_LABEL,
}
const MICROSECOND_FIELDS = ['avg_exec_time', 'avg_cpu_time', 'avg_io_time', 'total_time', 'cpu_time', 'plan_time', 'io_time', 'lock_time']
export function nativeColumnLabel(key: string, units: Record<string, string> = {}, values: unknown[] = []): string {
  if (isNativeTimeKey(key)) {
    const present = values.filter(value => value != null && value !== '')
    const local = present.filter(isMilliseconds).length
    return `${TIME_LABEL[key]}（${local ? local === present.length ? '本地' : '本地 / 原生时间' : '原生时间'}）`
  }
  const unit = units[key] || (MICROSECOND_FIELDS.includes(key) ? 'μs' : key === 'exec_time' ? '秒' : '')
  return `${COLUMN_LABEL[key] || key}${unit ? `（${unit}）` : ''}`
}
export function nativeColumns(rows: GaussNativeRow[]): string[] {
  const keys = [...new Set(rows.flatMap(row => Object.keys(row)))]
  const priority = Object.keys(COLUMN_LABEL)
  return keys.sort((a, b) => {
    const left = priority.indexOf(a), right = priority.indexOf(b)
    return (left < 0 ? priority.length : left) - (right < 0 ? priority.length : right)
  })
}
