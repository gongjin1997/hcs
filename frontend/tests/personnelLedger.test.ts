import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Resource, ResourceDetail, ResourcePatch, UserOption } from '../src/types'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import * as columns from '../src/views/ledger/columns'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import { allNodes, clickButton, findNode, formField, mountPersonnel, setField, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
const resource = (overrides: Partial<Resource> = {}): Resource => ({
  id: 5, tenant_id: 7, service: 'ECS', resource_type: 'ecs_server', resource_id: 'qa-resource', name: '测试资源',
  status_raw: 'ACTIVE', health: 'OK', region: null, az: null, ip: null, spec: null, vcpus: null, memory_gb: null,
  storage_gb: null, created_time: null, labels: {}, attrs: {}, metrics: {}, expected_state: {}, owner: null, owner_user_id: null,
  business: null, env_type: null, remark: null, monitored: true, critical: false, lifecycle: 'ACTIVE',
  first_seen: '', last_seen: '', deleted_at: null, status_since: null, active_alarms: 0, ...overrides,
})
const detail = (value: Resource): ResourceDetail => ({ resource: value, tenant: null, alarms: [], changes: [], status_changes: [] })
const owners: UserOption[] = [
  { id: 101, username: 'alpha', display_name: '同名人员', email: null, phone: null, enabled: true, notify_enabled: false, tenant_ids: [7] },
  { id: 102, username: 'beta', display_name: '同名人员', email: 'beta@example.test', phone: null, enabled: true, notify_enabled: true, tenant_ids: null },
  { id: 103, username: 'disabled', display_name: '已停用人员', email: null, phone: null, enabled: false, notify_enabled: false, tenant_ids: [7] },
]
const dependencies = { './columns': columns, '@/composables/useLatestRequest': { useLatestRequest }, '@/utils/clipboard': { copyText: vi.fn() } }
async function drawer(value: Resource = resource(), failOptions = false) {
  const api = {
    getResource: vi.fn(async (_id: number) => detail(value)),
    listUserOptions: vi.fn(async (_tenant: number | null) => { if (failOptions) throw new Error('test-only unavailable'); return owners }),
    patchResource: vi.fn(async (_id: number, _body: ResourcePatch) => value),
  }
  const view = mountPersonnel('ledger/ResourceDrawer.vue', { api, dependencies, props: {
    modelValue: true, resourceId: value.id, inspectedTypes: [], options: { business: [] }, enforce: false,
  } })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, api }
}

describe('资源负责人稳定用户 ID', () => {
  it.each([7, null])('按资源租户 %s 请求人员，选择显示姓名与账号，通知关闭不影响负责人资格', async tenantId => {
    const { root, api } = await drawer(resource({ tenant_id: tenantId }))
    expect(api.listUserOptions).toHaveBeenCalledWith(tenantId)
    const select = findNode(formField(root, '负责人'), 'Select')
    expect(select.props['allow-create']).not.toBe(true)
    expect(findNode(root, 'Option', item => item.props.label === '同名人员（alpha）').props.value).toBe(101)
    expect(allNodes(root).some(item => item.tag === 'Option' && item.props.value === 103)).toBe(false)
    await setField(root, '负责人', 101)
    await clickButton(root, '保存')
    expect(api.patchResource).toHaveBeenCalledWith(5, expect.objectContaining({ owner_user_id: 101 }))
    expect(api.patchResource.mock.calls[0][1]).not.toHaveProperty('owner')
  })

  it('未匹配的历史姓名继续显示，修改备注时不清空也不重发 owner', async () => {
    const { root, api } = await drawer(resource({ owner: '历史未匹配人员' }))
    expect(textOf(root)).toContain('历史未匹配人员（历史未关联）')
    await setField(root, '备注', '只修改备注')
    await clickButton(root, '保存')
    expect(api.patchResource.mock.calls[0][1]).not.toHaveProperty('owner_user_id')
    expect(api.patchResource.mock.calls[0][1]).not.toHaveProperty('owner')
  })

  it('历史负责人可明确清空，发送 owner_user_id=null', async () => {
    const { root, api } = await drawer(resource({ owner: '历史未匹配人员' }))
    await clickButton(root, '清空历史负责人')
    await clickButton(root, '保存')
    expect(api.patchResource).toHaveBeenCalledWith(5, expect.objectContaining({ owner_user_id: null }))
  })

  it('已关联但当前不可选的历史人员只读展示，其他修改保留关联', async () => {
    const { root, api } = await drawer(resource({ owner: '离任人员', owner_user_id: 999 }))
    const historical = findNode(root, 'Option', item => item.props.value === 999)
    expect(historical.props.disabled).toBe(true)
    expect(historical.props.label).toBe('离任人员（当前不可选）')
    await setField(root, '备注', '保留负责人')
    await clickButton(root, '保存')
    expect(api.patchResource.mock.calls[0][1]).not.toHaveProperty('owner_user_id')
  })

  it('清空当前关联发送 null，非法输入不能作为新负责人提交', async () => {
    const { root, api } = await drawer(resource({ owner: '同名人员', owner_user_id: 101 }))
    await setField(root, '负责人', 999)
    await clickButton(root, '保存')
    expect(api.patchResource).not.toHaveBeenCalled()
    await setField(root, '负责人', null)
    await clickButton(root, '保存')
    expect(api.patchResource).toHaveBeenCalledWith(5, expect.objectContaining({ owner_user_id: null }))
  })

  it('人员选项加载失败不阻止编辑其他字段或清空历史值', async () => {
    const { root, api } = await drawer(resource({ owner: '历史值' }), true)
    expect(findNode(formField(root, '负责人'), 'Select').props.disabled).toBe(true)
    expect(textOf(root)).toContain('人员选项加载失败')
    await setField(root, '备注', '仍可保存')
    await clickButton(root, '保存')
    expect(api.patchResource.mock.calls[0][1]).not.toHaveProperty('owner_user_id')
  })

  it('切换资源后旧租户人员响应不能覆盖新资源选项', async () => {
    const first = deferred<UserOption[]>()
    const api = {
      getResource: vi.fn(async (id: number) => detail(resource({ id, tenant_id: id === 5 ? 7 : 8 }))),
      listUserOptions: vi.fn((tenantId: number) => tenantId === 7 ? first.promise : Promise.resolve([owners[1]])),
    }
    const view = mountBusinessScript<{ ownerOptions: UserOption[]; data: ResourceDetail | null }>('ledger/ResourceDrawer.vue', {
      api, dependencies, props: { modelValue: true, resourceId: 5, inspectedTypes: [], options: { business: [] } },
    })
    dispose.push(view.unmount)
    await settleView()
    view.props.resourceId = 6
    await settleView(); await settleView()
    first.resolve([owners[0]])
    await settleView()
    expect(view.state.data?.resource.id).toBe(6)
    expect(view.state.ownerOptions.map(owner => owner.id)).toEqual([102])
  })
})

describe('台账仅维护业务系统字典', () => {
  it('不读取 / 不保存负责人字典，兼容派生 owners 名称字段', async () => {
    const api = {
      getLedgerDict: vi.fn(async () => ({ business: [{ name: '业务系统', remark: null }], owners: [{ name: '派生人员' }], business_outside: [], owners_outside: [], enforce: true })),
      saveLedgerDict: vi.fn(async () => undefined),
    }
    const view = mountBusinessScript<{ save(): Promise<void>; lists: Record<string, unknown[]>; COLS: { key: string }[] }>('ledger/LedgerDictDialog.vue', {
      api, props: { visible: false },
    })
    dispose.push(view.unmount)
    view.props.visible = true
    await settleView()
    expect(view.state.COLS.map(column => column.key)).toEqual(['business'])
    expect(view.state.lists).not.toHaveProperty('owners')
    await view.state.save()
    expect(api.saveLedgerDict).toHaveBeenCalledWith({ business: [{ name: '业务系统', remark: null }], enforce: true })
  })
})
