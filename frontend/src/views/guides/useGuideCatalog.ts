import { onScopeDispose, shallowRef } from 'vue'
import type { ResourceGuide } from '../../types/guides'

export function useGuideCatalog(fetcher: (signal: AbortSignal) => Promise<ResourceGuide[]>) {
  const guides = shallowRef<ResourceGuide[]>([])
  const loading = shallowRef(false)
  const loadError = shallowRef('')
  let request: AbortController | undefined
  let disposed = false

  async function load() {
    if (disposed) return
    request?.abort()
    const current = new AbortController()
    request = current
    loading.value = true
    loadError.value = ''
    try {
      const data = await fetcher(current.signal)
      if (!disposed && request === current && !current.signal.aborted) guides.value = data
    } catch (cause) {
      if (!disposed && request === current && !current.signal.aborted) {
        loadError.value = cause instanceof Error ? cause.message : '指南加载失败'
      }
    } finally {
      if (!disposed && request === current) loading.value = false
    }
  }

  onScopeDispose(() => { disposed = true; request?.abort(); loading.value = false })
  return { guides, loading, loadError, load }
}
