import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter, type LocationQuery, type LocationQueryRaw } from 'vue-router'
import { monitoringListQuery, readMonitoringContext, resourceAlarmsLocation, resourceLedgerLocation, resourceMonitoringLocation } from '../src/config/resource-navigation'
import type { DcsHistoryOut, DcsHistoryQuery, DcsInstance } from '../src/types/dcs'
import * as dcsUtils from '../src/views/dcs/dcs-utils'
import { memoryChart } from '../src/views/dcs/memory-chart'
import { useDcsMemory } from '../src/views/dcs/useDcsMemory'
import { deferred } from './businessViewHarness'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).reverse().forEach(stop => stop()))
const instance = (id: number, tenant = 7): DcsInstance => ({ id, resource_id: `cloud-${id}`, name: `缓存 ${id}`,
  tenant_id: tenant, tenant_name: `租户 ${tenant}`, project_ref: 107, project_name: '项目甲', region: 'region-test',
  status_raw: 'RUNNING', memory_usage: 0, used_memory_mb: 0, max_memory_mb: 1024, snapshot_at: null })
const history = (query: DcsHistoryQuery, tenant = 7): DcsHistoryOut => ({ ...query, timezone: 'UTC', demo: false,
  generated_at: query.end_at, series: query.resource_ids.map(id => ({ instance: instance(id, tenant), source: 'LOCAL', status: 'AVAILABLE',
    warning: null, valid_points: 1, total_points: 1, summary: { average_usage: 0, peak_usage: 0, latest_usage: 0, latest_at: query.start_at },
    points: [{ at: query.start_at, memory_usage: 0, memory_max: 0, used_memory_mb: 0, max_memory_mb: 1024, sample_count: 1 }],
  })) })

async function setup(options: { query?: LocationQueryRaw; tenant?: number | null; permissions?: string[];
  configure?: (api: ReturnType<typeof apiFactory>) => void } = {}) {
  const state = reactive({ tenantId: options.tenant === undefined ? 7 : options.tenant, tenantLabel: '当前范围',
    user: { id: 1, tenant_ids: [7, 8] as number[] | null } as { id: number; tenant_ids: number[] | null } | null,
    permissions: options.permissions ?? ['report:view', 'ledger:view', 'alarm:view'] })
  const api = apiFactory(); options.configure?.(api)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dcs', component: {} }] })
  await router.push({ path: '/dcs', query: options.query ?? {} }); await router.isReady()
  const view = mountPersonnel('dcs/DcsMemoryView.vue', { dependencies: {
    '@/api/dcs': api, './dcs-utils': dcsUtils, './memory-chart': { memoryChart }, './useDcsMemory': { useDcsMemory },
    '@/stores/app': { useAppStore: () => state },
    '@/stores/user': { useUserStore: () => ({ get user() { return state.user }, can: (permission: string) => state.permissions.includes(permission) }) },
    'vue-router': { useRoute: () => ({ get query() { return router.currentRoute.value.query } }), useRouter: () => router },
  } })
  dispose.push(view.unmount)
  await settlePersonnel()
  const selector = () => findNode(view.root, 'Select', node => node.props['aria-label'] === '选择 DCS 实例')
  return { ...view, state, api, router, selector }
}
function apiFactory() {
  return {
    listDcsInstances: vi.fn(async () => ({ items: [instance(1)], total: 1000, page: 1, page_size: 50 })),
    getDcsInstance: vi.fn(async (id: number, _context: { signal: AbortSignal; tenantId: number | null }) => instance(id)),
    getDcsMetricsHistory: vi.fn(async (query: DcsHistoryQuery, _context: { signal: AbortSignal; tenantId: number | null }) => history(query)),
    exportDcsMetricsHistory: vi.fn(async (_query: DcsHistoryQuery, _context: { signal: AbortSignal; tenantId: number | null }) => 'DCS原生指标.xlsx'),
  }
}

describe('监控 URL 的统一边界', () => {
  const viewer = { user: { id: 1, tenant_ids: [7] }, can: (permission: string) => ['ledger:view', 'report:view', 'alarm:view'].includes(permission) }
  const invalidQueries: LocationQuery[] = [
    {}, { instance_id: '99' }, { tenant_id: '7' }, { instance_id: ['99', '100'], tenant_id: '7' },
    { instance_id: '99', tenant_id: ['7', '8'] }, { instance_id: '1e2', tenant_id: '7' },
    { instance_id: ' 99', tenant_id: '7' }, { instance_id: '099', tenant_id: '7' },
    { instance_id: '9007199254740992', tenant_id: '7' }, { instance_id: '99', tenant_id: '0' },
  ]
  it.each(invalidQueries)('不猜测、合并或放宽不完整参数 %j', query => {
    expect(readMonitoringContext(query, null, viewer, 'report:view').context).toBeNull()
  })
  it('全部范围仍要求明确授权租户；租户不能从全局筛选补入', () => {
    expect(readMonitoringContext({ instance_id: '99', tenant_id: '7' }, null, viewer, 'report:view').context).toEqual({ instanceId: 99, tenantId: 7 })
    expect(readMonitoringContext({ instance_id: '99', tenant_id: '8' }, null, viewer, 'report:view').context).toBeNull()
    expect(readMonitoringContext({ instance_id: '99', tenant_id: '7' }, 8, viewer, 'report:view').context).toBeNull()
    expect(readMonitoringContext({ instance_id: '99' }, 7, viewer, 'report:view').context).toBeNull()
  })
  it('返回台账和告警使用本地资源关联及自己的权限，清理实例参数保留筛选', () => {
    const target = { id: 99, tenant_id: 7, resource_type: 'dcs_instance' }
    expect(resourceLedgerLocation(target, viewer)).toEqual({ path: '/ledger', query: { id: '99', type: 'dcs_instance', tenant_id: '7' } })
    expect(resourceAlarmsLocation(target, viewer)).toEqual({ path: '/alarms', query: { resource_ref: '99', tenant_id: '7' } })
    const reportOnly = { ...viewer, can: (permission: string) => permission === 'report:view' }
    expect(resourceMonitoringLocation(target, reportOnly)).toEqual({ path: '/dcs', query: { instance_id: '99', tenant_id: '7' } })
    expect(resourceLedgerLocation(target, reportOnly)).toBeNull(); expect(resourceAlarmsLocation(target, reportOnly)).toBeNull()
    expect(resourceMonitoringLocation({ ...target, resource_type: 'ecs_server' }, reportOnly)).toBeNull()
    expect(monitoringListQuery({ instance_id: '99', tenant_id: '7', tab: 'disk', keyword: 'cache', project_ref: '107' })).toEqual({ keyword: 'cache', project_ref: '107' })
  })
})

describe('DCS 实际页面的深链与上下文', () => {
  it('刷新按本地主键直接打开第一页之外实例，查询和导出都采用该实例', async () => {
    const query = { instance_id: '901', tenant_id: '7', keyword: '保留' }
    const view = await setup({ query })
    expect(view.api.getDcsInstance).toHaveBeenCalledExactlyOnceWith(901, expect.objectContaining({ tenantId: 7 }))
    expect(view.api.listDcsInstances).toHaveBeenCalledOnce()
    expect(view.selector().props['model-value']).toEqual([901])
    expect(view.api.getDcsMetricsHistory).toHaveBeenCalledOnce()
    expect(view.api.getDcsMetricsHistory.mock.calls[0][0].resource_ids).toEqual([901])
    await clickButton(view.root, '导出 Excel')
    expect(view.api.exportDcsMetricsHistory).toHaveBeenCalledExactlyOnceWith(view.api.getDcsMetricsHistory.mock.calls[0][0], expect.objectContaining({ tenantId: 7 }))
    expect(findNode(view.root, 'router-link', node => textOf(node) === '查看台账').props.to).toEqual({ path: '/ledger', query: { id: '901', type: 'dcs_instance', tenant_id: '7' } })
    expect(findNode(view.root, 'router-link', node => textOf(node) === '相关告警').props.to).toEqual({ path: '/alarms', query: { resource_ref: '901', tenant_id: '7' } })
    const reloaded = await setup({ query })
    expect(reloaded.selector().props['model-value']).toEqual([901]); expect(reloaded.router.currentRoute.value.query).toEqual(query)
  })
  it('全部租户模式仍将单实例详情请求固定到链接租户', async () => {
    const view = await setup({ tenant: null, query: { instance_id: '901', tenant_id: '7' } })
    expect(view.api.getDcsInstance.mock.calls[0][1].tenantId).toBe(7)
    expect(view.selector().props['model-value']).toEqual([901])
  })
  it.each([
    { instance_id: '901' }, { instance_id: ['901', '902'], tenant_id: '7' },
    { instance_id: '901', tenant_id: '8' }, { instance_id: '901', tenant_id: '99' },
  ])('错误链接 %j 不降级到第一页实例或发历史查询', async query => {
    const view = await setup({ query })
    expect(view.api.getDcsInstance).not.toHaveBeenCalled(); expect(view.api.getDcsMetricsHistory).not.toHaveBeenCalled()
    expect(view.selector().props['model-value']).toEqual([])
    expect(allNodes(view.root).some(node => node.tag === 'Alert' && node.props.type === 'error')).toBe(true)
  })
  it('只有 ledger:view 不会获得 DCS report:view；纯报表用户不显示台账/告警入口', async () => {
    const denied = await setup({ query: { instance_id: '901', tenant_id: '7' }, permissions: ['ledger:view'] })
    expect(denied.api.listDcsInstances).not.toHaveBeenCalled(); expect(denied.api.getDcsInstance).not.toHaveBeenCalled()
    const allowed = await setup({ query: { instance_id: '901', tenant_id: '7' }, permissions: ['report:view'] })
    expect(allowed.selector().props['model-value']).toEqual([901])
    expect(textOf(allowed.root)).not.toContain('查看台账'); expect(textOf(allowed.root)).not.toContain('相关告警')
  })
  it.each(['tenant', 'permission', 'logout', 'route'] as const)('%s 改变取消定位请求，旧实例永不回填', async action => {
    const pending = deferred<DcsInstance>()
    const view = await setup({ query: { instance_id: '901', tenant_id: '7', keyword: '保留' }, configure: api => {
      api.getDcsInstance.mockReturnValueOnce(pending.promise)
    } })
    const signal = view.api.getDcsInstance.mock.calls[0][1].signal
    if (action === 'tenant') view.state.tenantId = 8
    else if (action === 'permission') view.state.permissions = ['ledger:view']
    else if (action === 'logout') view.state.user = null
    else await view.router.replace({ query: { instance_id: '902', tenant_id: '7' } })
    await settlePersonnel(); expect(signal.aborted).toBe(true)
    pending.resolve(instance(901)); await settlePersonnel()
    expect(view.selector().props['model-value']).toEqual(action === 'route' ? [902] : [])
    expect(view.api.getDcsMetricsHistory.mock.calls.every(([query]) => !query.resource_ids.includes(901))).toBe(true)
    if (action !== 'route') expect(view.router.currentRoute.value.query).toEqual({ keyword: '保留' })
  })
  it('单实例响应租户不符或404时清空选择，不使用相同名称兜底', async () => {
    const view = await setup({ query: { instance_id: '901', tenant_id: '7' }, configure: api => {
      api.getDcsInstance.mockResolvedValueOnce(instance(901, 8))
    } })
    expect(view.selector().props['model-value']).toEqual([]); expect(view.api.getDcsMetricsHistory).not.toHaveBeenCalled()
    view.api.getDcsInstance.mockRejectedValueOnce(new Error('实例不可访问或已移除'))
    await view.router.replace({ query: { instance_id: '902', tenant_id: '7' } }); await settlePersonnel()
    expect(view.selector().props['model-value']).toEqual([])
    expect(allNodes(view.root).some(node => node.props.title === '实例不可访问或已移除')).toBe(true)
  })
  it('手选单实例同步链接，多选清除单实例参数且保留多实例查询', async () => {
    const view = await setup({ query: { instance_id: '901', tenant_id: '7', keyword: '保留' } })
    await (view.selector().props['onUpdate:modelValue'] as (ids: number[]) => Promise<void>)([1]); await settlePersonnel()
    expect(view.router.currentRoute.value.query).toEqual({ instance_id: '1', tenant_id: '7', keyword: '保留' })
    await (view.selector().props['onUpdate:modelValue'] as (ids: number[]) => Promise<void>)([1, 901]); await settlePersonnel()
    expect(view.router.currentRoute.value.query).toEqual({ keyword: '保留' }); expect(view.selector().props['model-value']).toEqual([1, 901])
    expect(view.api.getDcsMetricsHistory.mock.calls.at(-1)![0].resource_ids).toEqual([1, 901])
  })
})
