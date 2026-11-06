import { onScopeDispose, shallowRef } from 'vue'
import type { Rule } from '../../types'
import type { RuleTarget } from '../../types/rules'

export function useRuleCatalog(fetchers: {
  rules: (signal: AbortSignal) => Promise<Rule[]>
  targets: (signal: AbortSignal) => Promise<RuleTarget[]>
}) {
  const rules = shallowRef<Rule[]>([])
  const targets = shallowRef<RuleTarget[]>([])
  const loading = shallowRef(false)
  const loadError = shallowRef('')
  const targetError = shallowRef('')
  let request: AbortController | undefined
  let disposed = false

  async function load() {
    if (disposed) return
    request?.abort()
    const current = new AbortController()
    request = current
    loading.value = true
    loadError.value = ''
    targetError.value = ''
    const [rows, metadata] = await Promise.allSettled([fetchers.rules(current.signal), fetchers.targets(current.signal)])
    if (disposed || request !== current || current.signal.aborted) return
    if (rows.status === 'fulfilled') rules.value = rows.value
    else loadError.value = rows.reason instanceof Error ? rows.reason.message : '规则加载失败'
    if (metadata.status === 'fulfilled') targets.value = metadata.value
    else {
      targets.value = []
      targetError.value = metadata.reason instanceof Error ? metadata.reason.message : '检查项加载失败'
    }
    loading.value = false
  }

  onScopeDispose(() => { disposed = true; request?.abort(); loading.value = false })
  return { rules, targets, loading, loadError, targetError, load }
}
