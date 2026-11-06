import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type { GaussDiskApi, GaussDiskHistoryOut, GaussDiskHistoryQuery, GaussInstance } from '../../types/gaussdb'
import { errorText, rangeQuery } from './gauss-utils'

export function useGaussDiskHistory(options: {
  instance: () => GaussInstance
  tenantId: () => number | null
  canView: () => boolean
  canExport: () => boolean
  api: GaussDiskApi
  now?: () => number
}) {
  const now = options.now ?? Date.now
  const range = shallowRef<[Date, Date] | null>([new Date(now() - 86400000), new Date(now())])
  const preset = shallowRef<number | null>(24)
  const period = shallowRef(300)
  const history = shallowRef<GaussDiskHistoryOut | null>(null)
  const applied = shallowRef<GaussDiskHistoryQuery | null>(null)
  const error = shallowRef(''), exportError = shallowRef('')
  const loading = shallowRef(false), exporting = shallowRef(false)
  let controller: AbortController | null = null, exportController: AbortController | null = null
  let disposed = false
  const canExport = computed(() => options.canView() && options.canExport() && !!applied.value && !loading.value && !exporting.value)
  function cancelExport() { exportController?.abort(); exportController = null; exporting.value = false }
  function clear() {
    controller?.abort(); controller = null; cancelExport()
    history.value = null; applied.value = null; error.value = ''; exportError.value = ''; loading.value = false
  }
  async function query() {
    clear()
    if (disposed || !options.canView()) return
    let query: GaussDiskHistoryQuery
    try {
      const time = rangeQuery(range.value)
      if (Date.parse(time.end_at!) - Date.parse(time.start_at!) > 31 * 86400000) throw new Error('时间范围不能超过 31 天')
      query = { start_at: time.start_at!, end_at: time.end_at!, period_sec: period.value }
    } catch (cause) { error.value = errorText(cause, '请选择时间范围'); return }
    const currentController = new AbortController(), instance = options.instance(), tenantId = options.tenantId()
    controller = currentController; loading.value = true
    const current = () => !disposed && controller === currentController && !currentController.signal.aborted
      && options.instance().id === instance.id && options.instance().tenant_id === instance.tenant_id
      && options.tenantId() === tenantId && options.canView()
    try {
      const result = await options.api.history(instance.id, query, { tenantId, signal: currentController.signal })
      if (!current()) return
      if (result.source !== 'GAUSSDB' || result.sampling_source !== 'LOCAL_COLLECTOR'
        || result.instance.id !== instance.id || result.instance.tenant_id !== instance.tenant_id
        || result.instance.project_ref !== instance.project_ref
        || Date.parse(result.start_at) !== Date.parse(query.start_at) || Date.parse(result.end_at) !== Date.parse(query.end_at)
        || result.period_sec !== query.period_sec) throw new Error('存储历史范围不匹配，请重试')
      history.value = result; applied.value = { ...query }
    } catch (cause) { if (current()) error.value = errorText(cause, '存储历史查询失败') }
    finally { if (current()) loading.value = false }
  }
  function setRange(value: unknown) {
    range.value = Array.isArray(value) && value.length === 2 && value.every(date => date instanceof Date)
      ? [new Date(value[0].getTime()), new Date(value[1].getTime())] : null
    preset.value = null
  }
  function setPreset(hours: number) {
    if (![1, 6, 24, 168].includes(hours)) return
    const end = now()
    range.value = [new Date(end - hours * 3600000), new Date(end)]
    period.value = hours === 168 ? 3600 : 300; preset.value = hours
    return query()
  }
  async function exportExcel() {
    if (!canExport.value || !applied.value || disposed) return null
    cancelExport()
    const request = new AbortController(), instance = options.instance(), tenantId = options.tenantId()
    exportController = request; exporting.value = true; exportError.value = ''
    const current = () => !disposed && exportController === request && !request.signal.aborted
      && options.instance().id === instance.id && options.instance().tenant_id === instance.tenant_id
      && options.tenantId() === tenantId && options.canView() && options.canExport()
    try {
      const filename = await options.api.export(instance.id, { ...applied.value }, { tenantId, signal: request.signal })
      return current() ? filename : null
    } catch (cause) { if (current()) exportError.value = errorText(cause, 'Excel 导出失败'); return null }
    finally { if (exportController === request) { exportController = null; exporting.value = false } }
  }
  watch([range, period], clear, { flush: 'sync' })
  watch(() => [options.instance().id, options.instance().tenant_id, options.instance().project_ref, options.tenantId(), options.canView()], () => { void query() }, { immediate: true, flush: 'sync' })
  watch(options.canExport, value => { if (!value) cancelExport() }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; clear() })
  return { range, preset, period, history, applied, error, exportError, loading, exporting, canExport, query, setRange, setPreset, exportExcel, cancelExport }
}
