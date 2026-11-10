import { onScopeDispose, shallowReadonly, shallowRef, toValue, watch, type MaybeRefOrGetter } from 'vue'

/** A task feed owns one context, one request, and at most one polling timer. */
export function useTaskScopedResource<T>(
  context: MaybeRefOrGetter<string | null>,
  fetchResource: (context: string, signal: AbortSignal) => Promise<T>,
  pollIntervalMs = 0,
  pollWhen: (value: T | null) => boolean = () => true,
) {
  const data = shallowRef<T | null>(null)
  const loading = shallowRef(false)
  const error = shallowRef<unknown>(null)
  let version = 0
  let disposed = false
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined

  function invalidate() {
    ++version
    controller?.abort()
    controller = undefined
    clearTimeout(timer)
    timer = undefined
    loading.value = false
  }

  async function refresh() {
    if (disposed) return
    const key = toValue(context)
    invalidate()
    if (key === null) { data.value = null; error.value = null; return }
    const request = version
    const requestController = new AbortController()
    controller = requestController
    const isCurrent = () => !disposed && request === version && key === toValue(context) && !requestController.signal.aborted
    loading.value = true
    error.value = null
    try {
      const result = await fetchResource(key, requestController.signal)
      if (isCurrent()) data.value = result
    } catch (cause) {
      if (isCurrent()) error.value = cause
    } finally {
      if (isCurrent()) {
        loading.value = false
        controller = undefined
        if (pollIntervalMs > 0 && pollWhen(data.value)) timer = setTimeout(() => { void refresh() }, pollIntervalMs)
      }
    }
  }

  watch(() => toValue(context), () => {
    data.value = null
    error.value = null
    void refresh()
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { disposed = true; invalidate() })

  function updateFromServer(update: (previous: T | null) => T | null) {
    if (!disposed && toValue(context) !== null) data.value = update(data.value)
  }

  return { updateFromServer, data: shallowReadonly(data), loading: shallowReadonly(loading), error: shallowReadonly(error), refresh }
}
