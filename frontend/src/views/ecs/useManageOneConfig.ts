import { computed, onScopeDispose, reactive, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { ManageOneConfig, ManageOneConfigIn, ManageOneFetcher, ManageOneSaver } from '../../types/manageone'

const blank = () => ({ enabled: false, endpoint: '', username: '', organization: '', password: '', clear_password: false, select_source: false })
export function useManageOneConfig(options: {
  tenantId: () => number | null; visible: () => boolean; canView: () => boolean; canManage: () => boolean
  scopeKey?: () => string; fetcher: ManageOneFetcher; saver: ManageOneSaver
}) {
  const request = useLatestRequest(), saveRequest = useLatestRequest()
  const form = reactive(blank())
  const loaded = shallowRef<ManageOneConfig | null>(null)
  const error = shallowRef(''), validationError = shallowRef('')
  let controller: AbortController | null = null, saveController: AbortController | null = null
  const validTenant = () => { const id = options.tenantId(); return id !== null && Number.isSafeInteger(id) && id > 0 }
  const canSave = computed(() => options.visible() && options.canView() && options.canManage() && validTenant()
    && loaded.value?.tenant_id === options.tenantId() && !request.loading.value && !saveRequest.loading.value)
  function cancel() { request.invalidate(); saveRequest.invalidate(); controller?.abort(); saveController?.abort() }
  function reset() { loaded.value = null; Object.assign(form, blank()); error.value = ''; validationError.value = '' }
  function apply(value: ManageOneConfig, tenantId: number) {
    if (value.tenant_id !== tenantId) throw new Error('ManageOne 配置范围不匹配')
    // 仅保留 GET 契约字段，不把响应中的未知字段或秘密带入页面状态。
    loaded.value = { tenant_id: tenantId, enabled: value.enabled, configured: value.configured, selected: value.selected,
      endpoint: value.endpoint, username: value.username, organization: value.organization, secret_set: value.secret_set }
    Object.assign(form, blank(), { enabled: value.enabled, endpoint: value.endpoint ?? '', username: value.username ?? '', organization: value.organization ?? '' })
  }
  async function load() {
    cancel(); reset()
    if (!options.visible() || !options.canView() || !validTenant()) return
    const tenantId = options.tenantId()!
    const local = new AbortController(); controller = local
    try { await request.run(() => options.fetcher(tenantId, { signal: local.signal }), value => apply(value, tenantId)) }
    catch { if (!local.signal.aborted) error.value = '读取 ManageOne 配置失败，请重试' }
  }
  function payload(): ManageOneConfigIn {
    const endpoint = form.endpoint.trim(), username = form.username.trim(), organization = form.organization.trim()
    const changed = endpoint !== (loaded.value?.endpoint ?? '') || username !== (loaded.value?.username ?? '') || organization !== (loaded.value?.organization ?? '')
    if (form.clear_password && form.password) throw new Error('新密码与清除密码不能同时选择')
    if (changed && !form.password) throw new Error('修改服务地址、集成账号或组织后，请重新填写密码')
    if (form.clear_password && (form.enabled || form.select_source)) throw new Error('清除密码前请先关闭 ManageOne 配置')
    if (form.select_source && !form.enabled) throw new Error('用于 ECS 指标采集前须启用 ManageOne 配置')
    if (endpoint) {
      let url: URL
      try { url = new URL(endpoint) } catch { throw new Error('请填写有效的运维北向 baseURL') }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('baseURL 仅允许 HTTP/HTTPS 地址，不含凭据、查询或片段')
    }
    const secret = !!form.password || !!loaded.value?.secret_set && !form.clear_password && !changed
    if ((form.enabled || form.select_source) && (!endpoint || !username || !secret)) throw new Error('启用或选择 ManageOne 来源须填写服务地址、集成账号和密码')
    return { enabled: form.enabled, endpoint, username, organization, ...(form.password ? { password: form.password } : {}),
      clear_password: form.clear_password, select_source: form.select_source }
  }
  async function save() {
    if (!canSave.value) return false
    error.value = ''; validationError.value = ''
    let body: ManageOneConfigIn
    try { body = payload() }
    catch (cause) { validationError.value = cause instanceof Error ? cause.message : '请检查 ManageOne 配置'; return false }
    const tenantId = options.tenantId()!
    const local = new AbortController(); saveController = local
    form.password = ''
    const wasSelected = loaded.value!.selected
    const hadSecret = loaded.value!.secret_set
    try {
      return await saveRequest.run(async () => {
        await options.saver(tenantId, body, { signal: local.signal })
        const secretSet = !body.clear_password && (hadSecret || !!body.password)
        return {
          tenant_id: tenantId, enabled: body.enabled, configured: !!body.endpoint && !!body.username && secretSet,
          selected: body.select_source || wasSelected, endpoint: body.endpoint, username: body.username,
          organization: body.organization, secret_set: secretSet,
        } satisfies ManageOneConfig
      }, value => apply(value, tenantId))
    }
    catch { if (!local.signal.aborted) error.value = '保存 ManageOne 配置失败，请检查配置后重试'; return false }
  }
  watch(() => form.enabled, enabled => { if (!enabled) form.select_source = false }, { flush: 'sync' })
  watch([options.tenantId, options.visible, options.canView, options.canManage, () => options.scopeKey?.()], () => { void load() }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { cancel(); reset() })
  return { form, loaded, error, validationError, canSave, loading: request.loading, saving: saveRequest.loading, load, save }
}
