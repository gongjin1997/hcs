import { onScopeDispose, shallowRef } from 'vue'
import type { RuleDryRun, RuleDryRunResult } from '../../types/rules'

export function useRuleDryRun(fetcher: (body: RuleDryRun, signal: AbortSignal) => Promise<RuleDryRunResult>) {
  const result = shallowRef<RuleDryRunResult | null>(null)
  const error = shallowRef('')
  const running = shallowRef(false)
  let request: AbortController | undefined
  let disposed = false

  function clear() {
    request?.abort()
    request = undefined
    result.value = null
    error.value = ''
    running.value = false
  }

  async function run(body: RuleDryRun) {
    if (disposed) return
    clear()
    const current = new AbortController()
    request = current
    running.value = true
    try {
      const data = await fetcher(body, current.signal)
      if (!disposed && request === current && !current.signal.aborted) result.value = data
    } catch (cause) {
      if (!disposed && request === current && !current.signal.aborted) {
        error.value = cause instanceof Error ? cause.message : '试运行失败'
      }
    } finally {
      if (!disposed && request === current) running.value = false
    }
  }

  onScopeDispose(() => { disposed = true; clear() })
  return { result, error, running, run, clear }
}
