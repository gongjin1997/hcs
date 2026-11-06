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
  endpoint: null, username: null, organization: null, secret_set: false, ...overrides,
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

describe('ManageOne 原生配置状态与秘密隔离', () => {
  it('新建默认关闭，密码为空，选择来源必须显式勾选', async () => {
    const { model, ready } = setup(); await ready()
    expect(model.form).toMatchObject({ enabled: false, password: '', select_source: false, clear_password: false })
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
    Object.assign(view.model.form, { enabled: true, endpoint: 'https://manageone.test.invalid', username: 'integration', password: 'test-secret-only', select_source: true })
    const saving = view.model.save()
    expect(view.saver.mock.calls[0][1]).toEqual({ enabled: true, endpoint: 'https://manageone.test.invalid', username: 'integration', organization: '', password: 'test-secret-only', clear_password: false, select_source: true })
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
    view.reads[0].result.resolve({ ...configured(), password: 'unexpected-secret' } as ManageOneConfig)
    await settle()
    expect(view.model.form.password).toBe('')
    expect('password' in (view.model.loaded.value as unknown as Record<string, unknown>)).toBe(false)
    expect(JSON.stringify(view.model.loaded.value)).not.toContain('unexpected-secret')
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
