import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type { LocationQuery } from 'vue-router'
import type { RequestConfig } from '../api/request'
import { scopeHeaders } from '../api/scope'
import { positiveIdParam, resourceViewerScopeKey, type ResourceViewer } from '../config/resource-navigation'

/** 反向资源链接沿用全局租户选择，确保列表、统计、详情和变更记录使用同一范围。 */
export function useResourceRouteScope(options: {
  query: () => LocationQuery
  app: { tenantId: number | null; setTenant: (id: number | null) => void }
  viewer: ResourceViewer
  permission: string
}) {
  const unavailable = shallowRef(false)
  const error = computed(() => {
    if (!options.viewer.user || !options.viewer.can(options.permission)) return '当前账号无权查看此页面'
    const raw = options.query().tenant_id
    if (raw == null) return ''
    const id = positiveIdParam(raw)
    if (id === null) return '租户链接无效，请重新选择租户'
    const allowed = options.viewer.user.tenant_ids
    if (allowed !== null && !allowed.includes(id)) return '当前账号无权查看此租户'
    if (options.app.tenantId !== id) return '链接不属于当前租户，请重新选择'
    return unavailable.value ? '链接中的租户不可用，请重新选择' : ''
  })
  const key = computed(() => JSON.stringify([options.query().tenant_id, options.app.tenantId, resourceViewerScopeKey(options.viewer)]))
  const requests = new Map<string, AbortController>()
  let disposed = false
  function cancel() { requests.forEach(controller => controller.abort()); requests.clear() }
  watch(() => options.query().tenant_id, () => {
    unavailable.value = false
    const tenant = positiveIdParam(options.query().tenant_id)
    const user = options.viewer.user
    if (tenant !== null && options.app.tenantId === null && user && options.viewer.can(options.permission)
      && (user.tenant_ids === null || user.tenant_ids.includes(tenant))) {
      options.app.setTenant(tenant)
      unavailable.value = options.app.tenantId !== tenant
    }
  }, { immediate: true, flush: 'sync' })
  watch(key, cancel, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; cancel() })

  async function request<T>(name: string, fetcher: (config: RequestConfig) => Promise<T>): Promise<T | null> {
    if (disposed || error.value) return null
    requests.get(name)?.abort()
    const controller = new AbortController(), current = key.value
    requests.set(name, controller)
    const active = () => !disposed && !controller.signal.aborted && current === key.value && requests.get(name) === controller && !error.value
    try {
      const result = await fetcher({ signal: controller.signal, headers: scopeHeaders(options.app.tenantId) })
      return active() ? result : null
    } catch (cause) { if (active()) throw cause; return null }
    finally { if (requests.get(name) === controller) requests.delete(name) }
  }
  return { error, key, request }
}
