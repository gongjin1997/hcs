import { onScopeDispose, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { MonitoringCoverage } from '@/types/monitoring'

interface Options {
  tenantId: () => number | null
  permissionKey: () => string
  fetcher: (tenantId: number | null, signal: AbortSignal) => Promise<MonitoringCoverage>
}

/** 范围/权限变化立即清空结果；仅展示当前请求返回的本地巡检证据。 */
export function useMonitoringCoverage(options: Options) {
  const data = shallowRef<MonitoringCoverage | null>(null)
  const error = shallowRef('')
  const request = useLatestRequest()
  let controller: AbortController | null = null

  async function refresh() {
    controller?.abort()
    if (!options.permissionKey()) { request.invalidate(); data.value = null; error.value = ''; return }
    const current = new AbortController(), tenantId = options.tenantId()
    controller = current
    error.value = ''
    try {
      await request.run(() => options.fetcher(tenantId, current.signal), value => { data.value = value })
    } catch (cause) {
      if (!current.signal.aborted) error.value = cause instanceof Error ? cause.message : '监控覆盖加载失败'
    }
  }

  watch(() => [options.tenantId(), options.permissionKey()] as const, () => {
    request.invalidate(); controller?.abort(); data.value = null; error.value = ''; void refresh()
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => controller?.abort())
  return { data, error, loading: request.loading, refresh }
}
