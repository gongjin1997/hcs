import { computed, onScopeDispose, reactive, shallowRef, watch } from 'vue'
import type {
  GaussApi, GaussDirectoryApi, GaussExportQuery, GaussInstance, GaussNativeRow, GaussNativeRows, GaussOverviewOut,
  GaussPanel, GaussRequestContext, GaussSessionsQuery, GaussSlowQuery,
} from '../../types/gaussdb'
import { errorText, GAUSS_PANELS, rangeQuery } from './gauss-utils'

export function useGaussPanel(options: {
  tenantId: () => number | null
  canView: () => boolean
  canRun: () => boolean
  canExport: () => boolean
  scopeKey?: () => string
  api: GaussApi
  now?: () => number
}) {
  const items = shallowRef<GaussInstance[]>([])
  const total = shallowRef(0)
  const page = shallowRef(1)
  const pageSize = shallowRef(20)
  const listFilters = reactive({ keyword: '', project_ref: null as number | null })
  const listLoading = shallowRef(false)
  const listError = shallowRef('')
  const selected = shallowRef<GaussInstance | null>(null)
  const panel = shallowRef<GaussPanel>('disk')
  const overview = shallowRef<GaussOverviewOut | null>(null)
  const slowNodes = shallowRef<GaussNativeRows | null>(null)
  const rows = shallowRef<GaussNativeRows | null>(null)
  const detail = shallowRef<GaussNativeRows | null>(null)
  const detailTitle = shallowRef('')
  const inspectionRecord = shallowRef<GaussNativeRow | null>(null)
  const slowDetailQuery = shallowRef<GaussExportQuery | null>(null)
  const error = shallowRef('')
  const exportError = shallowRef('')
  const loadingKind = shallowRef<'' | 'overview' | 'nodes' | 'query' | 'detail'>('')
  const exporting = shallowRef(false)
  const applied = shallowRef<GaussExportQuery | null>(null)
  const slowFilters = reactive({ node_ids: [] as string[], threshold: 1, sql_id: '' })
  const sessionFilters = reactive({ componentKey: '', limit: 50, database_name: '', user_name: '', client_ip: '', state: '', wait: '' as '' | 'f' | 't' })
  const inspectionFilters = reactive({ limit: 20, offset: 0 })
  const end = options.now?.() ?? Date.now()
  const slowRange = shallowRef<[Date, Date] | null>([new Date(end - 3600000), new Date(end)])
  const inspectionRange = shallowRef<[Date, Date] | null>(null)
  let listController: AbortController | null = null
  let nativeController: AbortController | null = null
  let exportController: AbortController | null = null
  let instanceController: AbortController | null = null
  let disposed = false
  const panelResults = new Map<GaussPanel, { rows: GaussNativeRows | null; applied: GaussExportQuery | null; detail: GaussNativeRows | null; record: GaussNativeRow | null; detailTitle: string; slowQuery: GaussExportQuery | null }>()

  const loading = computed(() => loadingKind.value !== '')
  const canQuery = computed(() => options.canRun() && !!selected.value && !listLoading.value)
  const canExport = computed(() => canQuery.value && options.canExport() && !!applied.value && !loading.value && !exporting.value)
  const canExportDetail = computed(() => canQuery.value && options.canExport() && !!detail.value && typeof inspectionRecord.value?.record_id === 'string' && !loading.value && !exporting.value)
  const canExportSlowDetail = computed(() => canQuery.value && options.canExport() && !!detail.value && !!slowDetailQuery.value && !loading.value && !exporting.value)
  const components = computed(() => (overview.value?.components ?? []).filter(item => item.component_type === 'CN'
    || (item.component_type === 'DN' && (['master', 'slave'].includes(item.role ?? '')
      || (!item.role && ['Primary', 'Standby'].includes(item.status ?? ''))))))
  const panelTitle = computed(() => GAUSS_PANELS.find(item => item.key === panel.value)?.label ?? '')
  const componentKey = (nodeId: string, componentId: string) => JSON.stringify([nodeId, componentId])

  function cancelExport() {
    exportController?.abort(); exportController = null; exporting.value = false
  }
  function invalidateResults() {
    nativeController?.abort(); nativeController = null
    cancelExport()
    loadingKind.value = ''; rows.value = null; detail.value = null; detailTitle.value = ''
    inspectionRecord.value = null; slowDetailQuery.value = null; applied.value = null; error.value = ''; exportError.value = ''
  }
  function clearNative() { invalidateResults(); overview.value = null; slowNodes.value = null }
  function cancelQuery() { clearNative() }
  function clearSelection() { instanceController?.abort(); instanceController = null; panelResults.clear(); clearNative(); selected.value = null }
  function clearList() {
    listController?.abort(); listController = null
    listLoading.value = false; listError.value = ''; items.value = []; total.value = 0
    clearSelection()
  }
  function resetForms() {
    Object.assign(slowFilters, { node_ids: [], threshold: 1, sql_id: '' })
    Object.assign(sessionFilters, { componentKey: '', limit: 50, database_name: '', user_name: '', client_ip: '', state: '', wait: '' })
    Object.assign(inspectionFilters, { limit: 20, offset: 0 })
  }
  function context(controller: AbortController): GaussRequestContext { return { tenantId: options.tenantId(), signal: controller.signal } }

  async function loadInstances() {
    if (disposed) return
    clearList()
    if (!options.canView()) return
    const controller = new AbortController()
    listController = controller
    const scope = context(controller)
    listLoading.value = true
    try {
      const result = await options.api.instances({
        keyword: listFilters.keyword.trim() || undefined, project_ref: listFilters.project_ref ?? undefined,
        page: page.value, page_size: pageSize.value,
      }, scope)
      if (disposed || controller.signal.aborted || listController !== controller || scope.tenantId !== options.tenantId() || !options.canView()) return
      if (result.items.some(item => (scope.tenantId !== null && item.tenant_id !== scope.tenantId)
        || (listFilters.project_ref !== null && item.project_ref !== listFilters.project_ref))) throw new Error('实例列表范围不匹配，请重试')
      items.value = result.items; total.value = result.total
    } catch (cause) {
      if (!disposed && listController === controller && !controller.signal.aborted) listError.value = errorText(cause, '实例列表查询失败')
    } finally {
      if (!disposed && listController === controller) listLoading.value = false
    }
  }
  function search() { page.value = 1; return loadInstances() }
  function changePage(value: number) {
    if (!Number.isSafeInteger(value) || value < 1 || disposed) return
    page.value = value; return loadInstances()
  }
  function changePageSize(value: number) {
    if (![20, 50, 100].includes(value) || disposed) return
    pageSize.value = value; return search()
  }
  function selectInstance(row: GaussInstance) {
    const instance = items.value.find(item => item.id === row.id && item.tenant_id === row.tenant_id)
    if (disposed || !options.canView() || !instance) return
    clearSelection(); resetForms(); selected.value = instance
  }
  async function openInstance(id: number, request: GaussDirectoryApi['instance'], retainSelection = false, expectedTenant?: number) {
    if (disposed || !options.canView() || !Number.isSafeInteger(id) || id < 1) return false
    const retaining = retainSelection && selected.value?.id === id
    if (retaining) instanceController?.abort()
    else clearSelection()
    const controller = new AbortController(), tenantId = options.tenantId()
    const scope = { ...context(controller), tenantId: expectedTenant ?? tenantId }
    instanceController = controller; listError.value = ''
    try {
      const instance = await request(id, scope)
      if (disposed || controller.signal.aborted || instanceController !== controller || tenantId !== options.tenantId() || !options.canView()) return false
      if (instance.id !== id || (scope.tenantId !== null && instance.tenant_id !== scope.tenantId)) throw new Error('实例范围不匹配')
      if (!retaining) resetForms()
      selected.value = instance
      return true
    } catch (cause) {
      if (!controller.signal.aborted && instanceController === controller) listError.value = errorText(cause, '实例不可访问或已移除')
      return false
    } finally { if (instanceController === controller) instanceController = null }
  }
  function setPanel(value: string | number) {
    if (GAUSS_PANELS.some(item => item.key === value)) panel.value = value as GaussPanel
  }

  async function runNative<T>(kind: 'overview' | 'nodes' | 'query' | 'detail',
    request: (id: number, scope: GaussRequestContext) => Promise<T>, apply: (result: T) => void): Promise<boolean> {
    if (disposed || !canQuery.value || !selected.value) return false
    nativeController?.abort(); cancelExport()
    const controller = new AbortController()
    nativeController = controller
    const scope = context(controller), instance = selected.value, currentPanel = panel.value
    loadingKind.value = kind; error.value = ''; exportError.value = ''
    const current = () => !disposed && nativeController === controller && !controller.signal.aborted
      && scope.tenantId === options.tenantId() && selected.value === instance && panel.value === currentPanel && options.canRun()
    try {
      const result = await request(instance.id, scope)
      if (!current()) return false
      apply(result)
      return true
    } catch (cause) {
      if (current()) error.value = errorText(cause, 'GaussDB 原生接口查询失败')
      return false
    } finally { if (current()) loadingKind.value = '' }
  }
  function assertRows(result: GaussNativeRows) {
    if (result.source !== 'GAUSSDB') throw new Error('诊断来源不匹配，请重试')
  }
  async function queryOverview() {
    if (!canQuery.value) return false
    invalidateResults(); overview.value = null
    const instance = selected.value!
    return runNative('overview', options.api.overview, result => {
      if (result.source !== 'GAUSSDB' || result.instance.id !== instance.id || result.instance.tenant_id !== instance.tenant_id
        || result.instance.resource_id !== instance.resource_id) throw new Error('实例诊断范围不匹配，请重试')
      overview.value = result
      if (!components.value.some(item => componentKey(item.node_id, item.component_id) === sessionFilters.componentKey)) sessionFilters.componentKey = ''
      if (panel.value === 'disk' || panel.value === 'overview') applied.value = { panel: 'overview' }
    })
  }
  async function loadSlowNodes() {
    if (!canQuery.value) return false
    invalidateResults(); slowNodes.value = null; slowFilters.node_ids = []
    return runNative('nodes', options.api.slowNodes, result => { assertRows(result); slowNodes.value = result })
  }
  function slowQuery(): GaussSlowQuery {
    const time = rangeQuery(slowRange.value)
    const available = new Set((slowNodes.value?.items ?? []).map(item => item.node_id).filter(item => typeof item === 'string'))
    if (!slowFilters.node_ids.length || slowFilters.node_ids.some(id => !available.has(id))) throw new Error('请加载并选择慢 SQL 节点')
    if (slowFilters.node_ids.length > 20) throw new Error('一次最多查询 20 个慢 SQL 节点')
    if (!Number.isInteger(slowFilters.threshold) || slowFilters.threshold < 1 || slowFilters.threshold > 99) throw new Error('慢 SQL 阈值须为 1–99 的整数')
    return { start_at: time.start_at!, end_at: time.end_at!, node_ids: [...slowFilters.node_ids],
      threshold: slowFilters.threshold, sql_id: slowFilters.sql_id.trim() || undefined }
  }
  function sessionsQuery(): GaussSessionsQuery {
    const component = components.value.find(item => componentKey(item.node_id, item.component_id) === sessionFilters.componentKey)
    if (!component) throw new Error('请获取组件并选择 CN/DN')
    if (!Number.isInteger(sessionFilters.limit) || sessionFilters.limit < 1 || sessionFilters.limit > 200) throw new Error('会话条数须为 1–200 的整数')
    return { node_id: component.node_id, component_id: component.component_id, limit: sessionFilters.limit,
      database_name: sessionFilters.database_name.trim() || undefined, user_name: sessionFilters.user_name.trim() || undefined,
      client_ip: sessionFilters.client_ip.trim() || undefined, state: sessionFilters.state.trim() || undefined, wait: sessionFilters.wait || undefined }
  }
  async function queryPanel() {
    if (!canQuery.value) return false
    if (panel.value === 'disk' || panel.value === 'overview') return queryOverview()
    invalidateResults()
    try {
      if (panel.value === 'slow-sql') {
        const query = slowQuery()
        return runNative('query', (id, scope) => options.api.slowSql(id, query, scope), result => {
          assertRows(result); rows.value = result; applied.value = { panel: 'slow-sql', ...query }
        })
      }
      if (panel.value === 'sessions') {
        const query = sessionsQuery()
        return runNative('query', (id, scope) => options.api.sessions(id, query, scope), result => {
          assertRows(result); rows.value = result; applied.value = { panel: 'sessions', ...query }
        })
      }
      const query = { ...rangeQuery(inspectionRange.value, false), ...inspectionFilters }
      if (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > 100 || !Number.isInteger(query.offset) || query.offset < 0 || query.offset > 1000) throw new Error('巡检分页条件无效：每页 1–100 条，偏移量 0–1000')
      return runNative('query', (id, scope) => options.api.inspections(id, query, scope), result => {
        assertRows(result); rows.value = result; applied.value = { panel: 'inspections', ...query }
      })
    } catch (cause) { error.value = errorText(cause, '查询条件无效'); return false }
  }
  async function showDetail(row: GaussNativeRow) {
    const identity = applied.value?.panel === 'inspections' ? 'record_id' : 'sql_id'
    const item = rows.value?.items.find(item => item === row || (typeof row[identity] === 'string' && item[identity] === row[identity]))
    if (!canQuery.value || !applied.value || !item) return false
    row = item
    detail.value = null; detailTitle.value = ''; inspectionRecord.value = null; slowDetailQuery.value = null
    if (applied.value.panel === 'slow-sql') {
      if (typeof row.sql_id !== 'string' || !row.sql_id || !applied.value.node_ids) return false
      const query = { start_at: applied.value.start_at!, end_at: applied.value.end_at!, node_ids: [...applied.value.node_ids], sql_id: row.sql_id }
      return runNative('detail', (id, scope) => options.api.slowDetail(id, query, scope), result => {
        assertRows(result); detail.value = result; detailTitle.value = `SQL ${query.sql_id}`
        slowDetailQuery.value = { panel: 'slow-sql-detail', ...query }
      })
    }
    if (applied.value.panel === 'inspections' && typeof row.record_id === 'string' && row.record_id) {
      if (row.status === 'running') { error.value = '巡检进行中，完成后可查看详情'; return false }
      const recordId = row.record_id
      return runNative('detail', (id, scope) => options.api.inspectionDetail(id, recordId, scope), result => {
        assertRows(result)
        if (result.record_id && result.record_id !== recordId) throw new Error('巡检记录范围不匹配，请重试')
        detail.value = result; inspectionRecord.value = row; detailTitle.value = `巡检 ${recordId}`
      })
    }
    return false
  }
  function closeDetail() {
    cancelExport()
    if (loadingKind.value === 'detail') { nativeController?.abort(); nativeController = null; loadingKind.value = '' }
    detail.value = null; detailTitle.value = ''; inspectionRecord.value = null; slowDetailQuery.value = null
  }
  async function performExport(query: GaussExportQuery) {
    if (disposed || !selected.value || !options.canRun() || !options.canExport()) return null
    cancelExport()
    const controller = new AbortController()
    exportController = controller
    const scope = context(controller), instance = selected.value, currentPanel = panel.value
    exporting.value = true; exportError.value = ''
    const current = () => !disposed && exportController === controller && !controller.signal.aborted
      && scope.tenantId === options.tenantId() && selected.value === instance && currentPanel === panel.value && options.canRun() && options.canExport()
    try { const filename = await options.api.export(instance.id, query, scope); return current() ? filename : null }
    catch (cause) { if (current()) exportError.value = errorText(cause, 'Excel 导出失败'); return null }
    finally { if (exportController === controller) { exportController = null; exporting.value = false } }
  }
  function exportExcel() {
    if (!canExport.value || !applied.value) return Promise.resolve(null)
    return performExport({ ...applied.value, ...(applied.value.node_ids ? { node_ids: [...applied.value.node_ids] } : {}) })
  }
  function exportInspectionDetail() {
    if (!canExportDetail.value || typeof inspectionRecord.value?.record_id !== 'string') return Promise.resolve(null)
    return performExport({ panel: 'inspection-detail', record_id: inspectionRecord.value.record_id })
  }
  function exportSlowDetail() {
    if (!canExportSlowDetail.value || !slowDetailQuery.value) return Promise.resolve(null)
    return performExport({ ...slowDetailQuery.value, node_ids: [...slowDetailQuery.value.node_ids!] })
  }
  function setRange(kind: 'slow' | 'inspection', value: unknown) {
    const range = Array.isArray(value) && value.length === 2 && value.every(date => date instanceof Date)
      ? [new Date(value[0].getTime()), new Date(value[1].getTime())] as [Date, Date] : null
    if (kind === 'slow') slowRange.value = range
    else { inspectionRange.value = range; inspectionFilters.offset = 0 }
  }
  function setInspectionPage(value: number) {
    if (disposed || !Number.isSafeInteger(value) || value < 1) return
    const offset = (value - 1) * inspectionFilters.limit
    if (offset > 1000) { error.value = '巡检偏移量最多 1000，请缩小时间范围'; return }
    inspectionFilters.offset = offset
  }
  const refresh = () => selected.value && options.canRun() ? queryPanel() : loadInstances()

  watch(listFilters, () => { page.value = 1; clearList() }, { flush: 'sync' })
  const invalidatePanel = (key: GaussPanel) => { panelResults.delete(key); if (panel.value === key) invalidateResults() }
  watch([slowFilters, slowRange], () => invalidatePanel('slow-sql'), { deep: true, flush: 'sync' })
  watch(sessionFilters, () => invalidatePanel('sessions'), { deep: true, flush: 'sync' })
  watch([inspectionFilters, inspectionRange], () => invalidatePanel('performance'), { deep: true, flush: 'sync' })
  watch(panel, (current, previous) => {
    panelResults.set(previous, { rows: rows.value, applied: applied.value, detail: detail.value, record: inspectionRecord.value, detailTitle: detailTitle.value, slowQuery: slowDetailQuery.value })
    invalidateResults()
    const cached = panelResults.get(current)
    if (cached) {
      rows.value = cached.rows; applied.value = cached.applied; detail.value = cached.detail
      inspectionRecord.value = cached.record; detailTitle.value = cached.detailTitle; slowDetailQuery.value = cached.slowQuery
    }
    if ((current === 'overview' || current === 'disk') && overview.value) applied.value = { panel: 'overview' }
  }, { flush: 'sync' })
  watch(() => [options.canView(), options.canRun(), options.canExport()] as const, ([view, run, exp]) => {
    if (!view) clearList()
    else if (!run) { panelResults.clear(); clearNative() }
    if (!exp) cancelExport()
  }, { flush: 'sync' })
  watch([options.tenantId, () => options.scopeKey?.()], () => {
    clearList(); resetForms(); listFilters.keyword = ''; listFilters.project_ref = null; page.value = 1
    void loadInstances()
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { disposed = true; clearList() })
  return { items, total, page, pageSize, listFilters, listLoading, listError, selected, panel, panelTitle, overview, components,
    slowNodes, rows, detail, detailTitle, inspectionRecord, error, exportError, loading, loadingKind, exporting, applied, canQuery, canExport, canExportDetail, canExportSlowDetail,
    slowFilters, sessionFilters, inspectionFilters, slowRange, inspectionRange, componentKey,
    loadInstances, search, changePage, changePageSize, selectInstance, openInstance, clearSelection, setPanel, queryOverview, loadSlowNodes, queryPanel, cancelQuery,
    showDetail, closeDetail, exportExcel, exportInspectionDetail, exportSlowDetail, cancelExport, setRange, setInspectionPage, refresh }
}

export type GaussPanelModel = ReturnType<typeof useGaussPanel>
