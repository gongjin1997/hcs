import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type { JobPanel, JobReport, JobReportExporter, JobReportFetcher, JobReportQuery } from '../../types/job-reports'

type Filters = Pick<JobReportQuery, 'keyword' | 'status' | 'engine'>
const emptyQuery = (): JobReportQuery => ({ keyword: '', status: [], engine: '', page: 1, page_size: 20 })
const cloneQuery = (query: JobReportQuery): JobReportQuery => ({ ...query, status: [...(query.status ?? [])] })

/** 报表与统计原子替换；范围变更同步清空并取消旧请求，定时刷新不重叠。 */
export function useJobReport(options: { panel: () => JobPanel; tenantId: () => number | null; fetcher: JobReportFetcher; exporter: JobReportExporter }) {
  const query = shallowRef<JobReportQuery>(emptyQuery())
  const report = shallowRef<JobReport | null>(null)
  const loading = shallowRef(false)
  const error = shallowRef('')
  const fetchedAt = shallowRef<string | null>(null)
  const autoRefresh = shallowRef(true)
  const exporting = shallowRef(false)
  const exportError = shallowRef('')
  const rows = computed(() => report.value?.items ?? [])
  let disposed = false
  let generation = 0
  let request: AbortController | undefined
  let exportRequest: AbortController | undefined
  let debounce: ReturnType<typeof setTimeout> | undefined

  function cancelExport() {
    exportRequest?.abort()
    exportRequest = undefined
    exporting.value = false
  }

  function invalidate() {
    generation++
    request?.abort()
    clearTimeout(debounce)
    debounce = undefined
  }

  async function refresh() {
    if (disposed) return
    invalidate()
    const token = generation
    const controller = new AbortController()
    request = controller
    loading.value = true
    error.value = ''
    try {
      const result = await options.fetcher(options.panel(), cloneQuery(query.value), { signal: controller.signal, tenantId: options.tenantId() })
      if (disposed || token !== generation || controller.signal.aborted) return
      if (result.panel !== options.panel()) throw new Error('报表面板与请求不一致，请重新刷新')
      const lastPage = Math.max(1, Math.ceil(result.total / query.value.page_size))
      if (!result.items.length && result.total > 0 && query.value.page > lastPage) {
        changeQuery({ ...cloneQuery(query.value), page: lastPage })
        return
      }
      report.value = result
      fetchedAt.value = new Date().toISOString()
    } catch (cause) {
      if (!disposed && token === generation && !controller.signal.aborted) {
        error.value = cause instanceof Error ? cause.message : '报表加载失败，请稍后重试'
      }
    } finally {
      if (!disposed && token === generation) loading.value = false
    }
  }

  function changeQuery(next: JobReportQuery, delay = 0) {
    if (disposed) return
    invalidate()
    cancelExport()
    exportError.value = ''
    report.value = null
    fetchedAt.value = null
    error.value = ''
    query.value = cloneQuery(next)
    loading.value = true
    if (delay) debounce = setTimeout(() => { void refresh() }, delay)
    else void refresh()
  }

  const setFilters = (filters: Filters, delay = 0) => changeQuery({ ...cloneQuery(query.value), ...filters, page: 1 }, delay)
  const reset = () => changeQuery({ ...emptyQuery(), page_size: query.value.page_size })
  const setPage = (page: number) => { if (page !== query.value.page) changeQuery({ ...cloneQuery(query.value), page }) }
  const setPageSize = (page_size: number) => {
    if (page_size !== query.value.page_size) changeQuery({ ...cloneQuery(query.value), page: 1, page_size })
  }

  async function exportExcel(): Promise<string | null> {
    if (disposed || exporting.value || !report.value || loading.value) return null
    const controller = new AbortController()
    exportRequest = controller
    exporting.value = true
    exportError.value = ''
    const panel = options.panel()
    const tenantId = options.tenantId()
    const filters = cloneQuery(query.value)
    try {
      const filename = await options.exporter(panel, filters, { signal: controller.signal, tenantId })
      if (controller.signal.aborted || disposed || panel !== options.panel() || tenantId !== options.tenantId()) return null
      return filename
    } catch (cause) {
      if (!controller.signal.aborted && !disposed) exportError.value = cause instanceof Error ? cause.message : 'Excel 导出失败'
      return null
    } finally {
      if (exportRequest === controller) {
        exportRequest = undefined
        exporting.value = false
      }
    }
  }

  watch([options.panel, options.tenantId], () => changeQuery(emptyQuery()), { immediate: true, flush: 'sync' })
  const timer = setInterval(() => {
    if (autoRefresh.value && !loading.value && !exporting.value && (typeof document === 'undefined' || document.visibilityState !== 'hidden')) void refresh()
  }, 30000)
  onScopeDispose(() => {
    disposed = true
    invalidate()
    cancelExport()
    clearInterval(timer)
    loading.value = false
  })

  return { query, report, rows, loading, error, fetchedAt, autoRefresh, exporting, exportError,
    refresh, setFilters, reset, setPage, setPageSize, exportExcel, cancelExport }
}
