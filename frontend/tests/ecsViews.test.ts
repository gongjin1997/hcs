import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'
import { deferred } from './businessViewHarness'
import { ecsHistory, ecsInstance, ecsPolicy, ecsQuota } from './ecsFixtures'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import { useEcsPolicy } from '../src/views/ecs/useEcsPolicy'
import { useEcsTrend } from '../src/views/ecs/useEcsTrend'
import { useEcsInstances } from '../src/views/ecs/useEcsInstances'
import * as policyUtils from '../src/views/ecs/ecs-policy'
import * as utils from '../src/views/ecs/ecs-utils'
import * as chartUtils from '../src/views/ecs/ecs-chart'
import { fmtTime } from '../src/utils/format'
import type { EcsCollectionPolicy, EcsHistoryExporter, EcsHistoryFetcher, EcsHistoryOut, EcsHistoryQuery, EcsInstancesFetcher, EcsProjectQuota } from '../src/types/ecs'
const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(fn => fn()))
const permissions = (allowed: string[]) => ({ useUserStore: () => ({ user: { id: 1, tenant_ids: [7, 8] }, can: (key: string) => allowed.includes(key), canAny: (...keys: string[]) => keys.some(key => allowed.includes(key)) }) })
const RouterLink = Vue.defineComponent({ setup(_props, { slots }) { return () => Vue.h('link', slots.default?.()) } })
const vue = { ...Vue, resolveComponent: (name: string) => name === 'router-link' ? RouterLink : Vue.resolveComponent(name) }
const base = { vue, '@/composables/useLatestRequest': { useLatestRequest }, './ecs-utils': utils, './ecs-policy': policyUtils,
  './useEcsPolicy': { useEcsPolicy }, './useEcsTrend': { useEcsTrend }, './useEcsInstances': { useEcsInstances }, './ecs-chart': chartUtils,
  '@/stores/app': { useAppStore: () => ({ tenantId: null, tenants: [{ id: 7, name: '租户甲' }, { id: 8, name: '租户乙' }] }) } }
const mount = (file: string, options: Parameters<typeof mountPersonnel>[1]) => { const view = mountPersonnel(file, options); dispose.push(view.unmount); return view }
const buttons = (root: Parameters<typeof textOf>[0]) => allNodes(root).filter(node => node.tag === 'Button').map(node => textOf(node).trim())

describe('ECS 实际模板权限和交互', () => {
  it('实例行的指标状态可悬浮查看本行后台诊断，不新增列或请求', async () => {
    const instance = ecsInstance()
    instance.diagnostic = { status: 'NO_DATA', message: 'Prometheus 无可唯一匹配的 IP，当前使用率暂无数据', at: null }
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [instance], total: 1, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    const states = allNodes(view.root).filter(node => String(node.props.class).includes('metric-state'))
    expect(states).toHaveLength(3)
    expect(states.map(node => node.props.title)).toEqual(Array(3).fill(instance.diagnostic.message))
    expect(api.listEcsInstances).toHaveBeenCalledOnce()
    expect(allNodes(view.root).filter(node => node.tag === 'TableColumn')).toHaveLength(7)
  })
  it('健康 FRESH 读数和没有诊断的实例不挂诊断提示', async () => {
    const healthy = ecsInstance()
    healthy.diagnostic = { status: 'FRESH', message: '健康实例不需要展示的诊断说明', at: null }
    for (const metric of ['cpu_util', 'mem_util', 'disk_util'] as const) healthy.latest[metric] = {
      value: 0, source: 'PROMETHEUS', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z',
    }
    const empty = ecsInstance(12)
    empty.diagnostic = null
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [healthy, empty], total: 2, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    const states = allNodes(view.root).filter(node => String(node.props.class).includes('metric-state'))
    expect(states).toHaveLength(6)
    expect(states.every(node => node.props.title === undefined)).toBe(true)
    expect(textOf(view.root)).not.toContain(healthy.diagnostic.message)
  })
  it('切换租户立即清空旧诊断，旧请求迟到也不显示跨租户状态', async () => {
    const pending = deferred<Awaited<ReturnType<EcsInstancesFetcher>>>()
    const app = Vue.reactive({ tenantId: 7 as number | null, tenantLabel: '租户甲', tenants: [{ id: 7, name: '租户甲' }, { id: 8, name: '租户乙' }] })
    const first = ecsInstance()
    first.diagnostic = { status: 'NO_DATA', message: '甲租户未配置来源', at: null }
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [first], total: 1, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']), '@/stores/app': { useAppStore: () => app } } })
    await settlePersonnel()
    const titles = () => allNodes(view.root).map(node => node.props.title).filter(Boolean)
    expect(titles()).toContain('甲租户未配置来源')
    api.listEcsInstances.mockImplementationOnce(() => pending.promise)
    const header = findNode(view.root, 'Component', node => node.props.title === 'ECS 监控')
    const refreshing = (header.props.refresh as () => Promise<void>)()
    app.tenantId = 8
    await settlePersonnel()
    expect(titles()).not.toContain('甲租户未配置来源')
    const second = ecsInstance(12, 8)
    second.diagnostic = { status: 'NO_DATA', message: '乙租户无采样', at: null }
    api.listEcsInstances.mockResolvedValue({ items: [second], total: 1, page: 1, page_size: 20 })
    await (header.props.refresh as () => Promise<void>)()
    pending.resolve({ items: [first], total: 1, page: 1, page_size: 20 })
    await refreshing
    await settlePersonnel()
    expect(titles()).toContain('乙租户无采样')
    expect(titles()).not.toContain('甲租户未配置来源')
  })
  it('首屏按实例、租户项目、状态及三项读数排列，分别展示真实零、缺失与过期', async () => {
    const instance = ecsInstance()
    instance.latest.cpu_util = { value: 0, source: 'PROMETHEUS', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    instance.latest.disk_util = { value: 72.5, source: 'PROMETHEUS', state: 'STALE', observed_at: '2026-11-01T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [instance], total: 1, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    expect(allNodes(view.root).filter(node => node.tag === 'TableColumn').map(node => node.props.label)).toEqual(['实例 / IP', '租户 / 项目', '实例状态', 'CPU 使用率', '内存使用率', '磁盘使用率', '操作'])
    expect(textOf(view.root)).toContain('项目甲')
    expect(textOf(view.root)).toContain('vm-11')
    expect(textOf(view.root)).toContain('0.0%新鲜')
    expect(textOf(view.root)).toContain('—暂无样本')
    expect(textOf(view.root)).toContain('72.5%已过期')
    expect(allNodes(view.root).filter(node => node.tag === 'Tooltip').map(node => node.props.content)).toEqual([
      `时间语义未知；返回：${fmtTime(instance.latest.cpu_util.observed_at, 'YYYY-MM-DD HH:mm:ss')}；入库：${fmtTime(instance.latest.cpu_util.collected_at, 'YYYY-MM-DD HH:mm:ss')}`,
      '时间语义未知；返回：-；入库：-',
      `时间语义未知；返回：${fmtTime(instance.latest.disk_util.observed_at, 'YYYY-MM-DD HH:mm:ss')}；入库：${fmtTime(instance.latest.disk_util.collected_at, 'YYYY-MM-DD HH:mm:ss')}`,
    ])
    const header = findNode(view.root, 'Component', node => node.props.title === 'ECS 监控')
    expect(header.props.refreshing).toBe(false)
    await (header.props.refresh as () => Promise<void>)()
    expect(api.listEcsInstances).toHaveBeenCalledTimes(2)
    expect(buttons(view.root)).not.toContain('刷新')
  })
  it('通过表单查询和重置筛选，Enter 查询与分页保留过滤语义', async () => {
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async query => ({ items: [], total: 0, page: query.page, page_size: query.page_size })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    const keyword = findNode(view.root, 'Input', node => node.props['aria-label'] === '搜索 ECS 实例')
    ;(keyword.props['onUpdate:modelValue'] as (value: string) => void)(' vm-11 ')
    const critical = findNode(view.root, 'Select', node => node.props['aria-label'] === '核心资源')
    ;(critical.props['onUpdate:modelValue'] as (value: string) => void)('no')
    await (findNode(view.root, 'Form').props.onSubmit as (event: { preventDefault: () => void }) => Promise<void>)({ preventDefault() {} })
    await settlePersonnel()
    expect(api.listEcsInstances.mock.calls[1][0]).toMatchObject({ page: 1, keyword: 'vm-11', critical: false })
    const pager = findNode(view.root, 'Pagination')
    await (pager.props['onUpdate:currentPage'] as (value: number) => Promise<void>)(2)
    expect(api.listEcsInstances.mock.calls[2][0]).toMatchObject({ page: 2, keyword: 'vm-11', critical: false })
    await clickButton(view.root, '重置')
    expect(api.listEcsInstances.mock.calls[3][0]).toEqual({ page: 1, page_size: 20, keyword: undefined, critical: undefined, monitored: undefined })
    expect(buttons(view.root)).not.toContain('重置')
  })
  it('从全部租户打开实例详情和租户策略，切换租户关闭旧面板', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/ecs', component: {} }] })
    await router.push('/ecs'); await router.isReady()
    const app = Vue.reactive({ tenantId: null as number | null, tenantLabel: '全部租户', tenants: [{ id: 7, name: '租户甲' }, { id: 8, name: '租户乙' }] })
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [ecsInstance()], total: 1, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']), '@/stores/app': { useAppStore: () => app },
      'vue-router': { useRoute: () => ({ get query() { return router.currentRoute.value.query } }), useRouter: () => router } } })
    await settlePersonnel()
    await clickButton(view.root, '监控详情')
    const drawer = findNode(view.root, 'Component', node => node.props['instance-id'] === 11)
    expect(drawer.props).toMatchObject({ modelValue: true, 'tenant-id': 7 })
    expect(drawer.props.instance).toMatchObject({ id: 11, tenant_id: 7 })
    ;(drawer.props.onPolicy as (tenant: number) => void)(7)
    await settlePersonnel()
    const policy = findNode(view.root, 'Component', node => node.props['tenant-name'] === '租户甲')
    expect(policy.props.modelValue).toBe(true)
    expect(drawer.props.modelValue).toBe(false)
    app.tenantId = 8; await settlePersonnel()
    expect(policy.props.modelValue).toBe(false)
  })
  it.each([
    { id: '11', tenant: '7', selected: null, visible: true },
    { id: '11', tenant: '8', selected: 7, visible: false },
    { id: '11', tenant: '99', selected: null, visible: false },
    { id: '-1', tenant: '7', selected: null, visible: false },
  ])('保留 URL 实例入口并校验租户范围 $tenant / $selected', async ({ id, tenant, selected, visible }) => {
    const api = { listEcsInstances: vi.fn<EcsInstancesFetcher>(async () => ({ items: [], total: 0, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']),
      'vue-router': { useRoute: () => ({ query: { instance_id: id, tenant_id: tenant } }), useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) },
      '@/stores/app': { useAppStore: () => ({ tenantId: selected, tenantLabel: '当前范围', tenants: [{ id: 7, name: '租户甲' }, { id: 8, name: '租户乙' }] }) } } })
    await settlePersonnel()
    const drawer = findNode(view.root, 'Component', node => 'instance-id' in node.props)
    expect(drawer.props.modelValue).toBe(visible)
    if (visible) expect(drawer.props).toMatchObject({ 'instance-id': 11, 'tenant-id': 7 })
  })
  it('详情分成三个页签；明细最近窗口优先，空采样不显示伪零或非零值', async () => {
    const api = { getEcsHistory: vi.fn<EcsHistoryFetcher>(async (_id, query) => {
      const history = ecsHistory(query)
      history.series[0].points[1].average = 99
      return history
    }), exportEcsHistory: vi.fn<EcsHistoryExporter>(async () => '指标.xlsx') }
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    expect(allNodes(view.root).filter(node => node.tag === 'TabPane').map(node => node.props.label)).toEqual(['趋势', '最近结果', '采集信息'])
    expect(textOf(view.root)).toContain('0.0%')
    expect(textOf(view.root)).not.toContain('99.0%')
    expect(textOf(view.root)).toContain('已开启 · 5 分钟')
    expect(textOf(view.root)).toContain('按返回时间；PromQL 默认求值时间')
    const table = findNode(view.root, 'Table', node => node.props['max-height'] === '340')
    const rows = table.props.data as { at: string }[]
    expect(rows.every((row, index) => !index || Date.parse(rows[index - 1].at) >= Date.parse(row.at))).toBe(true)
    expect(api.getEcsHistory.mock.calls[0][1].metrics).toEqual(['cpu_util'])
  })
  it('历史失败仍显示已选实例的最新采样与采集设置，旧 AOM 当前快照继续隐藏', async () => {
    const instance = ecsInstance()
    instance.latest.cpu_util = { value: 0, source: 'PROMETHEUS', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    instance.latest.disk_util = { value: 98.7, source: 'AOM', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    const api = { getEcsHistory: vi.fn(async () => { throw new Error('历史暂时不可用') }), exportEcsHistory: vi.fn() }
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7, instance }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'ledger:export']) } })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('服务器 11')
    expect(textOf(view.root)).toContain('0.0%')
    expect(textOf(view.root)).toContain('已开启 · 5 分钟')
    expect(textOf(view.root)).not.toContain('98.7%')
    expect(textOf(view.root)).not.toContain('AOM')
    expect(allNodes(view.root).some(node => node.props.title === '历史暂时不可用')).toBe(true)
    expect(findNode(view.root, 'Button', node => textOf(node) === '导出 Excel').props.disabled).toBe(true)
    expect(api.exportEcsHistory).not.toHaveBeenCalled()
  })
  it.each([{ id: 12, tenant: 7 }, { id: 11, tenant: 8 }])('不使用其他实例或租户的快照 $id / $tenant', async ({ id, tenant }) => {
    const instance = { ...ecsInstance(id, tenant), name: '不应出现的实例快照' }
    const api = { getEcsHistory: vi.fn(async () => { throw new Error('历史不可用') }), exportEcsHistory: vi.fn() }
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7, instance }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    expect(textOf(view.root)).not.toContain('不应出现的实例快照')
    expect(buttons(view.root)).not.toContain('采集策略')
  })
  it('详情指标和时间切换立即取消旧请求，两个分区导出复用当前成功查询', async () => {
    const pending = deferred<EcsHistoryOut>()
    const api = { getEcsHistory: vi.fn<EcsHistoryFetcher>(async (_id, query) => ecsHistory(query)), exportEcsHistory: vi.fn<EcsHistoryExporter>(async () => '指标.xlsx') }
    api.getEcsHistory.mockImplementationOnce(() => pending.promise)
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'ledger:export']) } })
    const metric = findNode(view.root, 'Select', node => node.props['aria-label'] === '趋势指标')
    ;(metric.props['onUpdate:modelValue'] as (value: string) => void)('mem_util')
    await settlePersonnel()
    expect(api.getEcsHistory.mock.calls[0][2].signal.aborted).toBe(true)
    expect(api.getEcsHistory.mock.calls[1][1].metrics).toEqual(['mem_util'])
    pending.reject(new Error('旧实例指标不可用')); await settlePersonnel()
    expect(allNodes(view.root).some(node => node.props.title === '旧实例指标不可用')).toBe(false)
    const range = findNode(view.root, 'RadioGroup', node => node.props['aria-label'] === '时间范围')
    ;(range.props['onUpdate:modelValue'] as (value: string) => void)('7d')
    await settlePersonnel()
    const query = api.getEcsHistory.mock.calls[2][1]
    expect(query.metrics).toEqual(['mem_util'])
    expect(query.interval_sec).toBe(900)
    expect(Date.parse(query.end_at) - Date.parse(query.start_at)).toBe(7 * 86400000)
    await clickButton(view.root, '导出 Excel')
    expect(api.exportEcsHistory.mock.calls[0]).toEqual([11, query, expect.objectContaining({ tenantId: 7 })])
    const samples = findNode(view.root, 'TabPane', node => node.props.name === 'samples')
    await clickButton(samples, '导出 Excel')
    expect(api.exportEcsHistory.mock.calls[1][1]).toEqual(query)
  })
  it('采集信息的策略入口携带实例租户，只有具备策略查看权限才出现', async () => {
    const onPolicy = vi.fn()
    const api = { getEcsHistory: vi.fn<EcsHistoryFetcher>(async (_id, query) => ecsHistory(query)), exportEcsHistory: vi.fn() }
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7, onPolicy }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    await clickButton(view.root, '采集策略')
    expect(onPolicy).toHaveBeenCalledWith(7)
    const readonly = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions([]) } })
    await settlePersonnel()
    expect(buttons(readonly.root)).not.toContain('采集策略')
    expect(buttons(readonly.root)).not.toContain('导出 Excel')
  })
  it('项目配额默认展开首项目，当前页搜索切换展开项且不请求云端刷新', async () => {
    const second = { ...ecsQuota(), project_ref: 108, project_name: '项目乙', project_id: 'project-other' }
    const api = { listEcsQuotas: vi.fn(async () => ({ items: [ecsQuota(), second], total: 2, page: 1, page_size: 20 })), refreshEcsQuota: vi.fn() }
    const view = mount('ecs/EcsQuotaPanel.vue', { props: { tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    const projects = findNode(view.root, 'Collapse')
    expect(projects.props.modelValue).toBe('7:107')
    const search = findNode(view.root, 'Input', node => node.props['aria-label'] === '筛选项目配额')
    ;(search.props['onUpdate:modelValue'] as (value: string) => void)('项目乙')
    await settlePersonnel()
    expect(projects.props.modelValue).toBe('7:108')
    expect(allNodes(view.root).filter(node => node.tag === 'CollapseItem').map(node => node.props.title)).toEqual([expect.stringContaining('项目乙')])
    expect(api.listEcsQuotas).toHaveBeenCalledOnce()
    expect(api.refreshEcsQuota).not.toHaveBeenCalled()
    ;(search.props['onUpdate:modelValue'] as (value: string) => void)('没有此项目')
    await settlePersonnel()
    expect(findNode(view.root, 'Empty').props.description).toBe('当前页没有匹配项目')
  })
  it('策略操作保留在滚动区外的页脚，保存期间关闭和重新读取禁用', async () => {
    const pending = deferred<ReturnType<typeof ecsPolicy>>()
    const api = { getEcsPolicy: vi.fn(async () => ecsPolicy()), saveEcsPolicy: vi.fn(() => pending.promise), listEcsInstances: vi.fn() }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'collect:manage']) } })
    await settlePersonnel()
    const footer = findNode(view.root, 'div', node => node.props.class === 'table-actions policy-footer')
    expect(buttons(footer)).toEqual(['关闭', '重新读取', '保存策略'])
    const saving = (findNode(footer, 'Button', node => textOf(node) === '保存策略').props.onClick as () => Promise<void>)()
    await settlePersonnel()
    expect(findNode(footer, 'Button', node => textOf(node) === '关闭').props.disabled).toBe(true)
    expect(findNode(footer, 'Button', node => textOf(node) === '重新读取').props.disabled).toBe(true)
    pending.resolve(ecsPolicy()); await saving; await settlePersonnel()
    expect(view.messages.success).toHaveBeenCalledWith('采集策略已保存')
  })
  it('旧策略的 AOM 映射不进入编辑器，修改 Prometheus 后只提交 aom:null', async () => {
    const legacy = ecsPolicy()
    Object.assign(legacy.policy, { aom: { namespace: 'LEGACY_SECRET', dimension: 'vm_id', metrics: { cpu_util: 'old_cpu' } } })
    const api = { getEcsPolicy: vi.fn(async () => legacy), saveEcsPolicy: vi.fn(async (_tenant: number, body: EcsCollectionPolicy) => ({ ...ecsPolicy(), policy: body })), listEcsInstances: vi.fn() }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'collect:manage']) } })
    await settlePersonnel()
    expect(allNodes(view.root).some(node => node.props['aria-label'] === 'AOM 映射 JSON')).toBe(false)
    expect(textOf(view.root)).toContain('AOM 已停用，旧映射不再使用')
    expect(textOf(view.root)).not.toContain('LEGACY_SECRET')
    expect(allNodes(view.root).some(node => node.props.title === '未填写自定义查询时使用内置 Prometheus 查询；未配置数据源时显示“无数据”。')).toBe(true)
    const prom = { queries: { cpu_util: 'actual_cpu_ratio * 100' }, instance_label: 'vm_id', units: { cpu_util: '%' } }
    const editor = findNode(view.root, 'Input', node => node.props['aria-label'] === 'Prometheus 映射 JSON')
    ;(editor.props['onUpdate:modelValue'] as (value: string) => void)(JSON.stringify(prom))
    await clickButton(view.root, '保存策略')
    expect(api.saveEcsPolicy).toHaveBeenCalledOnce()
    expect(api.saveEcsPolicy.mock.calls[0][1]).toMatchObject({ aom: null, prometheus: prom })
    expect(legacy.policy.aom).toEqual(expect.objectContaining({ namespace: 'LEGACY_SECRET' }))
  })
  it.each(['AOM', 'PROMETHEUS'])('当前接口来源 %s 带旧 AOM 快照时不显示其数值、来源或正常状态', async source => {
    const instance = ecsInstance()
    instance.metrics_source = source
    instance.latest.cpu_util = { value: 98.7, source: 'AOM', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    const api = { listEcsInstances: vi.fn(async () => ({ items: [instance], total: 1, page: 1, page_size: 20 })) }
    const view = mount('ecs/EcsView.vue', { dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']), '@/stores/app': { useAppStore: () => ({ tenantId: 7, tenantLabel: '租户甲', tenants: [{ id: 7, name: '租户甲' }] }) } } })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('未配置数据源')
    expect(textOf(view.root)).toContain('无来源')
    expect(textOf(view.root)).not.toContain('98.7%')
    expect(textOf(view.root)).not.toContain('AOM')
    expect(instance.latest.cpu_util.value).toBe(98.7)
  })
  it('历史曲线和明细标注 AOM 已停用，保留原始来源、历史警告和导出条件', async () => {
    const warning = 'AOM 已停用，仅保留历史样本'
    let response: ReturnType<typeof ecsHistory>
    const api = { getEcsHistory: vi.fn(async (_id: number, query: EcsHistoryQuery) => {
      response = ecsHistory(query); response.warnings = [warning]; return response
    }), exportEcsHistory: vi.fn(async () => '历史指标.xlsx') }
    const view = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'ledger:export']) } })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('AOM（已停用，历史）')
    expect(allNodes(view.root).some(node => node.props.title === warning)).toBe(true)
    expect(response!.series[0].source).toBe('AOM')
    await clickButton(view.root, '导出 Excel')
    expect(api.exportEcsHistory.mock.calls[0]).toEqual([11, api.getEcsHistory.mock.calls[0][1], expect.objectContaining({ tenantId: 7 })])
  })
  it('只读策略显示总开关关闭与禁用表单，不显示保存按钮', async () => {
    const api = { getEcsPolicy: vi.fn(async () => ecsPolicy(7, false)), saveEcsPolicy: vi.fn(), listEcsInstances: vi.fn() }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7, tenantName: '租户甲' }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    expect(findNode(view.root, 'Form').props.disabled).toBe(true)
    expect(buttons(view.root)).not.toContain('保存策略')
    expect(allNodes(view.root).some(node => node.props.title === 'ECS 采集总开关已关闭，各阶段当前不执行')).toBe(true)
    expect(api.saveEcsPolicy).not.toHaveBeenCalled()
  })
  it('模板显示服务端全局任务和阶段关闭原因，本地开关不冒充生效状态', async () => {
    const response = ecsPolicy()
    response.phase_status.metrics = { configured_enabled: false, master_enabled: true, task_code: 'ecs_metrics', task_enabled: false, supported: true, effective_enabled: false }
    response.policy.inspection.enabled = false
    response.phase_status.inspection.configured_enabled = false
    response.phase_status.inspection.effective_enabled = false
    const api = { getEcsPolicy: vi.fn(async () => response), saveEcsPolicy: vi.fn(), listEcsInstances: vi.fn() }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'collect:manage', 'task:view']) } })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('全局任务关闭（ecs_metrics）；本阶段关闭')
    expect(textOf(view.root)).toContain('执行状态按已保存策略显示')
    expect(allNodes(view.root).some(node => String(node.props.title).includes('保存后将关闭该租户'))).toBe(false)
    expect(api.saveEcsPolicy).not.toHaveBeenCalled()
  })
  it('显示当前沿用的全局 cron，编辑周期不冒充已切换，保存后使用返回的租户周期状态', async () => {
    const response = ecsPolicy()
    response.phase_status.metrics = { ...response.phase_status.metrics, cron: '15 2 * * *', schedule_source: 'task', interval_sec: 300 }
    const api = { getEcsPolicy: vi.fn(async () => response), listEcsInstances: vi.fn(), saveEcsPolicy: vi.fn(async (_tenant: number, body: EcsCollectionPolicy) => {
      const saved = ecsPolicy()
      saved.policy = JSON.parse(JSON.stringify(body))
      saved.phase_status.metrics = { ...saved.phase_status.metrics, cron: null, schedule_source: 'policy', interval_sec: body.metrics.interval_sec }
      return saved
    }) }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'collect:manage']) } })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('当前沿用全局定时：15 2 * * *')
    expect(textOf(view.root)).toContain('保存后按本租户周期执行')
    const interval = findNode(view.root, 'InputNumber', node => node.props['aria-label'] === '指标采集间隔秒')
    ;(interval.props['onUpdate:modelValue'] as (value: number) => void)(600)
    await settlePersonnel()
    expect(textOf(view.root)).toContain('当前沿用全局定时：15 2 * * *')
    expect(api.saveEcsPolicy).not.toHaveBeenCalled()
    await clickButton(view.root, '保存策略')
    expect(api.saveEcsPolicy.mock.calls[0][1].metrics.interval_sec).toBe(600)
    expect(api.saveEcsPolicy.mock.calls[0][1]).not.toHaveProperty('phase_status')
    expect(textOf(view.root)).not.toContain('当前沿用全局定时')
    expect(api.getEcsPolicy).toHaveBeenCalledTimes(1)
    expect(api.listEcsInstances).not.toHaveBeenCalled()
  })
  it('从真实保存按钮校验间隔，再保存独立阶段开关', async () => {
    const api = { getEcsPolicy: vi.fn(async () => ecsPolicy()), saveEcsPolicy: vi.fn(async () => ecsPolicy()), listEcsInstances: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 100 })) }
    const view = mount('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', 'collect:manage']) } })
    await settlePersonnel()
    const interval = findNode(view.root, 'InputNumber', node => node.props['aria-label'] === '指标采集间隔秒')
    ;(interval.props['onUpdate:modelValue'] as (value: number) => void)(30)
    await clickButton(view.root, '保存策略')
    expect(api.saveEcsPolicy).not.toHaveBeenCalled()
    expect(allNodes(view.root).some(node => String(node.props.title).includes('指标采集间隔须'))).toBe(true)
    ;(interval.props['onUpdate:modelValue'] as (value: number) => void)(600)
    const metricsSwitch = findNode(view.root, 'Switch', node => node.props['aria-label'] === '指标采集开关')
    ;(metricsSwitch.props['onUpdate:modelValue'] as (value: boolean) => void)(false)
    await clickButton(view.root, '保存策略')
    expect(api.saveEcsPolicy.mock.calls[0]).toEqual([7, expect.objectContaining({ metrics: { enabled: false, interval_sec: 600 }, inventory: { enabled: true, interval_sec: 86400 } }), expect.objectContaining({ tenantId: 7 })])
    expect(view.messages.success).toHaveBeenCalledWith('采集策略已保存')
  })
  it('趋势只读隐藏导出；有导出权限才用已查条件下载', async () => {
    const api = { getEcsHistory: vi.fn(async (_id: number, query: EcsHistoryQuery) => ecsHistory(query)), exportEcsHistory: vi.fn(async () => 'trend.xlsx') }
    const deps = { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) }
    const readonly = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: deps })
    await settlePersonnel()
    expect(buttons(readonly.root)).not.toContain('导出 Excel')
    const editable = mount('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 }, dependencies: { ...deps, '@/stores/user': permissions(['ledger:view', 'ledger:export']) } })
    await settlePersonnel()
    await clickButton(editable.root, '导出 Excel')
    expect(api.exportEcsHistory.mock.calls[0]).toEqual([11, api.getEcsHistory.mock.calls[1][1], expect.objectContaining({ tenantId: 7 })])
  })
  it('配额 GET 不自动刷新云端，只读隐藏刷新，未知与不限额不显示为零', async () => {
    const api = { listEcsQuotas: vi.fn(async () => ({ items: [ecsQuota()], total: 1, page: 1, page_size: 20 })), refreshEcsQuota: vi.fn() }
    const view = mount('ecs/EcsQuotaPanel.vue', { props: { tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view']) } })
    await settlePersonnel()
    expect(buttons(view.root)).not.toContain('刷新项目配额')
    expect(api.refreshEcsQuota).not.toHaveBeenCalled()
    expect(textOf(view.root)).toContain('不限额')
    expect(textOf(view.root)).toContain('接口未提供')
    expect(textOf(view.root)).toContain('0.0%')
    expect(textOf(view.root)).toContain('获取时间')
    expect(textOf(view.root)).toContain('源端未提供采样时间')
  })
  it.each(['ledger:sync', 'task:run'])('配额权限 %s 可刷新单项目；换租户拒绝迟到刷新', async permission => {
    const pending = deferred<EcsProjectQuota>()
    const api = { listEcsQuotas: vi.fn(async (_query, context) => ({ items: [ecsQuota(context.tenantId)], total: 1, page: 1, page_size: 20 })), refreshEcsQuota: vi.fn(() => pending.promise) }
    const view = mount('ecs/EcsQuotaPanel.vue', { props: { tenantId: 7 }, dependencies: { ...base, '@/api/ecs': api, '@/stores/user': permissions(['ledger:view', permission]) } })
    await settlePersonnel()
    const button = findNode(view.root, 'Button', node => textOf(node) === '刷新项目配额')
    const clicking = (button.props.onClick as () => Promise<void>)()
    expect(api.refreshEcsQuota.mock.calls[0]).toEqual([107, expect.objectContaining({ tenantId: 7 })])
    view.updateProps({ tenantId: 8 }); await settlePersonnel()
    pending.resolve({ ...ecsQuota(7), tenant_name: '不应出现的旧租户' }); await clicking; await settlePersonnel()
    expect(textOf(view.root)).not.toContain('不应出现的旧租户')
    expect(view.messages.success).not.toHaveBeenCalled()
  })
})
