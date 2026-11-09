import { computed, onScopeDispose, reactive, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { ManageOneAuthType, ManageOneConfig, ManageOneConfigIn, ManageOneFetcher, ManageOneSaver } from '../../types/manageone'

const blank = () => ({ enabled: false, endpoint: '', auth_type: 'AKSK' as ManageOneAuthType,
  username: '', organization: '', password: '', clear_password: false,
  access_key: '', secret_key: '', clear_secret_key: false, select_source: false })
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
  function clearSecrets() { form.password = ''; form.access_key = ''; form.secret_key = '' }
  function apply(value: ManageOneConfig, tenantId: number) {
    if (value.tenant_id !== tenantId) throw new Error('ManageOne 配置范围不匹配')
    if (value.auth_type !== 'PASSWORD' && value.auth_type !== 'AKSK') throw new Error('ManageOne 认证方式无效')
    // 仅保留 GET 契约字段，不把响应中的未知字段或秘密带入页面状态。
    loaded.value = { tenant_id: tenantId, enabled: value.enabled, configured: value.configured, selected: value.selected,
      endpoint: value.endpoint, auth_type: value.auth_type, access_key_mask: value.access_key_mask, secret_key_set: value.secret_key_set,
      username: value.username, organization: value.organization, secret_set: value.secret_set }
    const empty = !value.enabled && !value.configured && !value.selected && !value.endpoint && !value.username && !value.organization
      && !value.secret_set && !value.access_key_mask && !value.secret_key_set
    Object.assign(form, blank(), { enabled: value.enabled, endpoint: value.endpoint ?? '', auth_type: empty ? 'AKSK' : value.auth_type,
      username: value.auth_type === 'PASSWORD' ? value.username ?? '' : '',
      organization: value.auth_type === 'PASSWORD' ? value.organization ?? '' : '' })
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
    const sameMode = form.auth_type === loaded.value?.auth_type
    if (form.select_source && !form.enabled) throw new Error('用于 ECS 指标采集前须启用 ManageOne 配置')
    if (endpoint) {
      let url: URL
      try { url = new URL(endpoint) } catch { throw new Error('请填写有效的运维北向 baseURL') }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('baseURL 仅允许 HTTP/HTTPS 地址，不含凭据、查询或片段')
    }
    const common = { enabled: form.enabled, endpoint, auth_type: form.auth_type, select_source: form.select_source }
    if (form.auth_type === 'AKSK') {
      const accessKey = form.access_key
      if (accessKey && /[^\x21-\x7e]|[*,=]/.test(accessKey)) throw new Error('请填写实际 AK，不含空白或脱敏占位符')
      if (form.secret_key && (!form.secret_key.trim() || /^\*+$/.test(form.secret_key))) throw new Error('请填写实际 SK')
      if (form.clear_secret_key && form.secret_key) throw new Error('新 SK 与清除 SK 不能同时选择')
      if (form.clear_secret_key && (form.enabled || form.select_source)) throw new Error('清除 SK 前请先关闭 ManageOne 配置')
      const changed = endpoint !== (loaded.value?.endpoint ?? '') || !!accessKey
      if (sameMode && loaded.value?.secret_key_set && changed && !form.secret_key && !form.clear_secret_key) {
        throw new Error('修改地址或 AK 后，请重新填写 SK，或关闭配置并清除旧 SK')
      }
      const hasKey = !!accessKey || sameMode && !!loaded.value?.access_key_mask
      const hasSecret = !!form.secret_key || sameMode && !!loaded.value?.secret_key_set && !form.clear_secret_key && !changed
      if ((form.enabled || form.select_source) && (!endpoint || !hasKey || !hasSecret)) throw new Error('启用或选择 ManageOne 来源须填写地址、AK 和 SK')
      return { ...common, ...(accessKey ? { access_key: accessKey } : {}),
        ...(form.secret_key ? { secret_key: form.secret_key } : {}), clear_secret_key: form.clear_secret_key }
    }
    if (form.auth_type !== 'PASSWORD') throw new Error('请选择认证方式')
    const changed = endpoint !== (loaded.value?.endpoint ?? '') || username !== (loaded.value?.username ?? '') || organization !== (loaded.value?.organization ?? '')
    if (form.clear_password && form.password) throw new Error('新密码与清除密码不能同时选择')
    if (form.clear_password && (form.enabled || form.select_source)) throw new Error('清除密码前请先关闭 ManageOne 配置')
    if (sameMode && loaded.value?.secret_set && changed && !form.password && !form.clear_password) {
      throw new Error('修改地址、账号或组织后，请重新填写密码，或关闭配置并清除旧密码')
    }
    const secret = !!form.password || sameMode && !!loaded.value?.secret_set && !form.clear_password && !changed
    if ((form.enabled || form.select_source) && (!endpoint || !username || !secret)) throw new Error('启用或选择 ManageOne 来源须填写地址、集成账号和密码')
    return { ...common, username, organization, ...(form.password ? { password: form.password } : {}), clear_password: form.clear_password }
  }
  async function save() {
    if (!canSave.value) return false
    error.value = ''; validationError.value = ''
    let body: ManageOneConfigIn
    try { body = payload() }
    catch (cause) { clearSecrets(); validationError.value = cause instanceof Error ? cause.message : '请检查 ManageOne 配置'; return false }
    const tenantId = options.tenantId()!
    const local = new AbortController(); saveController = local
    clearSecrets()
    const before = loaded.value!
    const sameMode = body.auth_type === before.auth_type
    try {
      return await saveRequest.run(async () => {
        await options.saver(tenantId, body, { signal: local.signal })
        const secretSet = body.auth_type === 'PASSWORD' && !body.clear_password && (!!body.password || sameMode && before.secret_set)
        const secretKeySet = body.auth_type === 'AKSK' && !body.clear_secret_key && (!!body.secret_key || sameMode && before.secret_key_set)
        // PUT 不返回配置；只合成公开状态，绝不将提交的 AK/SK 或密码保存到 loaded。
        const accessKeyMask = body.auth_type === 'AKSK' ? body.access_key ? '********' : sameMode ? before.access_key_mask : null : null
        return {
          tenant_id: tenantId, enabled: body.enabled, auth_type: body.auth_type,
          configured: !!body.endpoint && (body.auth_type === 'AKSK' ? !!accessKeyMask && secretKeySet : !!body.username && secretSet),
          selected: body.select_source || before.selected, endpoint: body.endpoint, username: body.username ?? '',
          organization: body.organization ?? '', secret_set: secretSet, access_key_mask: accessKeyMask, secret_key_set: secretKeySet,
        } satisfies ManageOneConfig
      }, value => apply(value, tenantId))
    }
    catch { if (!local.signal.aborted) error.value = '保存 ManageOne 配置失败，请检查配置后重试'; return false }
  }
  watch(() => form.auth_type, () => {
    clearSecrets(); form.username = ''; form.organization = ''; form.clear_password = false; form.clear_secret_key = false
    form.select_source = false; validationError.value = ''
  }, { flush: 'sync' })
  watch(() => form.enabled, enabled => { if (!enabled) form.select_source = false }, { flush: 'sync' })
  watch([options.tenantId, options.visible, options.canView, options.canManage, () => options.scopeKey?.()], () => { void load() }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { cancel(); reset() })
  return { form, loaded, error, validationError, canSave, loading: request.loading, saving: saveRequest.loading, load, save }
}
