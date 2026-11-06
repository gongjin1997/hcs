import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { EcsHistoryExporter, EcsHistoryFetcher, EcsHistoryOut, EcsHistoryQuery, EcsMetricKey, EcsRangePreset } from '../../types/ecs'
import { ecsRangeQuery, errorText } from './ecs-utils'

export function useEcsTrend(options: {
  tenantId: () => number | null; instanceId: () => number | null; visible: () => boolean; canExport: () => boolean
  metric?: () => EcsMetricKey
  canView?: () => boolean
  scopeKey?: () => string
  fetcher: EcsHistoryFetcher; exporter: EcsHistoryExporter
}) {
  const request = useLatestRequest()
  const exportRequest = useLatestRequest()
  const preset = shallowRef<EcsRangePreset>('24h')
  const history = shallowRef<EcsHistoryOut | null>(null)
  const appliedQuery = shallowRef<EcsHistoryQuery | null>(null)
  const error = shallowRef('')
  const exportError = shallowRef('')
  let controller: AbortController | null = null
  let exportController: AbortController | null = null
  const canExport = computed(() => (options.canView?.() ?? true) && options.canExport() && !!history.value && !!appliedQuery.value && !request.loading.value && !exportRequest.loading.value)

  function cancel() {
    request.invalidate()
    exportRequest.invalidate()
    controller?.abort()
    exportController?.abort()
  }
  async function refresh() {
    cancel()
    history.value = null
    appliedQuery.value = null
    error.value = ''
    exportError.value = ''
    const id = options.instanceId(), tenantId = options.tenantId()
    if (!options.visible() || !id || (options.canView && !options.canView())) return
    const query = { ...ecsRangeQuery(preset.value), ...(options.metric ? { metrics: [options.metric()] } : {}) }
    const local = new AbortController()
    controller = local
    try {
      await request.run(() => options.fetcher(id, query, { tenantId, signal: local.signal }), value => {
        if (value.instance.id !== id || (tenantId !== null && value.instance.tenant_id !== tenantId)) throw new Error('实例趋势范围不匹配，请重试')
        history.value = value
        appliedQuery.value = { ...query, ...(query.metrics ? { metrics: [...query.metrics] } : {}) }
      })
    } catch (cause) { if (!local.signal.aborted) error.value = errorText(cause, '趋势加载失败') }
  }
  async function exportExcel() {
    if (!canExport.value || !appliedQuery.value) return false
    const id = options.instanceId(), tenantId = options.tenantId()
    if (!id) return false
    const query = { ...appliedQuery.value, ...(appliedQuery.value.metrics ? { metrics: [...appliedQuery.value.metrics] } : {}) }
    exportController?.abort()
    const local = new AbortController()
    exportController = local
    exportError.value = ''
    try { return await exportRequest.run(() => options.exporter(id, query, { tenantId, signal: local.signal }), () => {}) }
    catch (cause) { if (!local.signal.aborted) exportError.value = errorText(cause, '导出失败'); return false }
  }
  watch([options.tenantId, options.instanceId, options.visible, preset, () => options.metric?.(), () => options.canView?.(), () => options.scopeKey?.()], () => { void refresh() }, { immediate: true, flush: 'sync' })
  watch(options.canExport, allowed => { if (!allowed) { exportController?.abort(); exportRequest.invalidate(); exportError.value = '' } }, { flush: 'sync' })
  onScopeDispose(cancel)
  return { preset, history, appliedQuery, error, exportError, loading: request.loading, exporting: exportRequest.loading, canExport, refresh, exportExcel }
}
