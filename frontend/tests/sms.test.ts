import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { SYSTEM_PERMS } from '../src/router/perms'
import type { Contact } from '../src/types'
import type { SmsNotification, SmsTenantConfig, SmsTenantUpdate } from '../src/types/sms'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import * as smsForm from '../src/views/system/sms-form'
import { recipientUnavailable } from '../src/utils/recipients'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import { allNodes, clickButton, findNode, formField, mountPersonnel, setField, settlePersonnel, textOf } from './personnelHarness'

const request = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/request', () => request)
import * as smsApi from '../src/api/sms'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.clearAllMocks() })
const config = (values: Partial<SmsTenantConfig> = {}): SmsTenantConfig => ({
  enabled: false, endpoint: '', username: '', password_set: false, expand_port: '', verify_tls: true,
  severities: ['P1', 'P2'], revision: 0, ...values,
})
const configured = () => config({ endpoint: 'https://sms-gateway.example.test/HttpSmsBatchSend', username: 'fixture-sms-user', password_set: true })
function api() {
  return {
    getSmsGlobal: vi.fn(async () => ({ enabled: false })), saveSmsGlobal: vi.fn(async (enabled: boolean) => ({ enabled })),
    getTenantSms: vi.fn(async (_id: number) => config()),
    saveTenantSms: vi.fn(async (_id: number, body: SmsTenantUpdate) => {
      const { password, clear_password, ...values } = body
      return config({ ...values, password_set: clear_password ? false : !!password, revision: 1 })
    }),
    getSmsNotifications: vi.fn(async (_id: number): Promise<SmsNotification[]> => []),
  }
}
const dependencies = (service: ReturnType<typeof api>) => ({
  '@/api/sms': service, '@/composables/useLatestRequest': { useLatestRequest }, './sms-form': smsForm,
})
async function tenant(service = api(), tenantIds: number[] | null = null, tenantId = 7) {
  const view = mountPersonnel('system/SmsTenantDialog.vue', { dependencies: dependencies(service), user: { id: 1, tenant_ids: tenantIds },
    props: { modelValue: true, tenantId, tenantName: '测试租户' } })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, service }
}

describe('短信接口仅配置与读取，不提供发送入口', () => {
  it('全局配置固定全部范围，租户配置与记录固定目标租户范围', async () => {
    await smsApi.getSmsGlobal()
    await smsApi.saveSmsGlobal(false)
    await smsApi.getTenantSms(7)
    await smsApi.saveTenantSms(8, smsForm.smsUpdate(smsForm.blankSmsDraft()))
    await smsApi.getSmsNotifications(8)
    expect(request.get).toHaveBeenCalledWith('/system/sms', undefined, { silent: true, headers: { 'X-Tenant-Id': 'all' } })
    expect(request.put).toHaveBeenCalledWith('/system/sms', { enabled: false }, expect.any(Object))
    expect(request.get).toHaveBeenCalledWith('/system/tenants/7/sms', undefined, { silent: true, headers: { 'X-Tenant-Id': '7' } })
    expect(request.put).toHaveBeenCalledWith('/system/tenants/8/sms', expect.objectContaining({ enabled: false, verify_tls: true }), { silent: true, headers: { 'X-Tenant-Id': '8' } })
    expect(request.get).toHaveBeenCalledWith('/system/sms/notifications', { tenant_id: 8 }, { silent: true, headers: { 'X-Tenant-Id': '8' } })
    expect(request.post).not.toHaveBeenCalled()
  })
})

describe('短信总开关默认关闭', () => {
  it('保存成功后通知系统设置更新租户发送状态，保存失败不发出成功事件', async () => {
    const service = api(), onSaved = vi.fn()
    const view = mountPersonnel('system/SmsGlobalPanel.vue', { dependencies: dependencies(service), props: { onSaved } })
    dispose.push(view.unmount)
    await settlePersonnel()
    await setField(view.root, '开启升级短信', true)
    await clickButton(view.root, '保存短信总开关')
    expect(onSaved).toHaveBeenCalledOnce()
    service.saveSmsGlobal.mockRejectedValueOnce(new Error('fixture save failure'))
    await setField(view.root, '开启升级短信', false)
    await clickButton(view.root, '保存短信总开关')
    expect(onSaved).toHaveBeenCalledOnce()
  })

  it('系统设置收到总开关保存事件后重新读取租户短信概况', async () => {
    const reload = vi.fn(async () => undefined)
    const Global = defineComponent({ emits: ['saved'], setup: (_props, { emit }) => () => h('button', { onClick: () => emit('saved') }, '保存总开关') })
    const Tenants = defineComponent({ setup(_props, { expose }) { expose({ reload }); return () => h('summary', '租户短信概况') } })
    const view = mountPersonnel('system/SettingsPanel.vue', { api: { getSettings: vi.fn(async () => ({})) }, dependencies: {
      './SmsGlobalPanel.vue': { default: Global }, './SmsTenantPanel.vue': { default: Tenants },
    } })
    dispose.push(view.unmount)
    await settlePersonnel()
    await (findNode(view.root, 'button', item => textOf(item) === '保存总开关').props.onClick as () => void)()
    await settlePersonnel()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('只有租户管理权限时仍可通过系统路由与菜单进入短信设置', () => {
    const permissions = new Set(['tenant:manage'])
    expect(SYSTEM_PERMS.some(permission => permissions.has(permission))).toBe(true)
  })

  it('读取默认关闭，不自动保存或发送，仅手动保存才修改总开关', async () => {
    const service = api()
    const view = mountPersonnel('system/SmsGlobalPanel.vue', { dependencies: dependencies(service) })
    dispose.push(view.unmount); await settlePersonnel()
    expect(findNode(view.root, 'Switch').props.modelValue).toBe(false)
    expect(service.saveSmsGlobal).not.toHaveBeenCalled()
    expect(allNodes(view.root).some(item => item.tag === 'Button' && /测试|发送/.test(textOf(item)))).toBe(false)
    await setField(view.root, '开启升级短信', true)
    expect(service.saveSmsGlobal).not.toHaveBeenCalled()
    await clickButton(view.root, '保存短信总开关')
    expect(service.saveSmsGlobal).toHaveBeenCalledExactlyOnceWith(true)
  })

  it('读取失败按关闭显示并锁定，重试不会自动开通', async () => {
    const service = api()
    service.getSmsGlobal.mockRejectedValueOnce(new Error('test-only unavailable'))
    const view = mountPersonnel('system/SmsGlobalPanel.vue', { dependencies: dependencies(service) })
    dispose.push(view.unmount); await settlePersonnel()
    const toggle = findNode(view.root, 'Switch')
    expect(toggle.props.modelValue).toBe(false)
    expect(toggle.props.disabled).toBe(true)
    expect(findNode(view.root, 'Button', item => textOf(item) === '保存短信总开关').props.disabled).toBe(true)
    await clickButton(view.root, '重试')
    expect(findNode(view.root, 'Switch').props.modelValue).toBe(false)
    expect(service.saveSmsGlobal).not.toHaveBeenCalled()
  })

  it('已有开启状态在刷新读取失败后也按关闭显示，不自动发起关闭写入', async () => {
    const service = api()
    service.getSmsGlobal.mockResolvedValueOnce({ enabled: true }).mockRejectedValueOnce(new Error('test-only'))
    const view = mountBusinessScript<{ load(): Promise<void>; enabled: boolean; failed: boolean }>('system/SmsGlobalPanel.vue', { dependencies: dependencies(service) })
    dispose.push(view.unmount)
    await view.state.load(); expect(view.state.enabled).toBe(true)
    await view.state.load(); expect(view.state.enabled).toBe(false)
    expect(view.state.failed).toBe(true)
    expect(service.saveSmsGlobal).not.toHaveBeenCalled()
  })

  it.each([null, [7]] as const)('总开关写权限与全租户范围同时生效：%j', async scope => {
    const service = api()
    const view = mountPersonnel('system/SmsGlobalPanel.vue', { dependencies: dependencies(service), user: { id: 1, tenant_ids: scope == null ? null : [...scope] }, can: () => scope != null })
    dispose.push(view.unmount); await settlePersonnel()
    expect(findNode(view.root, 'Switch').props.disabled).toBe(true)
    expect(allNodes(view.root).some(item => item.tag === 'Button' && textOf(item) === '保存短信总开关')).toBe(false)
    expect(service.saveSmsGlobal).not.toHaveBeenCalled()
  })
})

describe('租户短信配置独立且默认不发送', () => {
  it('默认关闭、空网关空账号、TLS 开启、级别 P1/P2，不读取 HCS 凭据或自动保存', async () => {
    const { root, service } = await tenant()
    expect(findNode(formField(root, '开启升级短信'), 'Switch').props.modelValue).toBe(false)
    expect(findNode(formField(root, '验证 TLS'), 'Switch').props.modelValue).toBe(true)
    expect(findNode(formField(root, '网关地址'), 'Input').props.modelValue).toBe('')
    expect(findNode(formField(root, '短信账号'), 'Input').props.modelValue).toBe('')
    expect(findNode(formField(root, '短信密码'), 'Input').props.modelValue).toBe('')
    expect(findNode(formField(root, '短信告警级别'), 'CheckboxGroup').props.modelValue).toEqual(['P1', 'P2'])
    expect(findNode(root, 'Alert', item => String(item.props.title).includes('总开关未开启'))).toBeTruthy()
    expect(service.saveTenantSms).not.toHaveBeenCalled()
    expect(allNodes(root).some(item => item.tag === 'Button' && /发送|测试|重发/.test(textOf(item)))).toBe(false)
    expect(textOf(root)).toContain('沿用通知路由')
  })

  it('默认关闭配置可保存，密码 / 开启时间 / 版本状态不回传', async () => {
    const { root, service } = await tenant()
    await clickButton(root, '保存短信配置')
    const body = service.saveTenantSms.mock.calls[0][1]
    expect(body).toMatchObject({ enabled: false, endpoint: '', username: '', verify_tls: true, severities: ['P1', 'P2'], clear_password: false })
    for (const key of ['password', 'password_set', 'enabled_since', 'revision']) expect(body).not.toHaveProperty(key)
  })

  it('已有密码不回显，留空保留；级别可选择 P3，TLS 不继承 HCS 设置', async () => {
    const service = api()
    service.getTenantSms.mockResolvedValueOnce(configured())
    const { root } = await tenant(service)
    const password = findNode(formField(root, '短信密码'), 'Input')
    expect(password.props.modelValue).toBe('')
    expect(password.props.placeholder).toContain('留空保留')
    await setField(root, '短信告警级别', ['P3'])
    await setField(root, '扩展端口', '12')
    await clickButton(root, '保存短信配置')
    expect(service.saveTenantSms).toHaveBeenCalledWith(7, expect.objectContaining({ severities: ['P3'], expand_port: '12', verify_tls: true }))
    expect(service.saveTenantSms.mock.calls[0][1]).not.toHaveProperty('password')
  })

  it('设置新密码后保存成功即清除明文输入，关闭再打开无残留', async () => {
    const { root, service, updateProps } = await tenant()
    await setField(root, '短信密码', 'fixture-sms-secret')
    await clickButton(root, '保存短信配置')
    expect(service.saveTenantSms.mock.calls[0][1].password).toBe('fixture-sms-secret')
    expect(findNode(formField(root, '短信密码'), 'Input').props.modelValue).toBe('')
    await setField(root, '短信密码', 'discard-on-close')
    await clickButton(root, '关闭')
    updateProps({ modelValue: false }); await settlePersonnel()
    updateProps({ modelValue: true }); await settlePersonnel()
    expect(findNode(formField(root, '短信密码'), 'Input').props.modelValue).toBe('')
  })

  it('清除密码为显式操作；启用时不允许同时清除密码', async () => {
    const service = api(); service.getTenantSms.mockResolvedValueOnce(configured())
    const { root, messages } = await tenant(service)
    const clear = findNode(root, 'Checkbox', item => textOf(item) === '清除已保存密码')
    ;(clear.props['onUpdate:modelValue'] as (value: boolean) => void)(true)
    await settlePersonnel()
    await setField(root, '开启升级短信', true)
    await clickButton(root, '保存短信配置')
    expect(messages.warning).toHaveBeenCalledWith(expect.stringContaining('不能同时清除'))
    expect(service.saveTenantSms).not.toHaveBeenCalled()
    await setField(root, '开启升级短信', false)
    await clickButton(root, '保存短信配置')
    expect(service.saveTenantSms).toHaveBeenCalledWith(7, expect.objectContaining({ enabled: false, clear_password: true }))
  })

  it('配置读取失败时不能保存，记录读取失败则不阻止保存关闭配置', async () => {
    const failed = api(); failed.getTenantSms.mockRejectedValueOnce(new Error('test-only'))
    const first = await tenant(failed)
    expect(findNode(first.root, 'Button', item => textOf(item) === '保存短信配置').props.disabled).toBe(true)
    const service = api(); service.getSmsNotifications.mockRejectedValueOnce(new Error('test-only records'))
    const second = await tenant(service)
    await clickButton(second.root, '保存短信配置')
    expect(service.saveTenantSms).toHaveBeenCalledOnce()
  })

  it('越权租户不发起配置请求，也不显示保存入口', async () => {
    const { root, service } = await tenant(api(), [8], 7)
    expect(service.getTenantSms).not.toHaveBeenCalled()
    expect(service.getSmsNotifications).not.toHaveBeenCalled()
    expect(allNodes(root).some(item => item.tag === 'Button' && textOf(item) === '保存短信配置')).toBe(false)
  })

  it('租户范围内的管理者可保存关闭配置；入口在“系统设置 → 租户短信”，租户接入卡片不再有短信配置', async () => {
    const { root, service } = await tenant(api(), [7], 7)
    await clickButton(root, '保存短信配置')
    expect(service.saveTenantSms.mock.calls[0][0]).toBe(7)
    const summaries = { global_enabled: false, items: [{ tenant_id: 7, tenant_name: '已授权租户', short_name: '租户七', tenant_enabled: true,
      enabled: true, configured: true, severities: ['P1' as const], effective: false }] }
    const listTenantSms = vi.fn(async () => summaries)
    const list = mountBusinessScript<{ load(): Promise<void>; open(row: unknown): void; items: unknown[]; statusText(row: unknown): string
      dialog: { visible: boolean; tenantId: number | null; tenantName: string } }>('system/SmsTenantPanel.vue', {
      dependencies: { '@/api/sms': { listTenantSms } },
    })
    dispose.push(list.unmount)
    await list.state.load(); await settleView()
    expect(listTenantSms).toHaveBeenCalledOnce(); expect(list.state.items).toEqual(summaries.items)
    expect(list.state.statusText(summaries.items[0])).toBe('总开关关闭')
    list.state.open(summaries.items[0])
    expect(list.state.dialog).toEqual({ visible: true, tenantId: 7, tenantName: '租户七' })
    const panel = mountBusinessScript<Record<string, unknown>>('system/TenantPanel.vue', {
      props: { info: null }, dependencies: {
        './tenant-form': {},
        '@/stores/user': { useUserStore: () => ({ user: { tenant_ids: [7] }, can: () => true }) },
      },
    })
    dispose.push(panel.unmount)
    expect('openSms' in panel.state).toBe(false); expect('openEcsPolicy' in panel.state).toBe(false)
  })

  it('记录只展示掩码号码，网关受理不标为送达，未知结果不提供重发', async () => {
    const service = api()
    const record = (status: string, id: number): SmsNotification => ({ id, status, phone_mask: '138****0000', tenant_id: 7,
      user_id: 101, alarm_id: 1, created_at: '2026-11-02T00:00:00Z', sent_at: null, error: null, gateway_message_id: null })
    service.getSmsNotifications.mockResolvedValueOnce([record('ACCEPTED', 1), record('UNKNOWN', 2)])
    const { root } = await tenant(service)
    expect(textOf(root)).toContain('138****0000')
    expect(textOf(root)).toContain('网关已受理')
    expect(textOf(root)).toContain('结果待核对')
    expect(textOf(root)).not.toContain('已送达')
    expect(allNodes(root).some(item => item.tag === 'Button' && /重发|发送|测试/.test(textOf(item)))).toBe(false)
  })
})

describe('短信配置跨租户请求隔离及密码清理', () => {
  it.each(['resolve', 'reject'] as const)('旧租户的 %s 不能覆盖新租户表单', async outcome => {
    const service = api(), pending = deferred<SmsTenantConfig>()
    service.getTenantSms.mockImplementation(id => id === 7 ? pending.promise : Promise.resolve(config({ username: 'tenant-eight' })))
    const { root, updateProps } = await tenant(service)
    updateProps({ tenantId: 8, tenantName: '另一租户' }); await settlePersonnel()
    if (outcome === 'resolve') pending.resolve(config({ enabled: true, username: 'old-tenant' }))
    else pending.reject(new Error('old-tenant failure'))
    await settlePersonnel()
    expect(findNode(formField(root, '短信账号'), 'Input').props.modelValue).toBe('tenant-eight')
    expect(findNode(formField(root, '开启升级短信'), 'Switch').props.modelValue).toBe(false)
  })

  it.each(['close', 'tenant', 'dispose'] as const)('%s 时同步清除表单中的明文密码', async action => {
    const service = api()
    const view = mountBusinessScript<{ form: smsForm.SmsTenantDraft }>('system/SmsTenantDialog.vue', { dependencies: dependencies(service), props: { modelValue: true, tenantId: 7 } })
    dispose.push(view.unmount); await settleView(); await settleView()
    view.state.form.password = 'discard-this-fixture'
    if (action === 'close') view.props.modelValue = false
    if (action === 'tenant') view.props.tenantId = 8
    if (action === 'dispose') view.unmount()
    expect(view.state.form.password).toBe('')
  })

  it('保存期间换租户，旧保存结果不回填新租户', async () => {
    const service = api(), pending = deferred<SmsTenantConfig>()
    service.getTenantSms.mockImplementation(async id => config({ username: id === 7 ? 'tenant-seven' : 'tenant-eight' }))
    service.saveTenantSms.mockImplementationOnce(() => pending.promise)
    const { root, updateProps } = await tenant(service)
    await setField(root, '短信密码', 'fixture-old-tenant-secret')
    const save = (findNode(root, 'Button', item => textOf(item) === '保存短信配置').props.onClick as () => Promise<void>)()
    updateProps({ tenantId: 8 }); await settlePersonnel()
    pending.resolve(config({ username: 'old-save-result', password_set: true }))
    await save; await settlePersonnel()
    expect(service.saveTenantSms.mock.calls[0][0]).toBe(7)
    expect(findNode(formField(root, '短信账号'), 'Input').props.modelValue).toBe('tenant-eight')
    expect(findNode(formField(root, '短信密码'), 'Input').props.modelValue).toBe('')
  })
})

describe('启用短信的必要条件', () => {
  it.each([
    { endpoint: '' }, { endpoint: '/HttpSmsBatchSend' }, { endpoint: 'https://host.test/path?token=fixture' },
    { username: '' }, { password: '' }, { severities: [] }, { expand_port: 'abc' },
  ])('无有效配置不能启用：%j', values => {
    const form = { ...smsForm.blankSmsDraft(), enabled: true, endpoint: 'https://host.test/HttpSmsBatchSend', username: 'fixture-user', password: 'fixture-secret', ...values }
    expect(smsForm.validateSmsDraft(form, false)).not.toBe('')
  })
  it('关闭配置允许空字段，已有密码可保持，P3 可用', () => {
    expect(smsForm.validateSmsDraft(smsForm.blankSmsDraft(), false)).toBe('')
    expect(smsForm.validateSmsDraft({ ...smsForm.blankSmsDraft(), enabled: true, endpoint: 'https://host.test/proxy', username: 'fixture-user', severities: ['P3'] }, true)).toBe('')
  })
  it.each(['ACCEPTED', 'SENT', 'GATEWAY_ACCEPTED'])('网关状态 %s 仅标为已受理', status => {
    expect(smsForm.smsStatusText(status)).toBe('网关已受理')
  })
})

describe('仅手机号人员仍由用户管理维护', () => {
  it('联系组允许已登记的仅手机号人员，显示手机号且继续提交 Contact ID', async () => {
    const contact: Contact = { id: 11, user_id: 101, username: 'phone-only', name: '短信人员', email: null, phone: '13800000000', enabled: true, groups: [] }
    const createGroup = vi.fn(async () => ({ id: 1 }))
    const view = mountPersonnel('notify/GroupDialog.vue', { api: { createGroup }, props: { visible: true, group: null, contacts: [contact] } })
    dispose.push(view.unmount)
    const option = findNode(view.root, 'Option')
    expect(option.props.disabled).toBe(false)
    expect(textOf(option)).toContain('13800000000')
    await setField(view.root, '代码', 'sms-team'); await setField(view.root, '名称', '短信组'); await setField(view.root, '成员', [11])
    await clickButton(view.root, '保存')
    expect(createGroup).toHaveBeenCalledWith(expect.objectContaining({ member_ids: [11] }))
  })
  it('邮件收件人选择仍要求邮箱，手机号不能代替邮箱', () => {
    expect(recipientUnavailable({ id: 101, username: 'phone-only', display_name: '短信人员', email: null, phone: '13800000000',
      enabled: true, notify_enabled: true, tenant_ids: null }, null, true)).toBe('缺少邮箱')
  })
})
