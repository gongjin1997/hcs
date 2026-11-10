import type { TaskParameterField, TaskParameterSchema, TaskParameterValue } from '@/types/task-executions'

export function taskParameterErrors(values: Record<string, TaskParameterValue>, schema: TaskParameterSchema): string[] {
  const fields = schema.properties ?? {}
  const errors: string[] = []
  for (const [key, value] of Object.entries(values)) {
    const field = Object.prototype.hasOwnProperty.call(fields, key) ? fields[key] : undefined
    if (!field) { errors.push(`参数 ${key} 未开放编辑`); continue }
    const types = Array.isArray(field.type) ? field.type : [field.type]
    const title = field.title ?? key
    const valid = value === null ? types.includes('null') : typeof value === 'number'
      ? Number.isFinite(value) && (types.includes('number') || types.includes('integer') && Number.isInteger(value))
      : typeof value === 'string' && types.includes('string')
    if (!valid) { errors.push(`${title}的类型不正确`); continue }
    if (field.enum && !field.enum.includes(value)) errors.push(`${title}不在允许选项内`)
    if (typeof value === 'number' && (field.minimum !== undefined && value < field.minimum
      || field.exclusiveMinimum !== undefined && value <= field.exclusiveMinimum || field.maximum !== undefined && value > field.maximum)) errors.push(`${title}超出后端允许范围`)
  }
  if (values.submitted_time_end !== undefined && values.submitted_time_begin === undefined) errors.push('显式时间窗需要填写起点')
  if (typeof values.submitted_time_end === 'number' && typeof values.submitted_time_begin === 'number' && values.submitted_time_end < values.submitted_time_begin) errors.push('显式时间窗终点不能早于起点')
  return errors
}

export function taskParameterUpdate(values: Record<string, TaskParameterValue>, key: string, value: TaskParameterValue | undefined,
  fields: Record<string, TaskParameterField>): Record<string, TaskParameterValue> {
  if (!Object.prototype.hasOwnProperty.call(fields, key)) return values
  const next = { ...values }
  const spec = fields[key]!
  if (value === undefined || value === null && !(Array.isArray(spec.type) && spec.type.includes('null'))) delete next[key]
  else next[key] = value
  if (key === 'query_mode' && (value === 'active_only' || value === 'all_history')) {
    delete next.submitted_time_begin; delete next.submitted_time_end; delete next.submitted_window_hours
  } else if (key === 'query_mode' && (value === 'active_and_recent' || value === 'recent_only')
    && next.submitted_time_begin === undefined && next.submitted_window_hours === undefined) {
    const hours = fields.submitted_window_hours?.default
    if (hours !== undefined) next.submitted_window_hours = hours
  }
  if ((key === 'submitted_time_begin' || key === 'submitted_time_end') && value !== undefined && value !== null) delete next.submitted_window_hours
  if (key === 'submitted_window_hours' && value !== undefined && value !== null) { delete next.submitted_time_begin; delete next.submitted_time_end }
  return next
}
