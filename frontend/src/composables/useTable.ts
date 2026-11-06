import { getCurrentScope, onScopeDispose, reactive, ref, shallowRef, toRaw, watch } from 'vue'
import type { Page } from '@/types'

/** 列表页通用逻辑：查询条件、分页、加载态。fetcher 返回后端统一分页结构 Page<T>。 */
export function useTable<T, Q extends object>(
  fetcher: (q: Q & { page: number; page_size: number }) => Promise<Page<T>>,
  initial: Q,
  pageSize = 20,
) {
  const query = reactive({ ...initial }) as Q
  const page = ref(1)
  const size = ref(pageSize)
  const total = ref(0)
  const rows = shallowRef<T[]>([])
  const loading = ref(false)
  let generation = 0
  let disposed = false
  let lastLoad: Promise<void> = Promise.resolve()

  function load(): Promise<void> {
    if (disposed) return Promise.resolve()
    const current = ++generation
    lastLoad = (async () => {
      loading.value = true
      try {
        const res = await fetcher({ ...(toRaw(query) as Q), page: page.value, page_size: size.value })
        if (disposed || current !== generation) return
        rows.value = res.items
        total.value = res.total
      } catch (error) {
        if (!disposed && current === generation) throw error
      } finally {
        if (!disposed && current === generation) loading.value = false
      }
    })()
    return lastLoad
  }
  const search = () => {
    if (page.value === 1) return load()
    page.value = 1 // 同步 watcher 启动唯一一次请求，调用方仍可等待该请求。
    return lastLoad
  }
  const reset = () => { Object.assign(query, initial); return search() }
  watch([page, size], () => { void load().catch(() => { /* 请求层已显示错误。 */ }) }, { flush: 'sync' })
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; generation++; loading.value = false })
  return { query, page, size, total, rows, loading, load, search, reset }
}
