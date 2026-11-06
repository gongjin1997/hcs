import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type {
  DcsHistoryExporter, DcsHistoryFetcher, DcsHistoryOut, DcsHistoryQuery, DcsInstance, DcsInstanceFetcher,
  DcsInstancesFetcher, DcsIntervalMinutes, DcsRangePreset, DcsRequestContext,
} from '../../types/dcs'
import { assertHistoryScope, cloneHistoryQuery, historyQuery, MAX_INSTANCES, metricPage } from './dcs-utils'

export function useDcsMemory(options: {
  tenantId: () => number | null
  projectRef?: () => number | null
  canView?: () => boolean
  scopeKey?: () => string
  autoSelect?: () => boolean
  instancesFetcher: DcsInstancesFetcher
  historyFetcher: DcsHistoryFetcher
  exporter: DcsHistoryExporter
  now?: () => number
}) {
  const instances = shallowRef<DcsInstance[]>([])
  const instancesTotal = shallowRef(0)
  const instancePage = shallowRef(1)
  const instanceKeyword = shallowRef('')
  const instancesLoading = shallowRef(false)
  const instancesError = shallowRef('')
  const opening = shallowRef(false)
  const openingError = shallowRef('')
  const selectedIds = shallowRef<number[]>([])
  const intervalMinutes = shallowRef<DcsIntervalMinutes>(5)
  const rangePreset = shallowRef<DcsRangePreset>('1d')
  const customRange = shallowRef<[Date | null, Date | null]>([null, null])
  const history = shallowRef<DcsHistoryOut | null>(null)
  const appliedQuery = shallowRef<DcsHistoryQuery | null>(null)
  const historyLoading = shallowRef(false)
  const historyError = shallowRef('')
  const validationError = shallowRef('')
  const cancelled = shallowRef(false)
  const exporting = shallowRef(false)
  const exportError = shallowRef('')
  const tablePage = shallowRef(1)
  const tableSize = shallowRef(20)
  const instancePageSize = 50
  const cache = new Map<number, DcsInstance>()
  let initialSelection = true
  let disposed = false
  let instanceRequest: AbortController | undefined
  let singleInstanceRequest: AbortController | undefined
  let historyRequest: AbortController | undefined
  let exportRequest: AbortController | undefined
  let searchTimer: ReturnType<typeof setTimeout> | undefined

  const projectRef = () => options.projectRef?.() ?? null
  const canView = () => options.canView?.() ?? true
  const context = (controller: AbortController): DcsRequestContext => ({ signal: controller.signal, tenantId: options.tenantId(), projectRef: projectRef() })
  const instanceOptions = computed(() => [...new Set([...selectedIds.value, ...instances.value.map(item => item.id)])]
    .map(id => cache.get(id)).filter((item): item is DcsInstance => item != null))
  const hasMoreInstances = computed(() => instancePage.value * instancePageSize < instancesTotal.value)
  const totalRows = computed(() => history.value?.series.reduce((sum, item) => sum + item.points.length, 0) ?? 0)
  const tableRows = computed(() => metricPage(history.value?.series ?? [], tablePage.value, tableSize.value))
  const canExport = computed(() => canView() && !historyLoading.value && !!appliedQuery.value && !!history.value?.series.length)

  function cancelExport() {
    exportRequest?.abort()
    exportRequest = undefined
    exporting.value = false
  }

  function clearHistory() {
    historyRequest?.abort()
    historyRequest = undefined
    cancelExport()
    historyLoading.value = false
    history.value = null
    appliedQuery.value = null
    historyError.value = ''
    exportError.value = ''
    validationError.value = ''
    cancelled.value = false
    tablePage.value = 1
  }

  function cancelHistory() {
    clearHistory()
    cancelled.value = true
  }

  async function queryHistory() {
    if (disposed || !canView()) return
    clearHistory()
    if (!selectedIds.value.length) return
    let query: DcsHistoryQuery
    try {
      query = historyQuery(selectedIds.value, rangePreset.value, customRange.value, intervalMinutes.value, options.now?.() ?? Date.now())
    } catch (cause) {
      validationError.value = cause instanceof Error ? cause.message : '查询条件无效'
      return
    }
    const controller = new AbortController()
    historyRequest = controller
    const scope = context(controller)
    historyLoading.value = true
    try {
      const result = await options.historyFetcher(cloneHistoryQuery(query), scope)
      if (disposed || historyRequest !== controller || controller.signal.aborted) return
      assertHistoryScope(result, query, scope.tenantId, scope.projectRef ?? null)
      if (result.series.some(series => {
        const expected = cache.get(series.instance.id)
        return !expected || expected.tenant_id !== series.instance.tenant_id || expected.project_ref !== series.instance.project_ref
      })) throw new Error('实例历史范围不匹配，请重新查询')
      history.value = result
      appliedQuery.value = cloneHistoryQuery(query)
    } catch (cause) {
      if (!disposed && historyRequest === controller && !controller.signal.aborted) {
        historyError.value = cause instanceof Error ? cause.message : '指标查询失败'
      }
    } finally {
      if (!disposed && historyRequest === controller) historyLoading.value = false
    }
  }

  async function setSelectedIds(ids: number[]) {
    if (disposed || !canView()) return
    cancelInstanceOpen()
    initialSelection = false
    clearHistory()
    const unique = [...new Set(ids)]
    if (unique.length > MAX_INSTANCES || unique.some(id => !Number.isSafeInteger(id) || id < 1 || !cache.has(id))) {
      validationError.value = unique.length > MAX_INSTANCES ? '最多选择 20 个实例' : '所选实例不可用，请重新选择'
      return
    }
    selectedIds.value = [...unique]
    await queryHistory()
  }

  function cancelInstanceOpen() {
    singleInstanceRequest?.abort()
    singleInstanceRequest = undefined
    opening.value = false
  }

  function clearSelection() {
    cancelInstanceOpen()
    initialSelection = false
    selectedIds.value = []
    openingError.value = ''
    clearHistory()
  }

  /** 单实例定位独立于搜索和分页，只加载本地目录与本地历史。 */
  async function openInstance(id: number, tenantId: number, fetcher: DcsInstanceFetcher) {
    if (disposed || !canView()) return false
    clearSelection()
    const controller = new AbortController(), scope = context(controller)
    singleInstanceRequest = controller
    opening.value = true
    try {
      const instance = await fetcher(id, { ...scope, tenantId })
      if (disposed || controller.signal.aborted || singleInstanceRequest !== controller || !canView()) return false
      if (instance.id !== id || instance.tenant_id !== tenantId || (scope.tenantId !== null && instance.tenant_id !== scope.tenantId)
        || (scope.projectRef != null && instance.project_ref !== scope.projectRef)) throw new Error('实例范围不匹配，请重新选择')
      cache.set(id, instance)
      selectedIds.value = [id]
      await queryHistory()
      return !controller.signal.aborted && singleInstanceRequest === controller
    } catch (cause) {
      if (!disposed && !controller.signal.aborted && singleInstanceRequest === controller) {
        openingError.value = cause instanceof Error ? cause.message : '实例不可访问或已移除'
      }
      return false
    } finally {
      if (singleInstanceRequest === controller) { singleInstanceRequest = undefined; opening.value = false }
    }
  }

  async function loadInstances(page = 1) {
    if (disposed || !canView()) return
    clearTimeout(searchTimer)
    searchTimer = undefined
    instanceRequest?.abort()
    const controller = new AbortController()
    instanceRequest = controller
    const scope = context(controller)
    instancesLoading.value = true
    instancesError.value = ''
    try {
      const result = await options.instancesFetcher({ keyword: instanceKeyword.value.trim() || undefined,
        page, page_size: instancePageSize, project_ref: scope.projectRef ?? undefined }, scope)
      if (disposed || instanceRequest !== controller || controller.signal.aborted) return
      if (result.items.some(item => (scope.tenantId != null && item.tenant_id !== scope.tenantId)
        || (scope.projectRef != null && item.project_ref !== scope.projectRef))) throw new Error('实例列表范围不匹配，请重试')
      result.items.forEach(item => cache.set(item.id, item))
      instances.value = page === 1 ? result.items : [...new Map([...instances.value, ...result.items].map(item => [item.id, item])).values()]
      instancesTotal.value = result.total
      instancePage.value = page
      if (initialSelection && (options.autoSelect?.() ?? true) && !selectedIds.value.length && result.items.length) void setSelectedIds([result.items[0].id])
    } catch (cause) {
      if (!disposed && instanceRequest === controller && !controller.signal.aborted) {
        instancesError.value = cause instanceof Error ? cause.message : '实例加载失败'
      }
    } finally {
      if (!disposed && instanceRequest === controller) instancesLoading.value = false
    }
  }

  function searchInstances(keyword: string) {
    if (disposed || !canView() || keyword.trim() === instanceKeyword.value) return
    instanceRequest?.abort()
    instanceRequest = undefined
    clearTimeout(searchTimer)
    instanceKeyword.value = keyword.trim()
    instances.value = []
    instancesTotal.value = 0
    instancePage.value = 1
    instancesError.value = ''
    instancesLoading.value = true
    searchTimer = setTimeout(() => { void loadInstances() }, 250)
  }

  const loadMoreInstances = () => {
    if (!instancesLoading.value && hasMoreInstances.value) return loadInstances(instancePage.value + 1)
  }

  async function setIntervalMinutes(interval: DcsIntervalMinutes) {
    if (disposed) return
    intervalMinutes.value = interval
    await queryHistory()
  }

  async function setRangePreset(preset: DcsRangePreset) {
    if (disposed) return
    if (preset === 'custom' && !customRange.value.every(date => date instanceof Date)) {
      const end = appliedQuery.value?.end_at ? new Date(appliedQuery.value.end_at) : new Date(options.now?.() ?? Date.now())
      customRange.value = [appliedQuery.value?.start_at ? new Date(appliedQuery.value.start_at) : new Date(end.getTime() - 86400000), end]
    }
    rangePreset.value = preset
    await queryHistory()
  }

  async function setCustomDate(index: 0 | 1, value: unknown) {
    if (disposed) return
    const date = value instanceof Date ? new Date(value.getTime()) : null
    customRange.value = index === 0 ? [date, customRange.value[1]] : [customRange.value[0], date]
    rangePreset.value = 'custom'
    await queryHistory()
  }

  async function refresh() {
    if (disposed) return
    if (initialSelection && !selectedIds.value.length) return loadInstances()
    await Promise.all([loadInstances(), queryHistory()])
  }

  async function exportExcel(): Promise<string | null> {
    if (disposed || exporting.value || !canExport.value || !appliedQuery.value) return null
    const controller = new AbortController()
    exportRequest = controller
    const scope = context(controller)
    const query = cloneHistoryQuery(appliedQuery.value)
    exporting.value = true
    exportError.value = ''
    try {
      const filename = await options.exporter(query, scope)
      if (disposed || exportRequest !== controller || controller.signal.aborted) return null
      return filename
    } catch (cause) {
      if (!disposed && exportRequest === controller && !controller.signal.aborted) {
        exportError.value = cause instanceof Error ? cause.message : 'Excel 导出失败'
      }
      return null
    } finally {
      if (!disposed && exportRequest === controller) { exportRequest = undefined; exporting.value = false }
    }
  }

  function setTablePage(page: number) {
    if (!disposed && Number.isSafeInteger(page) && page > 0) tablePage.value = Math.min(page, Math.max(1, Math.ceil(totalRows.value / tableSize.value)))
  }

  function setTableSize(size: number) {
    if (!disposed && [20, 50, 100].includes(size)) { tableSize.value = size; tablePage.value = 1 }
  }

  function resetScope() {
    instanceRequest?.abort()
    cancelInstanceOpen()
    clearTimeout(searchTimer)
    clearHistory()
    cache.clear()
    selectedIds.value = []
    instances.value = []
    instancesTotal.value = 0
    instancePage.value = 1
    instanceKeyword.value = ''
    instancesError.value = ''
    openingError.value = ''
    initialSelection = true
    instancesLoading.value = false
    if (canView()) void loadInstances()
  }

  watch([options.tenantId, projectRef, canView, () => options.scopeKey?.()], resetScope, { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    disposed = true
    instanceRequest?.abort()
    cancelInstanceOpen()
    clearTimeout(searchTimer)
    clearHistory()
    instancesLoading.value = false
  })

  return { instances, instancesTotal, instanceOptions, instancePage, instanceKeyword, instancesLoading, instancesError,
    hasMoreInstances, selectedIds, opening, openingError, intervalMinutes, rangePreset, customRange, history, appliedQuery,
    historyLoading, historyError, validationError, cancelled, exporting, exportError, canExport,
    tablePage, tableSize, tableRows, totalRows, loadInstances, searchInstances, loadMoreInstances,
    setSelectedIds, openInstance, clearSelection, cancelInstanceOpen, setIntervalMinutes, setRangePreset, setCustomDate, queryHistory, refresh, cancelHistory,
    exportExcel, cancelExport, setTablePage, setTableSize }
}
