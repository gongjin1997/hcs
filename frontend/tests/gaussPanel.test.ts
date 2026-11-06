import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, type EffectScope } from 'vue'
import { useGaussPanel } from '../src/views/gaussdb/useGaussPanel'
import { inspectionPoints, inspectionTime, nativeColumnLabel, nativeText, quantityText, rangeQuery } from '../src/views/gaussdb/gauss-utils'
import type { GaussApi, GaussDirectoryApi, GaussNativeRows, GaussOverviewOut } from '../src/types/gaussdb'
import { gaussInstance, gaussInspection, gaussOverview, gaussRows, inspectionRecord } from './gaussFixtures'
import { deferred } from './businessViewHarness'

const scopes: EffectScope[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
const settle = async () => { await new Promise<void>(resolve => setImmediate(resolve)); await nextTick() }
function setup() {
  const tenant = ref<number | null>(7), canView = ref(true), canRun = ref(true), canExport = ref(true)
  const api = {
    instances: vi.fn<GaussApi['instances']>(async (query, context) => ({ items: [gaussInstance(11, context.tenantId ?? 7), gaussInstance(12, context.tenantId ?? 7)], total: 2, page: query.page, page_size: query.page_size })),
    overview: vi.fn<GaussApi['overview']>(async (id, context) => gaussOverview(id, context.tenantId ?? 7)),
    slowNodes: vi.fn<GaussApi['slowNodes']>(async () => gaussRows({ items: [{ node_id: 'node-1', node_name: '节点甲', role: 'master', component_type: 'CN' }] })),
    slowSql: vi.fn<GaussApi['slowSql']>(async () => gaussRows({ items: [{ sql_id: 'sql-1', sql_text: 'select 1', avg_exec_time: '5000000' }] })),
    slowDetail: vi.fn<GaussApi['slowDetail']>(async () => gaussRows({ items: [{ sql_id: 'sql-1', sql_text: 'select 1', query_plan: 'Seq Scan' }] })),
    sessions: vi.fn<GaussApi['sessions']>(async () => gaussRows({ items: [{ session_id: '1', exec_time: '97', query_runtime: '00:00:22.641774' }] })),
    inspections: vi.fn<GaussApi['inspections']>(async () => gaussRows({ items: [{ ...inspectionRecord }], total: 4 })),
    inspectionDetail: vi.fn<GaussApi['inspectionDetail']>(async () => gaussInspection()),
    export: vi.fn<GaussApi['export']>(async () => '原生数据.xlsx'),
  }
  const scope = effectScope(); scopes.push(scope)
  const model = scope.run(() => useGaussPanel({ tenantId: () => tenant.value, canView: () => canView.value,
    canRun: () => canRun.value, canExport: () => canExport.value, api, now: () => Date.parse('2026-11-02T02:00:00Z') }))!
  async function select() { await settle(); model.selectInstance(model.items.value[0]) }
  async function slowReady() { await select(); model.setPanel('slow-sql'); await model.loadSlowNodes(); model.slowFilters.node_ids = ['node-1']; await model.queryPanel() }
  async function inspectionReady() { await select(); model.setPanel('performance'); await model.queryPanel(); await model.showDetail(model.rows.value!.items[0]) }
  return { model, api, tenant, canView, canRun, canExport, scope, select, slowReady, inspectionReady }
}

describe('GaussDB 显式只读查询与权限', () => {
  it('挂载只读本地实例，选实例/切标签/改筛选不自动调用原生接口', async () => {
    const { model, api, select } = setup()
    await select(); model.setPanel('slow-sql'); model.slowFilters.threshold = 3
    model.setPanel('sessions'); model.sessionFilters.limit = 100
    model.setPanel('performance'); model.setInspectionPage(2)
    await settle()
    expect(api.instances).toHaveBeenCalledOnce()
    for (const key of ['overview', 'slowNodes', 'slowSql', 'slowDetail', 'sessions', 'inspections', 'inspectionDetail', 'export'] as const) expect(api[key]).not.toHaveBeenCalled()
  })
  it('只读 viewer 可以打开实例，但不能触发任何诊断或导出', async () => {
    const { model, api, canRun, select } = setup(); canRun.value = false
    await select()
    expect(model.selected.value?.id).toBe(11)
    expect(await model.queryPanel()).toBe(false)
    expect(await model.queryOverview()).toBe(false)
    expect(await model.loadSlowNodes()).toBe(false)
    expect(await model.exportExcel()).toBeNull()
    model.setPanel('performance'); expect(await model.queryPanel()).toBe(false)
    expect(api.instances).toHaveBeenCalledOnce(); expect(api.overview).not.toHaveBeenCalled(); expect(api.inspections).not.toHaveBeenCalled()
  })
  it('磁盘与组件通过一次概要请求获取，不把配置内存或 CPU 当使用率', async () => {
    const { model, api, select } = setup(); await select(); await model.queryPanel()
    expect(api.overview).toHaveBeenCalledExactlyOnceWith(11, expect.objectContaining({ tenantId: 7 }))
    expect(model.overview.value?.disk.used_gb).toBe(0)
    expect(model.overview.value?.cpu_usage).toBeNull(); expect(model.overview.value?.memory_usage).toBeNull()
    expect(model.components.value.map(item => item.component_type)).toEqual(['CN', 'DN'])
  })
  it('会话选择真实 CN/DN 组件和节点，保留筛选原生值与条数上限', async () => {
    const { model, api, select } = setup(); await select(); model.setPanel('sessions')
    expect(await model.queryPanel()).toBe(false); expect(api.sessions).not.toHaveBeenCalled()
    await model.queryOverview()
    Object.assign(model.sessionFilters, { componentKey: model.componentKey('node-2', 'dn-2'), limit: 120, database_name: ' db ', user_name: ' user ', client_ip: '192.0.2.2', state: 'active', wait: 'f' })
    await model.queryPanel()
    expect(api.sessions).toHaveBeenCalledWith(11, { node_id: 'node-2', component_id: 'dn-2', limit: 120, database_name: 'db', user_name: 'user', client_ip: '192.0.2.2', state: 'active', wait: 'f' }, expect.objectContaining({ tenantId: 7 }))
    expect(model.rows.value?.total).toBeNull()
    expect(model.rows.value?.items[0].query_runtime).toBe('00:00:22.641774')
  })
  it.each([0, 201, 1.5])('会话 limit=%s 不发请求', async limit => {
    const { model, api, select } = setup(); await select(); model.setPanel('sessions'); await model.queryOverview()
    Object.assign(model.sessionFilters, { componentKey: model.componentKey('node-1', 'cn-1'), limit })
    expect(await model.queryPanel()).toBe(false); expect(api.sessions).not.toHaveBeenCalled()
  })
  it('慢 SQL 带时区时间与真实节点，详情复用同次查询时间和 SQL ID', async () => {
    const { model, api, slowReady } = setup(); await slowReady()
    const query = api.slowSql.mock.calls[0][1]
    expect(query).toEqual({ start_at: '2026-11-02T01:00:00.000Z', end_at: '2026-11-02T02:00:00.000Z', node_ids: ['node-1'], threshold: 1, sql_id: undefined })
    await model.showDetail(model.rows.value!.items[0])
    expect(api.slowDetail).toHaveBeenCalledWith(11, { start_at: query.start_at, end_at: query.end_at, node_ids: ['node-1'], sql_id: 'sql-1' }, expect.objectContaining({ tenantId: 7 }))
  })
  it.each([0, 100, 1.1])('慢 SQL threshold=%s 不发请求', async threshold => {
    const { model, api, select } = setup(); await select(); model.setPanel('slow-sql'); await model.loadSlowNodes()
    Object.assign(model.slowFilters, { node_ids: ['node-1'], threshold })
    expect(await model.queryPanel()).toBe(false); expect(api.slowSql).not.toHaveBeenCalled()
  })
  it('SQL明细导出固定已查询的SQL、节点与时间，关闭详情取消在途导出', async () => {
    const { model, api, slowReady } = setup(); await slowReady()
    await model.showDetail(model.rows.value!.items[0])
    expect(model.canExportSlowDetail.value).toBe(true)
    const query = api.slowDetail.mock.calls[0][1]
    const pending = deferred<string>(); api.export.mockReturnValueOnce(pending.promise)
    const exporting = model.exportSlowDetail()
    expect(api.export).toHaveBeenCalledWith(11, { panel: 'slow-sql-detail', ...query }, expect.objectContaining({ tenantId: 7 }))
    const signal = api.export.mock.calls[0][2].signal
    model.closeDetail(); expect(signal.aborted).toBe(true); expect(model.canExportSlowDetail.value).toBe(false)
    pending.resolve('stale.xlsx'); expect(await exporting).toBeNull()
    expect(await model.exportSlowDetail()).toBeNull()
  })
  it('未知节点和非 CN/DN 组件不能提交', async () => {
    const { model, api, select } = setup(); await select(); model.setPanel('slow-sql'); await model.loadSlowNodes()
    model.slowFilters.node_ids = ['unknown']; expect(await model.queryPanel()).toBe(false)
    model.setPanel('sessions'); await model.queryOverview(); model.sessionFilters.componentKey = model.componentKey('node-3', 'cm-3')
    expect(await model.queryPanel()).toBe(false); expect(api.sessions).not.toHaveBeenCalled(); expect(api.slowSql).not.toHaveBeenCalled()
  })
  it.each([
    ['CN', null, null, true], ['CN', 'other', 'ACTIVE', true],
    ['DN', 'master', 'ACTIVE', true], ['DN', 'slave', null, true],
    ['DN', null, 'Primary', true], ['DN', null, 'Standby', true],
    ['DN', null, 'ACTIVE', false], ['DN', 'other', 'Primary', false], ['CM', 'master', 'Primary', false],
  ] as const)('组件 %s role=%s status=%s 的会话资格为 %s', async (component_type, role, status, valid) => {
    const { model, api, select } = setup(); await select(); model.setPanel('sessions')
    const overview = gaussOverview(); overview.components = [{ node_id: 'test-node', node_name: null, component_id: 'test-component', component_type, role, status }]
    api.overview.mockResolvedValueOnce(overview); await model.queryOverview()
    model.sessionFilters.componentKey = model.componentKey('test-node', 'test-component')
    expect(await model.queryPanel()).toBe(valid)
    expect(api.sessions).toHaveBeenCalledTimes(valid ? 1 : 0)
    if (valid) expect(api.sessions.mock.calls[0][1]).toMatchObject({ node_id: 'test-node', component_id: 'test-component' })
  })
  it('巡检本地分页只改筛选；点击查询再请求，范围 0–1000', async () => {
    const { model, api, inspectionReady } = setup(); await inspectionReady(); model.setInspectionPage(3)
    expect(model.rows.value).toBeNull(); expect(model.detail.value).toBeNull(); expect(api.inspections).toHaveBeenCalledOnce()
    await model.queryPanel(); expect(api.inspections.mock.calls[1][1]).toEqual({ limit: 20, offset: 40 })
    model.setInspectionPage(52); expect(model.inspectionFilters.offset).toBe(40); expect(model.error.value).toContain('1000')
    model.inspectionFilters.offset = 1001; expect(await model.queryPanel()).toBe(false); expect(api.inspections).toHaveBeenCalledTimes(2)
  })
  it('没有 ledger:view 时不访问列表，失去权限立即清除实例和详情', async () => {
    const { model, api, canView, inspectionReady } = setup(); await inspectionReady(); canView.value = false
    expect(model.items.value).toEqual([]); expect(model.selected.value).toBeNull(); expect(model.detail.value).toBeNull()
    await model.loadInstances(); expect(api.instances).toHaveBeenCalledOnce()
  })
})

describe('GaussDB 租户、实例、分页和标签边界', () => {
  it('默认 disk → overview 查询 → performance → disk 恢复概要导出，不被初始空缓存遮蔽', async () => {
    const { model, api, select } = setup(); await select()
    expect(model.panel.value).toBe('disk')
    model.setPanel('overview'); await model.queryPanel()
    const overview = model.overview.value
    model.setPanel('performance'); await model.queryPanel(); await model.showDetail(model.rows.value!.items[0])
    model.setPanel('disk')
    expect(model.overview.value).toBe(overview); expect(model.applied.value).toEqual({ panel: 'overview' })
    expect(model.rows.value).toBeNull(); expect(model.detail.value).toBeNull(); expect(model.canExport.value).toBe(true)
    expect(await model.exportExcel()).toBe('原生数据.xlsx')
    expect(api.export).toHaveBeenCalledExactlyOnceWith(11, { panel: 'overview' }, expect.objectContaining({ tenantId: 7 }))
  })
  it('本地 retainSelection 刷新同一实例保留面板、节点、表单、结果和 applied', async () => {
    const { model, api, slowReady } = setup(); await slowReady(); await model.showDetail(model.rows.value!.items[0])
    const rows = model.rows.value, detail = model.detail.value, applied = model.applied.value, nodes = model.slowNodes.value
    const pending = deferred<ReturnType<typeof gaussInstance>>()
    const request = vi.fn<GaussDirectoryApi['instance']>().mockReturnValueOnce(pending.promise)
    const refreshing = model.openInstance(11, request, true)
    expect(model.selected.value?.id).toBe(11); expect(model.rows.value).toBe(rows)
    pending.resolve({ ...gaussInstance(), name: '本地更新名称' }); expect(await refreshing).toBe(true)
    expect(model.selected.value?.name).toBe('本地更新名称'); expect(model.panel.value).toBe('slow-sql')
    expect(model.rows.value).toBe(rows); expect(model.detail.value).toBe(detail); expect(model.applied.value).toBe(applied)
    expect(model.slowNodes.value).toBe(nodes); expect(model.slowFilters.node_ids).toEqual(['node-1'])
    expect(api.slowSql).toHaveBeenCalledOnce(); expect(api.slowDetail).toHaveBeenCalledOnce(); expect(api.overview).not.toHaveBeenCalled()
  })
  it.each(['tenant', 'permission', 'instance', 'back', 'dispose', 'newer'] as const)('retainSelection 的 %s 边界 abort 并忽略晚到本地详情', async action => {
    const { model, tenant, canView, scope, select } = setup(); await select(); model.setPanel('sessions')
    const pending = deferred<ReturnType<typeof gaussInstance>>()
    const request = vi.fn<GaussDirectoryApi['instance']>().mockReturnValueOnce(pending.promise).mockResolvedValue({ ...gaussInstance(), name: '较新名称' })
    const refreshing = model.openInstance(11, request, true), signal = request.mock.calls[0][1].signal
    if (action === 'tenant') tenant.value = 8
    if (action === 'permission') canView.value = false
    if (action === 'instance') model.selectInstance(model.items.value[1])
    if (action === 'back') model.clearSelection()
    if (action === 'dispose') scope.stop()
    if (action === 'newer') expect(await model.openInstance(11, request, true)).toBe(true)
    expect(signal.aborted).toBe(true)
    pending.resolve({ ...gaussInstance(), name: '晚到名称' }); expect(await refreshing).toBe(false)
    expect(model.selected.value?.name).not.toBe('晚到名称'); expect(model.listError.value).toBe('')
    if (action === 'instance') expect(model.selected.value?.id).toBe(12)
    if (action === 'newer') expect(model.selected.value?.name).toBe('较新名称')
  })
  it('目录按 ID 打开非当前列表实例，不扫描分页也不自动调用在线接口', async () => {
    const { model, api } = setup(); await settle()
    const request = vi.fn<GaussDirectoryApi['instance']>(async id => gaussInstance(id))
    expect(await model.openInstance(99, request)).toBe(true)
    expect(model.items.value.map(item => item.id)).toEqual([11, 12])
    expect(model.selected.value?.id).toBe(99)
    expect(request).toHaveBeenCalledExactlyOnceWith(99, expect.objectContaining({ tenantId: 7 }))
    expect(api.instances).toHaveBeenCalledOnce(); expect(api.overview).not.toHaveBeenCalled()
  })
  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])('非法目录实例 ID %s 不请求', async id => {
    const { model } = setup(); await settle()
    const request = vi.fn<GaussDirectoryApi['instance']>()
    expect(await model.openInstance(id, request)).toBe(false); expect(request).not.toHaveBeenCalled()
    expect(model.selected.value).toBeNull()
  })
  it.each([gaussInstance(99, 8), gaussInstance(100, 7)])('拒绝目录返回的错误租户或实例 %j', async instance => {
    const { model } = setup(); await settle()
    expect(await model.openInstance(99, async () => instance)).toBe(false)
    expect(model.selected.value).toBeNull(); expect(model.listError.value).toContain('范围不匹配')
  })
  it.each(['tenant', 'permission', 'back', 'dispose', 'reopen'] as const)('%s 立即中止目录读取，迟到实例不能恢复', async action => {
    const { model, tenant, canView, scope } = setup(); await settle()
    const pending = deferred<ReturnType<typeof gaussInstance>>()
    const request = vi.fn<GaussDirectoryApi['instance']>().mockReturnValueOnce(pending.promise).mockResolvedValue(gaussInstance(100))
    const opening = model.openInstance(99, request), signal = request.mock.calls[0][1].signal
    if (action === 'tenant') tenant.value = 8
    if (action === 'permission') canView.value = false
    if (action === 'back') model.clearSelection()
    if (action === 'dispose') scope.stop()
    if (action === 'reopen') expect(await model.openInstance(100, request)).toBe(true)
    expect(signal.aborted).toBe(true)
    pending.resolve(gaussInstance(99)); expect(await opening).toBe(false)
    expect(model.selected.value?.id ?? null).toBe(action === 'reopen' ? 100 : null)
    expect(model.listError.value).toBe('')
  })
  it('切标签缓存各自结果、详情和 applied 条件，概要、节点和表单保留', async () => {
    const { model, api, slowReady } = setup(); await slowReady()
    await model.showDetail(model.rows.value!.items[0])
    const slowRows = model.rows.value, slowDetail = model.detail.value, slowApplied = model.applied.value
    const nodes = model.slowNodes.value
    model.setPanel('sessions'); expect(model.rows.value).toBeNull(); expect(model.detail.value).toBeNull()
    await model.queryOverview()
    const overview = model.overview.value
    Object.assign(model.sessionFilters, { componentKey: model.componentKey('node-2', 'dn-2'), user_name: '业务用户' })
    await model.queryPanel()
    const sessionRows = model.rows.value, sessionApplied = model.applied.value
    model.setPanel('performance'); await model.queryPanel(); await model.showDetail(model.rows.value!.items[0])
    const report = model.detail.value
    model.setPanel('slow-sql')
    expect(model.rows.value).toBe(slowRows); expect(model.detail.value).toBe(slowDetail); expect(model.applied.value).toBe(slowApplied)
    expect(model.slowNodes.value).toBe(nodes); expect(model.overview.value).toBe(overview)
    expect(model.slowFilters.node_ids).toEqual(['node-1']); expect(model.sessionFilters.user_name).toBe('业务用户')
    await model.exportExcel(); expect(api.export.mock.calls.at(-1)![1]).toEqual({ ...api.slowSql.mock.calls[0][1], panel: 'slow-sql' })
    model.setPanel('sessions'); expect(model.rows.value).toBe(sessionRows); expect(model.detail.value).toBeNull(); expect(model.applied.value).toBe(sessionApplied)
    await model.exportExcel(); expect(api.export.mock.calls.at(-1)![1]).toEqual({ ...api.sessions.mock.calls[0][1], panel: 'sessions' })
    model.setPanel('performance'); expect(model.detail.value).toBe(report); expect(model.inspectionRecord.value?.record_id).toBe('report-1')
    expect(api.slowSql).toHaveBeenCalledOnce(); expect(api.sessions).toHaveBeenCalledOnce(); expect(api.inspections).toHaveBeenCalledOnce()
  })
  it('修改隐藏标签表单只失效该标签结果，不清当前报告或其他标签', async () => {
    const { model, api, slowReady } = setup(); await slowReady()
    model.setPanel('performance'); await model.queryPanel(); await model.showDetail(model.rows.value!.items[0])
    const report = model.detail.value
    model.slowFilters.threshold = 5
    expect(model.detail.value).toBe(report); expect(model.applied.value?.panel).toBe('inspections')
    model.setPanel('slow-sql'); expect(model.rows.value).toBeNull(); expect(model.canExport.value).toBe(false)
    expect(model.slowNodes.value?.items).toHaveLength(1)
    await model.queryPanel(); expect(api.slowSql.mock.calls.at(-1)![1].threshold).toBe(5)
    model.setPanel('performance'); expect(model.detail.value).toBe(report)
  })
  it.each(['tenant', 'instance', 'permission'] as const)('%s 切换清除所有标签缓存，重新进入不恢复旧 SQL', async action => {
    const { model, tenant, canRun, slowReady } = setup(); await slowReady()
    await model.showDetail(model.rows.value!.items[0]); model.setPanel('sessions')
    if (action === 'tenant') { tenant.value = 8; await settle(); model.selectInstance(model.items.value[0]) }
    if (action === 'instance') model.selectInstance(model.items.value[1])
    if (action === 'permission') { canRun.value = false; canRun.value = true }
    model.setPanel('slow-sql')
    expect(model.rows.value).toBeNull(); expect(model.detail.value).toBeNull(); expect(model.applied.value).toBeNull()
    expect(model.overview.value).toBeNull(); expect(model.slowNodes.value).toBeNull(); expect(model.canExport.value).toBe(false)
  })
  it('切租户中止原生请求，旧响应和错误都不能回填', async () => {
    const { model, api, tenant, select } = setup(); await select()
    const pending = deferred<GaussOverviewOut>(); api.overview.mockReturnValueOnce(pending.promise)
    const querying = model.queryPanel(); const ctx = api.overview.mock.calls[0][1]
    tenant.value = 8
    expect(ctx.signal.aborted).toBe(true); expect(model.selected.value).toBeNull(); expect(model.overview.value).toBeNull()
    pending.resolve(gaussOverview()); await querying; await settle()
    expect(model.overview.value).toBeNull(); expect(model.items.value.every(item => item.tenant_id === 8)).toBe(true)
    model.selectInstance(model.items.value[0]); await model.queryPanel(); expect(model.overview.value?.instance.tenant_id).toBe(8)
  })
  it('租户列表旧请求也 abort，清关键词与项目筛选，不显示迟到租户', async () => {
    const { model, api, tenant } = setup(); await settle()
    model.listFilters.keyword = 'secret'; model.listFilters.project_ref = 107
    const pending = deferred<Awaited<ReturnType<GaussApi['instances']>>>(); api.instances.mockReturnValueOnce(pending.promise)
    const querying = model.loadInstances(); const ctx = api.instances.mock.calls[1][1]
    tenant.value = 8; expect(ctx.signal.aborted).toBe(true); expect(model.listFilters.keyword).toBe(''); expect(model.listFilters.project_ref).toBeNull()
    pending.resolve({ items: [gaussInstance(99, 7)], total: 1, page: 1, page_size: 20 }); await querying; await settle()
    expect(model.items.value.map(item => item.tenant_id)).toEqual([8, 8])
  })
  it('切实例中止 SQL 详情，清空旧 SQL、节点和筛选', async () => {
    const { model, api, slowReady } = setup(); await slowReady()
    const pending = deferred<GaussNativeRows>(); api.slowDetail.mockReturnValueOnce(pending.promise)
    const query = model.showDetail(model.rows.value!.items[0]); const ctx = api.slowDetail.mock.calls[0][2]
    model.selectInstance(model.items.value[1]); expect(ctx.signal.aborted).toBe(true)
    expect(model.rows.value).toBeNull(); expect(model.slowNodes.value).toBeNull(); expect(model.slowFilters.node_ids).toEqual([])
    pending.resolve(gaussRows({ items: [{ sql_text: '旧敏感 SQL' }] })); await query
    expect(model.detail.value).toBeNull(); expect(model.selected.value?.id).toBe(12)
  })
  it.each(['tab', 'page', 'filter', 'permission', 'cancel', 'dispose'] as const)('%s 变化立即取消旧详情并清空敏感数据', async action => {
    const { model, api, canRun, scope, inspectionReady } = setup(); await inspectionReady()
    const pending = deferred<GaussNativeRows>(); api.inspectionDetail.mockReturnValueOnce(pending.promise)
    const querying = model.showDetail(model.rows.value!.items[0]); const ctx = api.inspectionDetail.mock.calls[1][2]
    if (action === 'tab') model.setPanel('sessions')
    if (action === 'page') await model.changePage(2)
    if (action === 'filter') model.inspectionFilters.offset = 20
    if (action === 'permission') canRun.value = false
    if (action === 'cancel') model.cancelQuery()
    if (action === 'dispose') scope.stop()
    expect(ctx.signal.aborted).toBe(true); expect(model.detail.value).toBeNull()
    pending.reject(new Error('旧请求错误不应展示')); await querying
    expect(model.detail.value).toBeNull(); expect(model.error.value).toBe('')
  })
  it('拒绝同请求返回的跨租户实例与错误来源', async () => {
    const { model, api, select } = setup(); await select()
    api.overview.mockResolvedValueOnce(gaussOverview(11, 8)); expect(await model.queryPanel()).toBe(false)
    expect(model.overview.value).toBeNull(); expect(model.error.value).toContain('范围不匹配')
    model.setPanel('performance'); api.inspections.mockResolvedValueOnce({ ...gaussRows(), source: 'AOM' } as unknown as GaussNativeRows)
    expect(await model.queryPanel()).toBe(false); expect(model.rows.value).toBeNull(); expect(model.error.value).toContain('来源不匹配')
  })
  it('重复刷新只接受最后结果，不让旧错误覆盖成功', async () => {
    const { model, api, select } = setup(); await select()
    const old = deferred<GaussOverviewOut>(); api.overview.mockReturnValueOnce(old.promise)
    const previous = model.queryPanel(); await model.queryPanel()
    expect(api.overview.mock.calls[0][1].signal.aborted).toBe(true)
    old.reject(new Error('迟到错误')); await previous
    expect(model.overview.value?.instance.id).toBe(11); expect(model.error.value).toBe(''); expect(model.loading.value).toBe(false)
  })
})

describe('GaussDB 原生 XLSX 导出', () => {
  it('只有查询完成且有额外导出权限时导出，保留已查条件', async () => {
    const { model, api, canExport, slowReady } = setup(); expect(await model.exportExcel()).toBeNull(); await slowReady()
    canExport.value = false; expect(await model.exportExcel()).toBeNull(); canExport.value = true
    expect(await model.exportExcel()).toBe('原生数据.xlsx')
    expect(api.export).toHaveBeenCalledWith(11, { panel: 'slow-sql', ...api.slowSql.mock.calls[0][1] }, expect.objectContaining({ tenantId: 7 }))
    model.slowFilters.threshold = 5; expect(model.canExport.value).toBe(false); expect(await model.exportExcel()).toBeNull()
  })
  it('巡检详情导出指定记录，切租户中止下载且不回填成功', async () => {
    const { model, api, tenant, inspectionReady } = setup(); await inspectionReady()
    const pending = deferred<string>(); api.export.mockReturnValueOnce(pending.promise)
    const exporting = model.exportInspectionDetail(); expect(api.export).toHaveBeenCalledWith(11, { panel: 'inspection-detail', record_id: 'report-1' }, expect.objectContaining({ tenantId: 7 }))
    tenant.value = 8; expect(api.export.mock.calls[0][2].signal.aborted).toBe(true); expect(model.exporting.value).toBe(false)
    pending.resolve('旧租户文件.xlsx'); expect(await exporting).toBeNull(); expect(model.exportError.value).toBe('')
  })
  it('关闭巡检详情取消在途下载，不保留旧记录', async () => {
    const { model, api, inspectionReady } = setup(); await inspectionReady()
    const pending = deferred<string>(); api.export.mockReturnValueOnce(pending.promise)
    const exporting = model.exportInspectionDetail(); model.closeDetail()
    expect(api.export.mock.calls[0][2].signal.aborted).toBe(true); expect(model.inspectionRecord.value).toBeNull()
    pending.reject(new Error('旧导出错误')); expect(await exporting).toBeNull(); expect(model.exportError.value).toBe('')
  })
})

describe('GaussDB 原生单位与巡检数据', () => {
  it('对象和数组保留真实时间与 0/null，不补时段、不将统计值作为点', () => {
    const point = { time: [1709163360000, 1709163420000], metrics: [0, null] }
    expect(inspectionPoints(point)).toEqual([{ time: 1709163360000, value: 0, block: 0, index: 0 }, { time: 1709163420000, value: null, block: 0, index: 1 }])
    expect(inspectionPoints([point, { time: [1709163480000], metrics: [90.01] }])).toHaveLength(3)
    expect(inspectionPoints({ max: 99, average: 50 })).toEqual([])
    expect(inspectionPoints({ time: [1709163360000], metrics: [90, 80] })[1]).toEqual({ time: null, value: 80, block: 0, index: 1 })
    expect(inspectionTime(1709163360000)).toBe(new Date(1709163360000).toLocaleString('zh-CN', { hour12: false }))
  })
  it('未知单位显示未确认；微秒、秒和原始时长字符串均不换算', () => {
    expect(quantityText(32, null)).toBe('32 （单位未确认）'); expect(quantityText(null, 'GB')).toBe('—')
    expect(quantityText(0, 'GB')).toBe('0 GB'); expect(nativeText(null)).toBe('—'); expect(nativeText(0)).toBe('0')
    expect(nativeColumnLabel('avg_exec_time')).toBe('平均执行耗时（μs）')
    expect(nativeColumnLabel('exec_time')).toBe('会话建立时长（秒）')
    expect(nativeColumnLabel('query_runtime')).toBe('语句执行时长（原始字符串）')
    expect(nativeText('00:00:22.641774')).toBe('00:00:22.641774')
  })
  it.each([
    ['fetched_pages', '扫描页数'], ['hit_pages', '命中页数'], ['lock_count', '加锁次数'],
    ['total_time', '总耗时（μs）'], ['cpu_time', 'CPU 耗时（μs）'], ['plan_time', '计划耗时（μs）'],
    ['io_time', 'I/O 耗时（μs）'], ['lock_time', '加锁耗时（μs）'],
  ])('慢 SQL 详情 %s 使用正确中文和单位', (key, label) => {
    expect(nativeColumnLabel(key)).toBe(label)
    if (label.endsWith('（μs）')) expect(nativeColumnLabel(key, { [key]: '微秒' })).toBe(label.replace('μs', '微秒'))
  })
  it('原生 UTC 字符串不转本地，数字毫秒按值标注本地，混合列明确区分', () => {
    const raw = '2024-03-26 11:23:47 +0000', numeric = 1709163360000
    expect(inspectionTime(raw)).toBe(raw)
    expect(inspectionTime(String(numeric))).toBe(String(numeric))
    expect(nativeColumnLabel('start_time')).toBe('开始时间（原生时间）')
    expect(nativeColumnLabel('start_time', {}, [raw])).toBe('开始时间（原生时间）')
    expect(nativeColumnLabel('finish_time', {}, [numeric])).toBe('结束时间（本地）')
    expect(nativeColumnLabel('start_time', {}, [raw, numeric])).toBe('开始时间（本地 / 原生时间）')
    expect(nativeColumnLabel('start_time', {}, [null, Number.NaN])).toBe('开始时间（原生时间）')
  })
  it('时间范围带时区，空时间可查询全部已有记录，倒序/无效时间不提交', () => {
    expect(rangeQuery([new Date('2026-11-02T09:00:00+08:00'), new Date('2026-11-02T10:00:00+08:00')])).toEqual({ start_at: '2026-11-02T01:00:00.000Z', end_at: '2026-11-02T02:00:00.000Z' })
    expect(rangeQuery(null, false)).toEqual({})
    expect(() => rangeQuery(null)).toThrow('请选择')
    expect(() => rangeQuery([new Date(2), new Date(1)])).toThrow('结束时间')
    expect(() => rangeQuery([new Date('invalid'), new Date()])).toThrow('请选择')
  })
})
