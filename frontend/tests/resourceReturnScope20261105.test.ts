import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter, type LocationQueryRaw } from 'vue-router'
import type { RequestConfig } from '../src/api/request'
import { useResourceRouteScope } from '../src/composables/useResourceRouteScope'
import { useTable } from '../src/composables/useTable'
import * as columns from '../src/views/ledger/columns'
import { deferred } from './businessViewHarness'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).reverse().forEach(stop => stop()))
const empty = { items: [], total: 0, page: 1, page_size: 20 }

async function setup(kind: 'ledger' | 'alarms', options: { tenant?: number | null; query?: LocationQueryRaw; tenantIds?: number[] | null;
  permissions?: string[]; configure?: (api: ReturnType<typeof apiFactory>) => void } = {}) {
  const state = reactive({ tenantId: options.tenant === undefined ? null : options.tenant, tenantLabel: '当前范围', stats: null, eventSeq: 0,
    user: { id: 1, tenant_ids: options.tenantIds === undefined ? [7, 8] : options.tenantIds } as { id: number; tenant_ids: number[] | null } | null,
    permissions: options.permissions ?? ['ledger:view', 'alarm:view'], refreshStats: vi.fn(async () => undefined),
    setTenant: vi.fn((id: number | null) => { state.tenantId = id }),
  })
  const api = apiFactory(); options.configure?.(api)
  const path = `/${kind}`, router = createRouter({ history: createMemoryHistory(), routes: [{ path, component: {} }] })
  await router.push({ path, query: options.query ?? {} }); await router.isReady()
  const view = mountPersonnel(kind === 'ledger' ? 'ledger/LedgerView.vue' : 'alarms/AlarmListView.vue', { api, dependencies: {
    '@/composables/useResourceRouteScope': { useResourceRouteScope }, '@/composables/useTable': { useTable }, './columns': columns,
    '@/stores/app': { useAppStore: () => state },
    '@/stores/user': { useUserStore: () => ({ get user() { return state.user }, can: (key: string) => state.permissions.includes(key) }) },
    'vue-router': { useRoute: () => ({ get query() { return router.currentRoute.value.query } }), useRouter: () => router },
  } })
  dispose.push(view.unmount); await settlePersonnel()
  return { ...view, api, state, router }
}
function apiFactory() {
  return {
    listAlarms: vi.fn(async (_query: object, _config?: RequestConfig) => empty),
    listResources: vi.fn(async (_query: object, _config?: RequestConfig) => empty),
    resourceMeta: vi.fn(async (_config?: RequestConfig) => ({ types: [], categories: [], options: { owner: [], business: [] } })),
    ledgerSummary: vi.fn(async (_config?: RequestConfig) => null),
    getClassifications: vi.fn(async () => ({ rules: [], fallback: '其他' })),
    exportAlarms: vi.fn(async (_query: object, _config?: RequestConfig) => '告警列表.xlsx'),
    exportLedger: vi.fn(async (_query: object, _config?: RequestConfig) => '资源台账.xlsx'),
  }
}

describe('监控返回告警与台账的实际查询范围', () => {
  it('告警浏览器后退恢复资源过滤，再后退关闭详情且移除资源过滤', async () => {
    const view = await setup('alarms', { tenant: 7, query: { tenant_id: '7' } })
    await view.router.push({ query: { resource_ref: '901', id: '91', tenant_id: '7' } }); await settlePersonnel()
    await view.router.push({ query: { resource_ref: '902', id: '92', tenant_id: '7' } }); await settlePersonnel()
    view.router.back(); await settlePersonnel(); await settlePersonnel()
    expect(view.api.listAlarms.mock.calls.at(-1)![0]).toMatchObject({ resource_ref: 901 })
    expect(findNode(view.root, 'Component', node => node.props.modelValue === 91).props.modelValue).toBe(91)
    view.router.back(); await settlePersonnel(); await settlePersonnel()
    expect(view.api.listAlarms.mock.calls.at(-1)![0]).toMatchObject({ resource_ref: undefined })
    expect(allNodes(view.root).some(node => node.tag === 'Component' && node.props.modelValue === 91)).toBe(false)
    expect(view.api.listAlarms.mock.calls.every(([, config]) => (config!.headers as Record<string, string>)['X-Tenant-Id'] === '7')).toBe(true)
  })
  it('台账浏览器后退恢复类型与详情，再后退到无 id 时关闭旧详情', async () => {
    const view = await setup('ledger', { tenant: 7, query: { tenant_id: '7' } })
    await view.router.push({ query: { type: 'dcs_instance', id: '901', tenant_id: '7' } }); await settlePersonnel()
    await view.router.push({ query: { type: 'ecs_server', id: '902', tenant_id: '7' } }); await settlePersonnel()
    view.router.back(); await settlePersonnel(); await settlePersonnel()
    expect(view.api.listResources.mock.calls.at(-1)![0]).toMatchObject({ resource_type: 'dcs_instance' })
    expect(findNode(view.root, 'Component', node => 'resource-id' in node.props).props).toMatchObject({ modelValue: true, 'resource-id': 901 })
    view.router.back(); await settlePersonnel(); await settlePersonnel()
    expect(view.api.listResources.mock.calls.at(-1)![0]).toMatchObject({ resource_type: undefined, category: 'ledger' })
    expect(findNode(view.root, 'Component', node => 'resource-id' in node.props).props).toMatchObject({ modelValue: false, 'resource-id': null })
    expect(view.api.listResources.mock.calls.every(([, config]) => (config!.headers as Record<string, string>)['X-Tenant-Id'] === '7')).toBe(true)
  })
  it.each(['ledger', 'alarms'] as const)('%s 的重复资源/告警 ID 不回退到宽范围列表', async kind => {
    const view = await setup(kind, { query: kind === 'ledger' ? { id: ['901', '902'], tenant_id: '7' }
      : { resource_ref: ['901', '902'], tenant_id: '7' } })
    expect(view.api.listResources).not.toHaveBeenCalled(); expect(view.api.listAlarms).not.toHaveBeenCalled()
    expect(view.api.resourceMeta).not.toHaveBeenCalled()
    expect(allNodes(view.root).some(node => node.tag === 'Alert' && node.props.type === 'error')).toBe(true)
  })
  it('告警首次加载与同页切换 resource_ref 都向后端提交真实过滤，并复用导出', async () => {
    const view = await setup('alarms', { query: { resource_ref: '901', tenant_id: '7' } })
    expect(view.state.setTenant).toHaveBeenCalledExactlyOnceWith(7); expect(view.state.tenantId).toBe(7)
    expect(view.api.listAlarms).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ resource_ref: 901 }),
      expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    await view.router.push({ query: { resource_ref: '902', tenant_id: '7' } }); await settlePersonnel()
    expect(view.api.listAlarms.mock.calls.at(-1)![0]).toMatchObject({ resource_ref: 902 })
    await clickButton(view.root, '导出 Excel')
    expect(view.api.exportAlarms).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ resource_ref: 902 }),
      expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    await view.router.push({ query: { tenant_id: '7' } }); await settlePersonnel()
    expect(view.api.listAlarms.mock.calls.at(-1)![0]).toMatchObject({ resource_ref: undefined })
  })
  it('台账进入明确租户后类型、目录、汇总和导出均使用该范围，详情按本地主键打开', async () => {
    const view = await setup('ledger', { query: { id: '901', type: 'dcs_instance', tenant_id: '7' } })
    expect(view.state.setTenant).toHaveBeenCalledExactlyOnceWith(7)
    expect(view.api.listResources).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ resource_type: 'dcs_instance' }),
      expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    expect(view.api.resourceMeta).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    expect(view.api.ledgerSummary).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    expect(findNode(view.root, 'Component', node => 'resource-id' in node.props).props).toMatchObject({ modelValue: true, 'resource-id': 901 })
    await clickButton(view.root, '导出 Excel')
    expect(view.api.exportLedger).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ resource_type: 'dcs_instance' }),
      expect.objectContaining({ headers: { 'X-Tenant-Id': '7' } }))
    await view.router.push({ query: { type: 'ecs_server', id: '902', tenant_id: '7' } }); await settlePersonnel()
    expect(view.api.listResources.mock.calls.at(-1)![0]).toMatchObject({ resource_type: 'ecs_server' })
    expect(findNode(view.root, 'Component', node => 'resource-id' in node.props).props['resource-id']).toBe(902)
  })
  it.each(['ledger', 'alarms'] as const)('%s 拒绝已选其他租户、越权租户和非法重复参数，不发目录/列表请求', async kind => {
    for (const options of [
      { tenant: 8, query: { tenant_id: '7' } }, { tenantIds: [8], query: { tenant_id: '7' } },
      { query: { tenant_id: ['7', '8'] } }, { query: { tenant_id: '1e2' } }, { permissions: [], query: { tenant_id: '7' } },
    ]) {
      const view = await setup(kind, options)
      expect(view.state.setTenant).not.toHaveBeenCalled()
      expect(view.api.listResources).not.toHaveBeenCalled(); expect(view.api.listAlarms).not.toHaveBeenCalled()
      expect(view.api.resourceMeta).not.toHaveBeenCalled(); expect(view.api.ledgerSummary).not.toHaveBeenCalled()
      expect(allNodes(view.root).some(node => node.tag === 'Alert' && node.props.type === 'error')).toBe(true)
    }
  })
  it.each(['ledger', 'alarms'] as const)('%s 切租户立即关闭详情和取消旧请求，迟到结果不回填', async kind => {
    const pending = deferred<typeof empty>()
    const view = await setup(kind, { query: kind === 'ledger' ? { id: '901', tenant_id: '7' } : { resource_ref: '901', tenant_id: '7' }, configure: api => {
      if (kind === 'ledger') api.listResources.mockReturnValueOnce(pending.promise)
      else api.listAlarms.mockReturnValueOnce(pending.promise)
    } })
    const calls = kind === 'ledger' ? view.api.listResources.mock.calls : view.api.listAlarms.mock.calls
    const signal = calls[0][1]!.signal!
    view.state.tenantId = 8; await settlePersonnel()
    expect(signal.aborted).toBe(true)
    pending.resolve(empty); await settlePersonnel()
    expect(calls).toHaveLength(1)
    expect(allNodes(view.root).some(node => node.tag === 'Table')).toBe(false)
    expect(textOf(view.root)).not.toContain('901')
  })
  it.each(['ledger', 'alarms'] as const)('%s 手动切回全部租户不会被旧 URL 强制拉回原租户', async kind => {
    const view = await setup(kind, { query: { tenant_id: '7' } })
    expect(view.state.setTenant).toHaveBeenCalledOnce()
    view.state.tenantId = null; await settlePersonnel()
    expect(view.state.setTenant).toHaveBeenCalledOnce(); expect(view.state.tenantId).toBeNull()
    expect(allNodes(view.root).some(node => node.tag === 'Table')).toBe(false)
    expect(allNodes(view.root).some(node => node.props.title === '链接不属于当前租户，请重新选择')).toBe(true)
  })
  it.each(['ledger', 'alarms'] as const)('%s 注销取消进行中的导出，不提示成功', async kind => {
    const pending = deferred<string>()
    const view = await setup(kind, { query: { tenant_id: '7' }, configure: api => {
      if (kind === 'ledger') api.exportLedger.mockReturnValueOnce(pending.promise)
      else api.exportAlarms.mockReturnValueOnce(pending.promise)
    } })
    const action = clickButton(view.root, '导出 Excel')
    const calls = kind === 'ledger' ? view.api.exportLedger.mock.calls : view.api.exportAlarms.mock.calls
    view.state.user = null; await settlePersonnel()
    expect(calls[0][1]!.signal!.aborted).toBe(true)
    pending.resolve('旧范围.xlsx'); await action
    expect(view.messages.success).not.toHaveBeenCalled()
  })
})
