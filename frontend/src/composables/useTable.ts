import { reactive, ref, shallowRef, toRaw, watch } from 'vue'
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

  async function load() {
    loading.value = true
    try {
      const res = await fetcher({ ...(toRaw(query) as Q), page: page.value, page_size: size.value })
      rows.value = res.items
      total.value = res.total
    } finally {
      loading.value = false
    }
  }
  const search = () => { page.value = 1; return load() }
  const reset = () => { Object.assign(query, initial); return search() }
  watch([page, size], load)
  return { query, page, size, total, rows, loading, load, search, reset }
}
