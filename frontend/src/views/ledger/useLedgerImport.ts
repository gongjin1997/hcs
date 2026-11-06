import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type { ImportResult } from '../../types'

export interface LedgerImportContext { tenantId: number | null; signal?: AbortSignal }

export function useLedgerImport(options: {
  tenantId: () => number | null
  importer: (file: File, dryRun: boolean, skipErrors: boolean, context: LedgerImportContext) => Promise<ImportResult>
}) {
  const file = shallowRef<File | null>(null)
  const result = shallowRef<ImportResult | null>(null)
  const parseError = shallowRef('')
  const parsing = shallowRef(false)
  const applying = shallowRef(false)
  const skipErrors = shallowRef(false)
  const canApply = computed(() => !parsing.value && !applying.value && !!file.value && !!result.value
    && result.value.changed > 0 && (!result.value.error_count || skipErrors.value))
  let request: AbortController | undefined
  let generation = 0
  let disposed = false
  let previewTenant: number | null = null

  function reset() {
    generation++
    request?.abort()
    request = undefined
    file.value = null
    result.value = null
    parseError.value = ''
    parsing.value = false
    skipErrors.value = false
  }

  async function preview(selected: File) {
    if (disposed || applying.value) return
    reset()
    const token = generation
    const controller = new AbortController()
    request = controller
    const tenantId = options.tenantId()
    file.value = selected
    parsing.value = true
    try {
      const value = await options.importer(selected, true, false, { tenantId, signal: controller.signal })
      if (disposed || generation !== token || controller.signal.aborted || options.tenantId() !== tenantId) return
      result.value = value
      previewTenant = tenantId
    } catch (cause) {
      if (!disposed && generation === token && !controller.signal.aborted) {
        parseError.value = cause instanceof Error ? cause.message : '文件校验失败'
        file.value = null
      }
    } finally { if (!disposed && generation === token) parsing.value = false }
  }

  async function apply(): Promise<ImportResult | null> {
    if (disposed || !canApply.value || !file.value || previewTenant !== options.tenantId()) return null
    const selected = file.value
    const token = generation
    const tenantId = previewTenant
    const skip = skipErrors.value
    applying.value = true
    parseError.value = ''
    try {
      const value = await options.importer(selected, false, skip, { tenantId })
      if (disposed || token !== generation || options.tenantId() !== tenantId) return null
      // 禁止同一份已应用预览再次提交；之后需要重新预览。
      result.value = null
      file.value = null
      return value
    } catch (cause) {
      if (!disposed && token === generation) parseError.value = cause instanceof Error ? cause.message : '导入失败'
      return null
    } finally { applying.value = false }
  }

  watch(options.tenantId, reset, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; reset() })
  return { file, result, parseError, parsing, applying, skipErrors, canApply, preview, apply, reset }
}
