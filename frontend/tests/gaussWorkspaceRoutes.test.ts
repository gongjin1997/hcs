import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import { createMemoryHistory, createRouter, type LocationQueryRaw } from 'vue-router'
import type { GaussPanelModel } from '../src/views/gaussdb/useGaussPanel'
import type { GaussInstance, GaussNativeRows, GaussProjectOption } from '../src/types/gaussdb'
import { deferred } from './businessViewHarness'
import { allNodes, findNode, settlePersonnel, textOf } from './personnelHarness'
import { createGaussApi, createGaussDirectoryApi, gaussInstance, gaussRows, mountGauss } from './gaussFixtures'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).reverse().forEach(fn => fn()))
async function setup(options: {
  query?: LocationQueryRaw
  permissions?: string[]
  configure?: (api: ReturnType<typeof createGaussApi>, directory: ReturnType<typeof createGaussDirectoryApi>) => void
} = {}) {
  const app = reactive({ tenantId: 7 as number | null, tenantLabel: '租户甲', multiTenant: false })
  const allowed = reactive({ user: { id: 1, tenant_ids: [7, 8] as number[] | null } as { id: number; tenant_ids: number[] | null } | null,
    permissions: options.permissions ?? ['ledger:view', 'task:run', 'ledger:export'] })
  const api = createGaussApi(), directory = createGaussDirectoryApi()
  options.configure?.(api, directory)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/gaussdb', component: {} }] })
  await router.push({ path: '/gaussdb', query: options.query ?? {} }); await router.isReady()
  const push = vi.spyOn(router, 'push'), replace = vi.spyOn(router, 'replace')
  const workspaceRefresh = vi.fn(async () => undefined)
  const Workspace = defineComponent({ inheritAttrs: false, setup(_props, { attrs, expose }) {
    expose({ refresh: workspaceRefresh })
    return () => h('Component', attrs)
  } })
  // 和 useRoute 一样，getter 始终读取当前的响应式 route；导航采用真正的内存 router。
  const route = { get query() { return router.currentRoute.value.query } }
  const view = mountGauss('GaussView.vue', { dependencies: {
    '@/api/gaussdb': { gaussApi: api, gaussDirectoryApi: directory },
    '@/stores/app': { useAppStore: () => app },
    '@/stores/user': { useUserStore: () => ({ get user() { return allowed.user }, can: (...keys: string[]) => keys.every(key => allowed.permissions.includes(key)) }) },
    'vue-router': { useRoute: () => route, useRouter: () => router },
    './GaussWorkspace.vue': { default: Workspace },
  } })
  dispose.push(view.unmount)
  const child = () => findNode(view.root, 'Component', node => !!node.props.model)
  const model = () => child().props.model as GaussPanelModel
  const open = async (id = 11) => {
    await settlePersonnel()
    await (child().props.onOpen as (row: GaussInstance) => unknown)(model().items.value.find(row => row.id === id)!)
    await settlePersonnel()
  }
  const tab = async (value: string) => {
    await (child().props.onPanel as (value: string) => unknown)(value)
    await settlePersonnel()
  }
  const refresh = () => (findNode(view.root, 'Component', node => typeof node.props.refresh === 'function').props.refresh as () => Promise<void>)()
  return { ...view, api, directory, app, allowed, router, push, replace, child, model, open, tab, refresh, workspaceRefresh }
}
const onlineKeys = ['overview', 'slowNodes', 'slowSql', 'slowDetail', 'sessions', 'inspections', 'inspectionDetail', 'export'] as const

describe('GaussDB root 路由与本地目录', () => {
  it.each([
    { instance_id: '99' }, { instance_id: '99', tenant_id: ['7', '8'] }, { instance_id: '99', tenant_id: '07' },
  ])('不完整或重复租户参数 %j 不请求实例', async query => {
    const view = await setup({ query }); await settlePersonnel()
    expect(view.directory.instance).not.toHaveBeenCalled(); expect(view.api.overview).not.toHaveBeenCalled()
    expect(view.model().selected.value).toBeNull(); expect(view.model().listError.value).toContain('实例链接无效')
  })
  it.each(['logout', 'scope', 'identity'] as const)('%s 变化清理 URL 并取消定位和项目请求', async action => {
    const pending = deferred<GaussInstance>(), projects = deferred<GaussProjectOption[]>()
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', keyword: '保留' }, configure: (_api, directory) => {
      directory.instance.mockReturnValueOnce(pending.promise); directory.projects.mockReturnValueOnce(projects.promise)
    } })
    await settlePersonnel()
    const signal = view.directory.instance.mock.calls[0][1].signal, projectSignal = view.directory.projects.mock.calls[0][0].signal
    if (action === 'logout') view.allowed.user = null
    else if (action === 'scope') view.allowed.user!.tenant_ids = [8]
    else view.allowed.user = { id: 2, tenant_ids: [8] }
    await settlePersonnel()
    expect(signal.aborted).toBe(true); expect(projectSignal.aborted).toBe(true)
    pending.resolve(gaussInstance(99)); projects.resolve([{ project_ref: 999, project_name: '旧项目', tenant_id: 7, tenant_name: '旧租户', region: null }])
    await settlePersonnel()
    expect(view.model().selected.value).toBeNull(); expect(view.router.currentRoute.value.query).toEqual({ keyword: '保留' })
    expect(view.api.overview).not.toHaveBeenCalled()
  })
  it('列表仅查询本地实例与完整项目目录，不进行在线诊断', async () => {
    const view = await setup(); await settlePersonnel()
    expect(view.api.instances).toHaveBeenCalledOnce(); expect(view.directory.projects).toHaveBeenCalledOnce()
    expect(view.directory.instance).not.toHaveBeenCalled()
    expect(view.model().selected.value).toBeNull()
    expect(view.child().props.projects).toEqual([{ project_ref: 107, project_name: '项目甲', tenant_id: 7, tenant_name: '租户甲', region: 'region-test' }])
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it('刷新深链接按 ID 读取不在第一页的实例，保留列表页，概览只读本地记录不自动在线查询', async () => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', tab: 'overview' } }); await settlePersonnel()
    expect(view.directory.instance).toHaveBeenCalledExactlyOnceWith(99, expect.objectContaining({ tenantId: 7 }))
    expect(view.model().selected.value?.id).toBe(99)
    expect(view.model().items.value.map(row => row.id)).toEqual([11, 12]); expect(view.api.instances).toHaveBeenCalledOnce()
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
    expect(view.model().overview.value).toBeNull()
    expect(view.router.currentRoute.value.query).toEqual({ instance_id: '99', tenant_id: '7', tab: 'overview' })
  })
  it('列表查询无 items 不妨碍深链接目录读取，目录可访问即可打开', async () => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7' }, configure: api => {
      api.instances.mockResolvedValueOnce({ items: [], total: 0, page: 1, page_size: 20 })
    } }); await settlePersonnel()
    expect(view.model().items.value).toEqual([]); expect(view.model().selected.value?.id).toBe(99)
    expect(view.directory.instance).toHaveBeenCalledOnce(); expect(view.api.overview).not.toHaveBeenCalled()
  })
  it.each(['sessions', 'slow-sql', 'performance', 'disk', 'bogus'] as const)('刷新 tab=%s 只恢复标签，不自动调用任何在线接口', async tab => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', tab } }); await settlePersonnel()
    expect(view.model().panel.value).toBe(['disk', 'bogus'].includes(tab) ? 'overview' : tab)   // 旧的磁盘标签已并入概览
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it('点击实例同步 instance/tenant/tab，标签使用 replace 且保留其他 query', async () => {
    const view = await setup({ query: { keyword: '保留' } }); await view.open(12)
    expect(view.push).toHaveBeenCalledWith({ query: { keyword: '保留', instance_id: '12', tenant_id: '7', tab: 'overview' } })
    await view.tab('slow-sql')
    expect(view.replace).toHaveBeenCalledWith({ query: { keyword: '保留', instance_id: '12', tenant_id: '7', tab: 'slow-sql' } })
    expect(view.model().panel.value).toBe('slow-sql')
    await view.tab('overview'); await view.tab('slow-sql')
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
    expect(view.directory.instance).toHaveBeenCalledOnce()
  })
  it('viewer 可以恢复深链接和切全部标签，但不能发在线 query 或导出', async () => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', tab: 'overview' }, permissions: ['ledger:view'] }); await settlePersonnel()
    expect(view.model().selected.value?.id).toBe(99); expect(view.child().props['can-run']).toBe(false)
    for (const tab of ['slow-sql', 'sessions', 'performance', 'overview']) await view.tab(tab)
    expect(await view.model().queryPanel()).toBe(false); expect(await view.model().exportExcel()).toBeNull()
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it.each(['overview', 'sessions', 'slow-sql', 'performance'])('viewer 在 %s 顶栏刷新仅读本地详情，保留标签与表单', async tab => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', tab }, permissions: ['ledger:view'] }); await settlePersonnel()
    view.model().sessionFilters.user_name = '保留用户'; view.model().slowFilters.threshold = 5
    view.directory.instance.mockResolvedValueOnce({ ...gaussInstance(99), name: '已刷新本地名称' })
    await view.refresh(); await settlePersonnel()
    expect(view.directory.instance).toHaveBeenCalledTimes(2)
    expect(view.model().selected.value?.name).toBe('已刷新本地名称'); expect(view.model().panel.value).toBe(tab)
    expect(view.model().sessionFilters.user_name).toBe('保留用户'); expect(view.model().slowFilters.threshold).toBe(5)
    expect(view.workspaceRefresh).toHaveBeenCalledOnce()                      // 工作区只刷新本地趋势或已有结果
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it.each(['permission', 'instance'] as const)('viewer 刷新中的 %s 变化 abort 目录请求，迟到结果不覆盖新状态', async action => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7', tab: 'sessions' }, permissions: ['ledger:view'] }); await settlePersonnel()
    const pending = deferred<GaussInstance>(); view.directory.instance.mockReturnValueOnce(pending.promise)
    const refreshing = view.refresh(), signal = view.directory.instance.mock.calls.at(-1)![1].signal
    if (action === 'permission') view.allowed.permissions = []
    else await view.router.replace({ query: { instance_id: '12', tenant_id: '7', tab: 'overview' } })
    await settlePersonnel(); expect(signal.aborted).toBe(true)
    pending.resolve({ ...gaussInstance(99), name: '迟到旧实例' }); await refreshing; await settlePersonnel()
    expect(view.model().selected.value?.id ?? null).toBe(action === 'permission' ? null : 12)
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it('返回列表去掉深链接参数并 abort 节点请求，迟到数据不能回填', async () => {
    const pending = deferred<GaussNativeRows>()
    const view = await setup({ query: { instance_id: '11', tenant_id: '7', tab: 'slow-sql', keyword: '保留' },
      configure: api => { api.slowNodes.mockReturnValueOnce(pending.promise) } })
    await settlePersonnel()
    void view.model().loadSlowNodes(); await settlePersonnel()               // 在线查询只在用户操作后发起
    const oldModel = view.model(), signal = view.api.slowNodes.mock.calls[0][1].signal
    await (view.child().props.onBack as () => unknown)(); await settlePersonnel()
    expect(view.router.currentRoute.value.query).toEqual({ keyword: '保留' })
    expect(signal.aborted).toBe(true); expect(oldModel.selected.value).toBeNull()
    pending.resolve(gaussRows({ items: [{ node_id: '旧租户节点' }] })); await settlePersonnel()
    expect(view.child().props.onOpen).toBeTypeOf('function'); expect(oldModel.slowNodes.value).toBeNull()
    expect(textOf(view.root)).not.toContain('旧租户节点')
  })
  it.each(['0', '-1', '1.5', '1e2', '9007199254740992', ['11', '12']])('非法深链接 %j 不请求实例或在线接口', async instance_id => {
    const view = await setup({ query: { instance_id, tenant_id: '7' } }); await settlePersonnel()
    expect(view.directory.instance).not.toHaveBeenCalled(); expect(view.model().selected.value).toBeNull()
    expect(view.model().listError.value).toContain('实例链接无效')
    for (const key of onlineKeys) expect(view.api[key]).not.toHaveBeenCalled()
  })
  it('跨当前租户链接阻止目录与诊断读取，并显示可重新选择的错误', async () => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '8', tab: 'sessions' } }); await settlePersonnel()
    expect(view.directory.instance).not.toHaveBeenCalled(); expect(view.model().selected.value).toBeNull()
    expect(view.model().listError.value).toContain('不属于当前租户'); expect(view.api.overview).not.toHaveBeenCalled()
  })
  it('目录返回错误租户不能打开，404 可读错误不保留前一实例', async () => {
    const view = await setup({ query: { instance_id: '99', tenant_id: '7' }, configure: (_api, directory) => {
      directory.instance.mockResolvedValueOnce(gaussInstance(99, 8))
    } }); await settlePersonnel()
    expect(view.model().selected.value).toBeNull(); expect(view.model().listError.value).toContain('范围不匹配')
    view.directory.instance.mockRejectedValueOnce(new Error('实例不可访问或已移除'))
    await view.router.replace({ query: { instance_id: '100', tenant_id: '7' } }); await settlePersonnel()
    expect(view.model().selected.value).toBeNull(); expect(view.model().listError.value).toContain('实例不可访问或已移除')
    expect(view.api.overview).not.toHaveBeenCalled()
  })
  it('清空租户中止原生与项目请求，旧实例/项目/SQL 全部不可回填', async () => {
    const nodes = deferred<GaussNativeRows>(), projects = deferred<GaussProjectOption[]>()
    const view = await setup({ query: { instance_id: '11', tenant_id: '7', tab: 'slow-sql' }, configure: (api, directory) => {
      api.slowNodes.mockReturnValueOnce(nodes.promise); directory.projects.mockReturnValueOnce(projects.promise)
    } }); await settlePersonnel()
    void view.model().loadSlowNodes(); await settlePersonnel()
    const model = view.model(), nodeSignal = view.api.slowNodes.mock.calls[0][1].signal, projectSignal = view.directory.projects.mock.calls[0][0].signal
    view.app.tenantId = null; await settlePersonnel()
    expect(nodeSignal.aborted).toBe(true); expect(projectSignal.aborted).toBe(true)
    nodes.resolve(gaussRows({ items: [{ node_id: '旧租户节点' }] })); projects.resolve([{ project_ref: 999, project_name: '旧项目', tenant_id: 7, tenant_name: '旧租户', region: null }])
    await settlePersonnel()
    expect(model.slowNodes.value).toBeNull(); expect(model.rows.value).toBeNull(); expect(model.detail.value).toBeNull()
    expect(model.selected.value).toBeNull()
    expect(allNodes(view.root).some(node => Array.isArray(node.props.projects) && (node.props.projects as GaussProjectOption[]).some(project => project.project_ref === 999))).toBe(false)
  })
  it('目录正在打开时返回列表中止请求，迟到目录响应不能重开工作区', async () => {
    const pending = deferred<GaussInstance>()
    const view = await setup({ query: { instance_id: '99', tenant_id: '7' }, configure: (_api, directory) => { directory.instance.mockReturnValueOnce(pending.promise) } })
    await settlePersonnel()
    expect(findNode(view.root, 'section', node => node.props['aria-label'] === '加载实例')).toBeTruthy()
    await view.router.replace({ query: {} }); await settlePersonnel()
    expect(view.directory.instance.mock.calls[0][1].signal.aborted).toBe(true)
    pending.resolve(gaussInstance(99)); await settlePersonnel()
    expect(view.model().selected.value).toBeNull(); expect(view.child().props.onOpen).toBeTypeOf('function')
    expect(view.api.overview).not.toHaveBeenCalled()
  })
  it('项目目录错误可重试，不伪造当前实例页作为项目全集', async () => {
    const view = await setup({ configure: (_api, directory) => { directory.projects.mockRejectedValueOnce(new Error('项目目录暂不可用')) } }); await settlePersonnel()
    expect(view.child().props['projects-error']).toBe('项目目录暂不可用'); expect(view.child().props.projects).toEqual([])
    view.directory.projects.mockResolvedValueOnce([{ project_ref: 202, project_name: '非第一页项目', tenant_id: 7, tenant_name: '租户甲', region: null }])
    await (view.child().props.onRetryProjects as () => unknown)(); await settlePersonnel()
    expect(view.child().props['projects-error']).toBe('')
    expect(view.child().props.projects).toEqual([expect.objectContaining({ project_ref: 202 })])
    expect(view.api.instances).toHaveBeenCalledOnce()
  })
})
