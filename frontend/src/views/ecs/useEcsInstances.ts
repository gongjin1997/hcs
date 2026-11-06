import { onScopeDispose, reactive, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { EcsInstance, EcsInstancesFetcher } from '../../types/ecs'
import { errorText } from './ecs-utils'

export function useEcsInstances(options: { tenantId: () => number | null; fetcher: EcsInstancesFetcher; canView?: () => boolean; scopeKey?: () => string }) {
  const request = useLatestRequest()
  const items = shallowRef<EcsInstance[]>([])
  const total = shallowRef(0)
  const error = shallowRef('')
  const filters = reactive({ keyword: '', critical: '' as '' | 'yes' | 'no', monitored: '' as '' | 'yes' | 'no' })
  const page = shallowRef(1)
  const pageSize = shallowRef(20)
  let controller: AbortController | null = null
  function cancel() { request.invalidate(); controller?.abort() }
  async function refresh() {
    cancel()
    error.value = ''
    items.value = []
    total.value = 0
    if (options.canView && !options.canView()) return
    const tenantId = options.tenantId()
    const query = { page: page.value, page_size: pageSize.value, keyword: filters.keyword.trim() || undefined,
      critical: filters.critical === '' ? undefined : filters.critical === 'yes',
      monitored: filters.monitored === '' ? undefined : filters.monitored === 'yes' }
    const local = new AbortController()
    controller = local
    try { await request.run(() => options.fetcher(query, { tenantId, signal: local.signal }), value => {
      if (value.items.some(item => tenantId !== null && item.tenant_id !== tenantId)) throw new Error('实例列表范围不匹配，请重试')
      items.value = value.items; total.value = value.total
    }) }
    catch (cause) { if (!local.signal.aborted) error.value = errorText(cause, '实例加载失败') }
  }
  function search() { page.value = 1; return refresh() }
  function changePage(value: number) { page.value = value; return refresh() }
  function changePageSize(value: number) { pageSize.value = value; return search() }
  function resetFilters() { filters.keyword = ''; filters.critical = ''; filters.monitored = ''; return search() }
  watch([options.tenantId, () => options.canView?.(), () => options.scopeKey?.()], () => { filters.keyword = ''; filters.critical = ''; filters.monitored = ''; page.value = 1; void refresh() }, { immediate: true, flush: 'sync' })
  onScopeDispose(cancel)
  return { items, total, error, filters, page, pageSize, loading: request.loading, refresh, search, changePage, changePageSize, resetFilters }
}
