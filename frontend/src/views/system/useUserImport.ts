import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import type { UserImportResult } from '../../types'

export function useUserImport(options: {
  allowed: () => boolean
  importer: (file: File, dryRun: boolean, signal?: AbortSignal) => Promise<UserImportResult>
}) {
  const file = shallowRef<File | null>(null)
  const result = shallowRef<UserImportResult | null>(null)
  const parseError = shallowRef('')
  const parsing = shallowRef(false)
  const applying = shallowRef(false)
  const canApply = computed(() => options.allowed() && !parsing.value && !applying.value && !!file.value
    && !!result.value?.dry_run && result.value.applied === 0 && result.value.error_count === 0
    && result.value.errors.length === 0 && result.value.created + result.value.updated > 0)
  let generation = 0
  let request: AbortController | undefined
  let disposed = false

  function invalidate() {
    generation++
    request?.abort()
    request = undefined
    file.value = null
    result.value = null
    parseError.value = ''
    parsing.value = false
  }
  function reset() { if (!applying.value) invalidate() }

  async function preview(selected: File) {
    if (disposed || applying.value || !options.allowed()) return
    invalidate()
    if (!selected.name.toLowerCase().endsWith('.xlsx')) {
      parseError.value = '请上传 .xlsx 文件（可先下载模板）'
      return
    }
    if (selected.size > 10 * 1024 * 1024) {
      parseError.value = '文件不能超过 10 MB'
      return
    }
    const token = generation
    const controller = new AbortController()
    request = controller
    file.value = selected
    parsing.value = true
    try {
      const value = await options.importer(selected, true, controller.signal)
      if (disposed || token !== generation || !options.allowed()) return
      result.value = value
    } catch (cause) {
      if (!disposed && token === generation) parseError.value = cause instanceof Error ? cause.message : '文件校验失败'
    } finally { if (!disposed && token === generation) parsing.value = false }
  }

  async function apply(): Promise<UserImportResult | null> {
    if (disposed || !canApply.value || !file.value) return null
    const selected = file.value
    const token = generation
    applying.value = true
    parseError.value = ''
    try {
      const value = await options.importer(selected, false)
      if (disposed || token !== generation || !options.allowed()) return null
      result.value = value
      if (value.dry_run || value.error_count || value.errors.length) {
        parseError.value = '导入未完成，请修正错误后重新预览；不会跳过错误行。'
        return null
      }
      return value
    } catch (cause) {
      if (!disposed && token === generation) {
        // 确认会重新校验。返回的错误行保留展示，旧预览不能再次提交。
        const data: unknown = cause && typeof cause === 'object' && 'data' in cause ? cause.data : null
        result.value = isImportResult(data) ? { ...data, dry_run: false } : null
        parseError.value = `${cause instanceof Error ? cause.message : '导入失败'}；请重新预览后再确认。`
      }
      return null
    } finally { applying.value = false }
  }

  watch(options.allowed, allowed => { if (!allowed) invalidate() }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; invalidate() })
  return { file, result, parseError, parsing, applying, canApply, preview, apply, reset }
}

function isImportResult(value: unknown): value is UserImportResult {
  return !!value && typeof value === 'object' && 'total' in value && typeof value.total === 'number'
    && 'error_count' in value && typeof value.error_count === 'number'
    && 'errors' in value && Array.isArray(value.errors) && 'items' in value && Array.isArray(value.items)
}
