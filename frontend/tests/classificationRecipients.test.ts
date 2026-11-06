import { afterEach, describe, expect, it, vi } from 'vitest'
import * as tenantForm from '../src/views/system/tenant-form'
import * as columns from '../src/views/ledger/columns'
import type { ClassificationConfig, ClassificationRule, UserOption } from '../src/types'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import * as recipients from '../src/utils/recipients'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.unstubAllGlobals() })
const baseUser = (values: Partial<UserOption> = {}): UserOption => ({ id: 101, username: 'alpha', display_name: '同名人员', email: 'alpha@example.test', phone: null, enabled: true, notify_enabled: true, tenant_ids: null, ...values })
const optionDependencies = { '@/composables/useLatestRequest': { useLatestRequest }, '@/utils/recipients': recipients }
async function recipientSelect(users: UserOption[], props: Record<string, unknown> = {}) {
  const api = { listUserOptions: vi.fn(async () => users) }
  const changed = vi.fn()
  const view = mountPersonnel('../components/RecipientSelect.vue', { api, dependencies: optionDependencies, props: { modelValue: [], 'onUpdate:modelValue': changed, ...props } })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, api, changed }
}

describe('统一收件人选择', () => {
  it('显示姓名、账号和邮箱，值为用户 ID，没有手填邮箱', async () => {
    const { root, api, changed } = await recipientSelect([baseUser()])
    expect(api.listUserOptions).toHaveBeenCalledWith(null, undefined, true)
    const option = findNode(root, 'Option')
    expect(option.props.label).toBe('同名人员（alpha） <alpha@example.test>')
    expect(option.props.value).toBe(101)
    const select = findNode(root, 'Select')
    expect(select.props['allow-create']).not.toBe(true)
    ;(select.props['onUpdate:modelValue'] as (ids: number[]) => void)([101, 999])
    expect(changed).toHaveBeenCalledWith([101])
  })

  it.each([
    { enabled: false }, { notify_enabled: false }, { email: null }, { tenant_ids: [] }, { tenant_ids: [8] },
  ])('不允许新选不可用人员：%j', async overrides => {
    const { root, changed } = await recipientSelect([baseUser(overrides)], { tenantId: 7 })
    expect(findNode(root, 'Option').props.disabled).toBe(true)
    ;(findNode(root, 'Select').props['onUpdate:modelValue'] as (ids: number[]) => void)([101])
    expect(changed).toHaveBeenCalledWith([])
  })

  it('全局报表仅可新选全部租户人员，租户报表按租户授权选择', async () => {
    const users = [baseUser(), baseUser({ id: 102, tenant_ids: [7] })]
    const { root, api, updateProps } = await recipientSelect(users, { globalOnly: true })
    expect(findNode(root, 'Option', item => item.props.value === 102).props.disabled).toBe(true)
    updateProps({ globalOnly: false, tenantId: 7 })
    await settlePersonnel()
    expect(api.listUserOptions).toHaveBeenLastCalledWith(7, undefined, true)
    expect(findNode(root, 'Option', item => item.props.value === 102).props.disabled).toBe(false)
  })

  it('既有停用 / 缺失人员保留可见，移除后不能重新选择', async () => {
    const { root, changed, updateProps } = await recipientSelect([baseUser({ enabled: false })], { modelValue: [101, 999] })
    expect(findNode(root, 'Option', item => item.props.value === 101).props.label).toContain('已停用')
    expect(findNode(root, 'Option', item => item.props.value === 999).props.label).toContain('当前不可选')
    expect(findNode(root, 'Option', item => item.props.value === 101).props.disabled).toBe(false)
    ;(findNode(root, 'Select').props['onUpdate:modelValue'] as (ids: number[]) => void)([])
    expect(changed).toHaveBeenLastCalledWith([])
    updateProps({ modelValue: [] }); await settlePersonnel()
    expect(findNode(root, 'Option', item => item.props.value === 101).props.disabled).toBe(true)
    expect(allNodes(root).some(item => item.tag === 'Option' && item.props.value === 999)).toBe(false)
  })

  it('切换租户后旧响应不能覆盖当前选项', async () => {
    const pending = deferred<UserOption[]>()
    const api = { listUserOptions: vi.fn((tenant: number) => tenant === 7 ? pending.promise : Promise.resolve([baseUser({ id: 102, tenant_ids: [8] })])) }
    const view = mountPersonnel('../components/RecipientSelect.vue', { api, dependencies: optionDependencies, props: { modelValue: [], tenantId: 7 } })
    dispose.push(view.unmount)
    await settlePersonnel()
    view.updateProps({ tenantId: 8 }); await settlePersonnel()
    pending.resolve([baseUser()]); await settlePersonnel()
    expect(findNode(view.root, 'Option').props.value).toBe(102)
  })

  it('加载失败可重试，关闭的弹窗不加载选项', async () => {
    const api = { listUserOptions: vi.fn<() => Promise<UserOption[]>>().mockRejectedValueOnce(new Error('test-only')).mockResolvedValueOnce([baseUser()]) }
    const view = mountPersonnel('../components/RecipientSelect.vue', { api, dependencies: optionDependencies, props: { modelValue: [], active: false } })
    dispose.push(view.unmount)
    expect(api.listUserOptions).not.toHaveBeenCalled()
    view.updateProps({ active: true }); await settlePersonnel()
    expect(textOf(view.root)).toContain('人员选项加载失败')
    await clickButton(view.root, '重试')
    expect(findNode(view.root, 'Option').props.value).toBe(101)
  })
})

const rule = (id: string, category: string): ClassificationRule => ({ id, resource_type: 'ecs_server', field: 'os', pattern: category, category, enabled: true })
const config = (): ClassificationConfig => ({ rules: [rule('mrs', 'MRS'), rule('cce', 'CCE')], fallback: '其他', categories: ['MRS', 'CCE', '其他'], counts: { MRS: 2 } })
async function mapping(tenantIds: number[] | null = null, source = config()) {
  const api = { getClassifications: vi.fn(async () => source), saveClassifications: vi.fn(async (_body: ClassificationConfig) => source) }
  const view = mountPersonnel('ledger/LedgerClassificationDialog.vue', { api, dependencies: optionDependencies, user: { id: 1, tenant_ids: tenantIds }, props: { visible: true, resourceTypes: [{ code: 'ecs_server', name: '云服务器' }] } })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, api }
}
describe('分类映射编辑', () => {
  it('按现有顺序显示和保存，移动规则不更改 ID / 资源类型 / 匹配字段', async () => {
    const { root, api } = await mapping()
    expect(textOf(root)).toContain('首次命中')
    const down = allNodes(root).find(item => item.tag === 'Button' && textOf(item) === '下移' && !item.props.disabled)!
    await (down.props.onClick as () => void)(); await settlePersonnel()
    await clickButton(root, '保存')
    expect(api.saveClassifications).toHaveBeenCalledWith({ rules: [rule('cce', 'CCE'), rule('mrs', 'MRS')], fallback: '其他' })
    expect(api.saveClassifications.mock.calls[0][0]).not.toHaveProperty('counts')
  })

  it('新规则须填完整，填好后可保存，源数据不被草稿修改', async () => {
    const source = config()
    const { root, api, messages } = await mapping(null, source)
    await clickButton(root, '添加规则')
    await clickButton(root, '保存')
    expect(messages.warning).toHaveBeenCalledWith('请填写资源类型、匹配文字和分类')
    expect(api.saveClassifications).not.toHaveBeenCalled()
    const inputs = allNodes(root).filter(item => item.tag === 'Input' && item.props.modelValue === '')
    ;(inputs[0].props['onUpdate:modelValue'] as (value: string) => void)(' HIS ')
    ;(inputs[1].props['onUpdate:modelValue'] as (value: string) => void)(' HIS ')
    await settlePersonnel(); await clickButton(root, '保存')
    expect(api.saveClassifications.mock.calls[0][0].rules[2]).toMatchObject({ pattern: 'HIS', category: 'HIS', enabled: true })
    expect(source.rules).toHaveLength(2)
  })

  it('最多 200 条，受限租户只有只读表格', async () => {
    const source = { ...config(), rules: Array.from({ length: 200 }, (_, i) => rule(String(i), 'MRS')) }
    const full = await mapping(null, source)
    expect(findNode(full.root, 'Button', item => textOf(item) === '添加规则').props.disabled).toBe(true)
    const scoped = await mapping([7])
    expect(allNodes(scoped.root).some(item => item.tag === 'Button' && /添加规则|保存|上移|下移|删除/.test(textOf(item)))).toBe(false)
    expect(findNode(scoped.root, 'Input').props.disabled).toBe(true)
  })

  it('加载中 / 加载失败不能保存空规则，避免覆盖已有配置', async () => {
    const pending = deferred<ClassificationConfig>()
    const api = { getClassifications: vi.fn(() => pending.promise), saveClassifications: vi.fn() }
    const view = mountPersonnel('ledger/LedgerClassificationDialog.vue', { api, dependencies: optionDependencies, props: { visible: true, resourceTypes: [] } })
    dispose.push(view.unmount)
    expect(findNode(view.root, 'Button', item => textOf(item) === '保存').props.disabled).toBe(true)
    pending.reject(new Error('test-only')); await settlePersonnel()
    expect(findNode(view.root, 'Button', item => textOf(item) === '保存').props.disabled).toBe(true)
  })

  it('内网 HTTP 环境没有 randomUUID 时仍可添加规则', async () => {
    vi.stubGlobal('crypto', undefined)
    const { root } = await mapping()
    await clickButton(root, '添加规则')
    expect(textOf(root)).toContain('3 / 200')
  })
})

describe('收件人写入稳定 ID', () => {
  it('平台告警保存不回传派生的 mail_to', async () => {
    const api = { getSettings: vi.fn(async () => ({ platform_alarm: { label: '平台告警', fields: [], editable: true, value: {
      enabled: true, task_fail_threshold: 3, auth_fail_alarm: true, notify_fail_alarm: true, heartbeat_timeout_min: 3,
      severity: 'P2', mail_to: ['old@example.test'], recipient_user_ids: [101],
    } } })), saveSetting: vi.fn(async () => ({ changed: ['recipient_user_ids'] })) }
    const view = mountBusinessScript<{ load(): Promise<void>; savePa(): Promise<void>; pa: { recipient_user_ids: number[] }; paDirty: boolean }>('system/SettingsPanel.vue', { api })
    dispose.push(view.unmount); await view.state.load()
    expect(view.state.paDirty).toBe(false)
    view.state.pa.recipient_user_ids = [102]
    await view.state.savePa()
    expect(api.saveSetting).toHaveBeenCalledWith('platform_alarm', expect.objectContaining({ recipient_user_ids: [102] }))
    expect(api.saveSetting.mock.calls[0]).not.toContain('old@example.test')
    expect((api.saveSetting.mock.calls[0] as unknown[])[1]).not.toHaveProperty('mail_to')
  })

  it('定时报表弹窗从 IDs 回填并只保存 IDs', async () => {
    const api = { saveReportSchedule: vi.fn(async (_body: unknown) => ({})) }
    const view = mountBusinessScript<{ schedule: unknown; sched: { recipient_user_ids: number[] }; openSchedule(): void; saveSchedule(): Promise<void> }>('system/TaskPanel.vue', { api })
    dispose.push(view.unmount)
    view.state.schedule = { cron: { DAILY: '0 9 * * *' }, enabled: { DAILY: true }, recipient_user_ids: [101], mail_to: ['old@example.test'], editable: true }
    view.state.openSchedule()
    expect(view.state.sched.recipient_user_ids).toEqual([101])
    view.state.sched.recipient_user_ids = []
    await view.state.saveSchedule()
    expect(api.saveReportSchedule).toHaveBeenCalledWith({ cron: { DAILY: '0 9 * * *' }, enabled: { DAILY: true }, recipient_user_ids: [] })
  })

  it('租户表单保留人员 ID，派生邮箱不进入新增 / 修改载荷', () => {
    const fresh = tenantForm.blankTenant()
    expect(fresh.report_user_ids).toEqual([])
    expect(fresh).not.toHaveProperty('report_mail_to')
    const existing = { ...fresh, id: 7, report_user_ids: [101], report_mail_to: ['resolved@example.test'],
      metrics_effective: 'AUTO', capabilities: {}, last_check: {}, last_check_at: null, resource_count: 0,
      user_count: 0, access_key_mask: null, secret_set: true } as Parameters<typeof tenantForm.toTenantIn>[0]
    const body = tenantForm.cleanTenantIn(tenantForm.toTenantIn(existing))
    expect(body.report_user_ids).toEqual([101])
    expect(body).not.toHaveProperty('report_mail_to')
    expect(body.report_user_ids).not.toBe(existing.report_user_ids)
  })

  it('手动发送使用报表的默认人员和租户，不从历史邮箱猜账号', async () => {
    const api = { sendReport: vi.fn(async () => undefined), listReports: vi.fn(async () => ({ items: [], total: 0 })), getReport: vi.fn(async () => ({})) }
    const view = mountBusinessScript<{ current: unknown; send: { recipient_user_ids: number[]; reportTenantId: number | null }; openSend(): Promise<void> }>('reports/ReportView.vue', { api, app: { tenantId: null } })
    dispose.push(view.unmount)
    const ids = [101]
    view.state.current = { id: 9, title: '租户日报', tenant_id: 7, default_user_ids: ids, default_to: ['resolved@example.test'], sent_to: ['old@example.test'] }
    await view.state.openSend()
    expect(view.state.send.reportTenantId).toBe(7)
    expect(view.state.send.recipient_user_ids).toEqual([101])
    ids.push(102)
    expect(view.state.send.recipient_user_ids).toEqual([101])
  })
})

describe('台账分类筛选与导出', () => {
  it('读取 types 的分类统计，列表和导出均携带当前分类', async () => {
    const api = {
      resourceMeta: vi.fn(async () => ({ types: [], categories: [], options: { business: [], owner: [] }, classifications: { categories: ['MRS', '其他'], counts: { MRS: 2 }, total: 2 } })),
      ledgerSummary: vi.fn(async () => null), getClassifications: vi.fn(async () => config()),
      listResources: vi.fn(async (_query: unknown) => ({ items: [], total: 0 })), exportLedger: vi.fn(async (_query: unknown) => '台账.xlsx'),
    }
    const view = mountBusinessScript<{
      filters: { classification: string }; classificationOptions: string[]; sel: { kind: string; code: string }
      loadMeta(): Promise<void>; loadClassifications(): Promise<void>; search(): Promise<void>; onExport(): Promise<void>
    }>('ledger/LedgerView.vue', { api, dependencies: { './columns': columns } })
    dispose.push(view.unmount)
    await view.state.loadMeta()
    expect(view.state.classificationOptions).toEqual(['MRS', '其他'])
    view.state.filters.classification = 'MRS'
    await view.state.search(); await view.state.onExport()
    expect(api.listResources).toHaveBeenCalledWith(expect.objectContaining({ classification: 'MRS' }),
      { headers: { 'X-Tenant-Id': '7' }, signal: expect.any(AbortSignal) })
    expect(api.exportLedger).toHaveBeenCalledWith({ classification: 'MRS' },
      { headers: { 'X-Tenant-Id': '7' }, signal: expect.any(AbortSignal) })
  })

  it('旧 types 没有分类元数据时，从映射配置生成筛选项', async () => {
    const api = { getClassifications: vi.fn(async () => config()), listResources: vi.fn(async () => ({ items: [], total: 0 })) }
    const view = mountBusinessScript<{ loadClassifications(): Promise<void>; classificationOptions: string[] }>('ledger/LedgerView.vue', { api, dependencies: { './columns': columns } })
    dispose.push(view.unmount)
    await view.state.loadClassifications(); await settleView()
    expect(view.state.classificationOptions).toEqual(['MRS', 'CCE', '其他'])
  })
})
