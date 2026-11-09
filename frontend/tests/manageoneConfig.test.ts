import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef } from 'vue'
import type { ManageOneConfig, ManageOneFetcher, ManageOneSaver } from '../src/types/manageone'
import { useManageOneConfig } from '../src/views/ecs/useManageOneConfig'
import { deferred } from './businessViewHarness'

const stops: (() => void)[] = []
afterEach(() => stops.splice(0).forEach(stop => stop()))
const settle = async () => { await new Promise<void>(resolve => setImmediate(resolve)); await nextTick() }
export const manageOneConfig = (tenant = 7, overrides: Partial<ManageOneConfig> = {}): ManageOneConfig => ({
  tenant_id: tenant, enabled: false, configured: false, selected: false,
  endpoint: null, auth_type: 'PASSWORD', access_key_mask: null, secret_key_set: false,
  username: null, organization: null, secret_set: false, ...overrides,
})
function setup(initial: { tenant?: number | null; read?: boolean; edit?: boolean } = {}) {
  const tenant = shallowRef<number | null>(initial.tenant === undefined ? 7 : initial.tenant)
  const visible = shallowRef(true), read = shallowRef(initial.read ?? true), edit = shallowRef(initial.edit ?? true)
  const reads: { id: number; signal: AbortSignal; result: ReturnType<typeof deferred<ManageOneConfig>> }[] = []
  const saves: { id: number; signal: AbortSignal; result: ReturnType<typeof deferred<void>> }[] = []
  const fetcher = vi.fn<ManageOneFetcher>((id, { signal }) => { const result = deferred<ManageOneConfig>(); reads.push({ id, signal, result }); return result.promise })
  const saver = vi.fn<ManageOneSaver>((id, _body, { signal }) => { const result = deferred<void>(); saves.push({ id, signal, result }); return result.promise })
  const scope = effectScope(); stops.push(() => scope.stop())
  const model = scope.run(() => useManageOneConfig({ tenantId: () => tenant.value, visible: () => visible.value,
    canView: () => read.value, canManage: () => edit.value, fetcher, saver }))!
  const ready = async (value = manageOneConfig()) => { reads.at(-1)!.result.resolve(value); await settle() }
  return { model, tenant, visible, read, edit, reads, saves, fetcher, saver, ready }
}
const configured = () => manageOneConfig(7, { enabled: true, configured: true, endpoint: 'https://manageone.test.invalid', username: 'integration', organization: '', secret_set: true })
const akskConfigured = () => manageOneConfig(7, { enabled: true, configured: true, endpoint: 'https://manageone.test.invalid',
  auth_type: 'AKSK', access_key_mask: 'AKTE****LAST', secret_key_set: true })

describe('ManageOne 原生配置状态与秘密隔离', () => {
  it('新建默认关闭，密码为空，选择来源必须显式勾选', async () => {
    const { model, ready } = setup(); await ready()
    expect(model.form).toMatchObject({ enabled: false, auth_type: 'AKSK', password: '', access_key: '', secret_key: '',
      select_source: false, clear_password: false, clear_secret_key: false })
    expect(model.loaded.value?.configured).toBe(false)
  })
  it.each([null, 0, -1, Number.NaN])('未选具体合法租户 %s 不发请求', tenant => {
    const { fetcher, saver, model } = setup({ tenant })
    expect(fetcher).not.toHaveBeenCalled(); expect(saver).not.toHaveBeenCalled(); expect(model.canSave.value).toBe(false)
  })
  it('无读取权限不请求；只读角色可看但不能保存', async () => {
    const denied = setup({ read: false }); expect(denied.fetcher).not.toHaveBeenCalled()
    const view = setup({ edit: false }); await view.ready(configured())
    expect(view.model.loaded.value?.secret_set).toBe(true)
    expect(await view.model.save()).toBe(false); expect(view.saver).not.toHaveBeenCalled()
  })
  it('已选中只描述服务端状态，不默认发出再次选择来源的意图', async () => {
    const view = setup(); await view.ready({ ...configured(), selected: true })
    expect(view.model.loaded.value?.selected).toBe(true)
    expect(view.model.form.select_source).toBe(false)
    view.model.form.enabled = false
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toMatchObject({ enabled: false, select_source: false })
    expect(view.saver.mock.calls[0][1]).not.toHaveProperty('password')
    view.saves[0].result.resolve(undefined)
    expect(await saving).toBe(true); expect(view.model.loaded.value?.selected).toBe(true)
  })
  it.each(['endpoint', 'username', 'organization'] as const)('修改 %s 必须重新填写密码，不能复用旧秘密', async key => {
    const view = setup(); await view.ready(configured())
    view.model.form[key] += 'changed'
    expect(await view.model.save()).toBe(false)
    expect(view.model.validationError.value).toContain('重新填写密码')
    expect(view.saver).not.toHaveBeenCalled()
  })
  it('完整配置显式选择来源，提交密码后立刻清空，不提交任务、阶段或 metric_keys', async () => {
    const view = setup(); await view.ready()
    Object.assign(view.model.form, { auth_type: 'PASSWORD', enabled: true, endpoint: 'https://manageone.test.invalid', username: 'integration', password: 'test-secret-only', select_source: true })
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toEqual({ auth_type: 'PASSWORD', enabled: true, endpoint: 'https://manageone.test.invalid', username: 'integration', organization: '', password: 'test-secret-only', clear_password: false, select_source: true })
    expect(view.model.form.password).toBe('')
    view.saves[0].result.resolve(undefined)
    expect(await saving).toBe(true)
    expect(view.model.form.password).toBe(''); expect(view.model.form.select_source).toBe(false)
  })
  it('启用/选择来源要求配置完整，关闭时不允许残留选择来源意图', async () => {
    const view = setup(); await view.ready()
    view.model.form.enabled = true
    expect(await view.model.save()).toBe(false); expect(view.saver).not.toHaveBeenCalled()
    view.model.form.select_source = true; view.model.form.enabled = false
    expect(view.model.form.select_source).toBe(false)
  })
  it('显式清除秘密仅在关闭配置时允许，不能与新密码一起提交', async () => {
    const view = setup(); await view.ready(configured())
    view.model.form.clear_password = true
    expect(await view.model.save()).toBe(false)
    view.model.form.enabled = false; view.model.form.password = 'new-test-secret'
    expect(await view.model.save()).toBe(false)
    view.model.form.password = ''
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toMatchObject({ enabled: false, clear_password: true, select_source: false })
    expect(view.saver.mock.calls[0][1]).not.toHaveProperty('password')
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
  })
  it('读取失败不允许保存默认值；错误不回显可能含密码的原始文本', async () => {
    const view = setup(); view.reads[0].result.reject(new Error('private-secret http://private.invalid/query')); await settle()
    expect(await view.model.save()).toBe(false); expect(view.saver).not.toHaveBeenCalled()
    expect(view.model.error.value).toContain('读取'); expect(view.model.error.value).not.toContain('private')
  })
  it('忽略GET响应中契约以外的秘密字段，不把它保留在已加载状态', async () => {
    const view = setup()
    view.reads[0].result.resolve({ ...configured(), password: 'unexpected-secret', access_key: 'unexpected-raw-ak',
      secret_key: 'unexpected-sk', sk_cipher: 'unexpected-cipher' } as ManageOneConfig)
    await settle()
    expect(view.model.form.password).toBe('')
    expect('password' in (view.model.loaded.value as unknown as Record<string, unknown>)).toBe(false)
    expect(JSON.stringify(view.model.loaded.value)).not.toContain('unexpected-secret')
    expect(Object.keys(view.model.loaded.value!).sort()).toEqual(['tenant_id', 'enabled', 'configured', 'selected', 'endpoint',
      'auth_type', 'access_key_mask', 'secret_key_set', 'username', 'organization', 'secret_set'].sort())
  })
  it('已有 PASSWORD 配置保留认证方式，空密码也不切换为 AK/SK', async () => {
    const view = setup(); await view.ready(configured())
    expect(view.model.form.auth_type).toBe('PASSWORD')
    expect(view.model.form.username).toBe('integration')
    expect(view.model.form.password).toBe('')
    const incomplete = setup(); await incomplete.ready(manageOneConfig(7, { username: 'draft-account' }))
    expect(incomplete.model.form.auth_type).toBe('PASSWORD')
  })
  it('已有 AK/SK 留空保留，掩码不回填为 AK；void 保存后只更新公开状态', async () => {
    const view = setup(); await view.ready(akskConfigured())
    expect(view.model.form).toMatchObject({ auth_type: 'AKSK', access_key: '', secret_key: '', password: '' })
    // 即使另一方式的草稿被外部写入，也不得提交。
    view.model.form.password = 'unrelated-password'
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toEqual({ auth_type: 'AKSK', enabled: true, endpoint: 'https://manageone.test.invalid',
      clear_secret_key: false, select_source: false })
    expect(view.model.form.password).toBe('')
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(view.model.loaded.value).toMatchObject({ configured: true, secret_set: false, secret_key_set: true, access_key_mask: 'AKTE****LAST' })
    expect(view.reads).toHaveLength(1)
  })
  it('新 AK/SK 完整提交，立即清空 AK/SK；成功状态中只保留掩码', async () => {
    const view = setup(); await view.ready()
    Object.assign(view.model.form, { enabled: true, endpoint: 'https://manageone.test.invalid', access_key: 'actual-ak-only',
      secret_key: 'actual-sk-only', select_source: true })
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toEqual({ enabled: true, endpoint: 'https://manageone.test.invalid', auth_type: 'AKSK',
      access_key: 'actual-ak-only', secret_key: 'actual-sk-only', clear_secret_key: false, select_source: true })
    expect(view.model.form).toMatchObject({ access_key: '', secret_key: '', password: '' })
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(view.model.loaded.value).toMatchObject({ configured: true, selected: true, secret_key_set: true, secret_set: false, access_key_mask: '********' })
    expect(JSON.stringify(view.model.loaded.value)).not.toMatch(/actual-ak-only|actual-sk-only/)
  })
  it.each(['endpoint', 'access_key'] as const)('AK/SK 修改 %s 且有旧 SK 时，必须重填或禁用并清除', async key => {
    const view = setup(); await view.ready(akskConfigured())
    view.model.form[key] = key === 'endpoint' ? 'https://other.test.invalid' : 'new-ak'
    expect(await view.model.save()).toBe(false)
    expect(view.model.validationError.value).toContain('重新填写 SK')
    expect(view.saver).not.toHaveBeenCalled()
    view.model.form.enabled = false
    view.model.form.clear_secret_key = true
    if (key === 'access_key') view.model.form.access_key = 'new-ak'
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toMatchObject({ enabled: false, clear_secret_key: true })
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(view.model.loaded.value).toMatchObject({ configured: false, secret_key_set: false })
  })
  it('更换 AK 时重新填写 SK，不能将掩码或旧 SK 当作新凭据', async () => {
    const view = setup(); await view.ready(akskConfigured())
    Object.assign(view.model.form, { access_key: 'replacement-ak', secret_key: 'replacement-sk' })
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toMatchObject({ access_key: 'replacement-ak', secret_key: 'replacement-sk' })
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(JSON.stringify(view.model.loaded.value)).not.toMatch(/replacement-ak|replacement-sk/)
  })
  it('清除 SK 不能与新 SK 同时提交，也不能在启用时清除', async () => {
    const view = setup(); await view.ready(akskConfigured())
    view.model.form.clear_secret_key = true
    expect(await view.model.save()).toBe(false)
    view.model.form.enabled = false; view.model.form.secret_key = 'replacement-sk'
    expect(await view.model.save()).toBe(false)
    expect(view.model.form.secret_key).toBe('')
    expect(view.saver).not.toHaveBeenCalled()
  })
  it.each(['AKTE****LAST', 'bad ak', 'bad,ak', 'bad=ak', '非ASCII'] as const)('拒绝不合法或脱敏 AK：%s', async accessKey => {
    const view = setup(); await view.ready()
    view.model.form.access_key = accessKey; view.model.form.secret_key = 'draft-sk'
    expect(await view.model.save()).toBe(false)
    expect(view.saver).not.toHaveBeenCalled()
    expect(view.model.form).toMatchObject({ access_key: '', secret_key: '', password: '' })
  })
  it.each(['PASSWORD', 'AKSK'] as const)('切到 %s 清理另一方式草稿，启用时不能沿用另一方式的已存秘密', async authType => {
    const view = setup(); await view.ready(authType === 'AKSK' ? configured() : akskConfigured())
    Object.assign(view.model.form, { password: 'draft-password', access_key: 'draft-ak', secret_key: 'draft-sk',
      username: 'draft-user', organization: 'draft-org', clear_password: true, clear_secret_key: true, select_source: true })
    view.model.form.auth_type = authType
    expect(view.model.form).toMatchObject({ auth_type: authType, password: '', access_key: '', secret_key: '',
      username: '', organization: '', clear_password: false, clear_secret_key: false, select_source: false })
    if (authType === 'PASSWORD') view.model.form.username = 'replacement-user'
    else view.model.form.access_key = 'replacement-ak'
    expect(await view.model.save()).toBe(false)
    expect(view.saver).not.toHaveBeenCalled()
    view.model.form.enabled = false
    const saving = view.model.save()
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(view.model.loaded.value).toMatchObject({ auth_type: authType, configured: false, secret_set: false, secret_key_set: false, access_key_mask: null })
  })
  it('PASSWORD 不提交 AK/SK 草稿和清除选项', async () => {
    const view = setup(); await view.ready(configured())
    Object.assign(view.model.form, { access_key: 'unrelated-ak', secret_key: 'unrelated-sk', clear_secret_key: true })
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).not.toHaveProperty('access_key')
    expect(view.saver.mock.calls[0][1]).not.toHaveProperty('secret_key')
    expect(view.saver.mock.calls[0][1]).not.toHaveProperty('clear_secret_key')
    view.saves[0].result.resolve(undefined); expect(await saving).toBe(true)
    expect(view.model.form).toMatchObject({ access_key: '', secret_key: '' })
  })
  it('AK/SK 保存失败清理所有秘密，旧公开状态不被未保存内容覆盖', async () => {
    const view = setup(); await view.ready(akskConfigured())
    Object.assign(view.model.form, { access_key: 'failed-ak', secret_key: 'failed-sk', password: 'unrelated-password' })
    const saving = view.model.save(); view.saves[0].result.reject(new Error('failed-ak failed-sk'))
    expect(await saving).toBe(false)
    expect(view.model.form).toMatchObject({ access_key: '', secret_key: '', password: '' })
    expect(view.model.loaded.value?.access_key_mask).toBe('AKTE****LAST')
    expect(view.model.error.value).not.toMatch(/failed-ak|failed-sk/)
  })
  it.each(['close', 'tenant', 'permission', 'dispose'] as const)('%s 立即清理 AK/SK 草稿', async action => {
    const view = setup(); await view.ready(akskConfigured())
    Object.assign(view.model.form, { access_key: 'draft-ak', secret_key: 'draft-sk', password: 'draft-password' })
    if (action === 'close') view.visible.value = false
    if (action === 'tenant') view.tenant.value = 8
    if (action === 'permission') view.read.value = false
    if (action === 'dispose') stops.at(-1)!()
    expect(view.model.form).toMatchObject({ access_key: '', secret_key: '', password: '' })
    expect(view.model.loaded.value).toBeNull()
  })
  it('保存失败不假成功、不回填密码、不覆盖已保存的选中状态', async () => {
    const view = setup(); await view.ready(configured())
    view.model.form.password = 'private-secret'
    const saving = view.model.save(); view.saves[0].result.reject(new Error('private-secret'))
    expect(await saving).toBe(false); expect(view.model.error.value).toContain('保存')
    expect(view.model.error.value).not.toContain('private-secret'); expect(view.model.form.password).toBe('')
    expect(view.model.loaded.value?.selected).toBe(false)
  })
  it('租户切换取消读取和保存并清空所有秘密，迟到响应不能恢复旧租户', async () => {
    const view = setup(); const old = view.reads[0]
    view.tenant.value = 8
    expect(old.signal.aborted).toBe(true)
    old.result.resolve(configured()); await settle(); expect(view.model.loaded.value).toBeNull()
    await view.ready({ ...configured(), tenant_id: 8 })
    view.model.form.password = 'tenant-eight-secret'
    const saving = view.model.save(); view.tenant.value = 7
    expect(view.saves[0].signal.aborted).toBe(true); expect(view.model.form.password).toBe('')
    view.saves[0].result.resolve(undefined)
    expect(await saving).toBe(false); expect(view.model.loaded.value).toBeNull()
  })
  it('关闭和撤销权限立即清空秘密并中止请求，响应租户错配也不展示', async () => {
    const view = setup(); await view.ready(configured())
    view.model.form.password = 'draft-secret'; view.visible.value = false
    expect(view.model.form.password).toBe(''); expect(view.model.loaded.value).toBeNull()
    view.visible.value = true; view.read.value = false
    expect(view.reads.at(-1)!.signal.aborted).toBe(true)
    view.read.value = true; await view.ready({ ...configured(), tenant_id: 99 })
    expect(view.model.loaded.value).toBeNull(); expect(view.model.error.value).toContain('读取')
  })
})
