import { onScopeDispose, readonly, shallowRef } from 'vue'

/** Prevent an earlier response from replacing the current selection. */
export function useLatestRequest() {
  const loading = shallowRef(false)
  let sequence = 0
  let disposed = false

  function invalidate() {
    sequence += 1
    loading.value = false
  }

  async function run<T>(load: () => Promise<T>, apply: (value: T) => void): Promise<boolean> {
    if (disposed) return false
    const current = ++sequence
    loading.value = true
    try {
      const value = await load()
      if (disposed || current !== sequence) return false
      apply(value)
      return true
    } catch (error) {
      if (disposed || current !== sequence) return false
      throw error
    } finally {
      if (!disposed && current === sequence) loading.value = false
    }
  }

  onScopeDispose(() => { disposed = true; invalidate() })
  return { loading: readonly(loading), run, invalidate }
}
