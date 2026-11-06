import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { GaussRequestContext, GaussSlowQuery } from '../src/types/gaussdb'

const request = vi.hoisted(() => ({ get: vi.fn(), download: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { gaussApi, gaussDirectoryApi, gaussDiskApi } from '../src/api/gaussdb'

beforeEach(() => { vi.clearAllMocks() })
const slow = { start_at: '2026-11-02T01:02:03+08:00', end_at: '2026-11-02T02:02:03+08:00', node_ids: ['node-1', 'node-2'], threshold: 3, sql_id: 'sql-1' } satisfies GaussSlowQuery
const endpoints = [
  { name: 'overview', suffix: '/overview', run: (ctx: GaussRequestContext) => gaussApi.overview(11, ctx) },
  { name: 'slow-nodes', suffix: '/slow-nodes', run: (ctx: GaussRequestContext) => gaussApi.slowNodes(11, ctx) },
  { name: 'slow-sql', suffix: '/slow-sql', run: (ctx: GaussRequestContext) => gaussApi.slowSql(11, slow, ctx) },
  { name: 'slow-detail', suffix: '/slow-sql/detail', run: (ctx: GaussRequestContext) => gaussApi.slowDetail(11, slow, ctx) },
  { name: 'sessions', suffix: '/sessions', run: (ctx: GaussRequestContext) => gaussApi.sessions(11, { node_id: 'node-1', component_id: 'cn-1', limit: 50, wait: 'f' }, ctx) },
  { name: 'inspections', suffix: '/inspections', run: (ctx: GaussRequestContext) => gaussApi.inspections(11, { limit: 20, offset: 0 }, ctx) },
  { name: 'inspection-detail', suffix: '/inspections/report-1', run: (ctx: GaussRequestContext) => gaussApi.inspectionDetail(11, 'report-1', ctx) },
]

describe('GaussDB 原生 API 合同', () => {
  it.each([7, null])('目录详情与项目只读 GET，租户 %s 与 abort signal 原样传递', async tenantId => {
    const signal = new AbortController().signal
    const context = { tenantId, signal }
    await gaussDirectoryApi.instance(99, context)
    await gaussDirectoryApi.projects(context)
    const config = { signal, silent: true, timeout: 120000, headers: { 'X-Tenant-Id': tenantId === null ? 'all' : String(tenantId) } }
    expect(request.get.mock.calls).toEqual([
      ['/gaussdb/instances/99', undefined, config], ['/gaussdb/projects', undefined, config],
    ])
    expect(request.download).not.toHaveBeenCalled()
  })
  it('本地存储趋势与导出保留相同时间、分桶及租户上下文', async () => {
    const ctx = { tenantId: 7, signal: new AbortController().signal }
    const query = { start_at: '2026-11-01T00:00:00Z', end_at: '2026-11-02T00:00:00Z', period_sec: 300 }
    await gaussDiskApi.history(11, query, ctx)
    expect(request.get).toHaveBeenCalledWith('/gaussdb/instances/11/disk-history', query, expect.objectContaining({ signal: ctx.signal, headers: { 'X-Tenant-Id': '7' } }))
    await gaussDiskApi.export(11, query, ctx)
    expect(request.download).toHaveBeenCalledWith('/gaussdb/instances/11/export', { panel: 'disk-history', ...query }, 'GaussDB存储趋势.xlsx', expect.objectContaining({ signal: ctx.signal, headers: { 'X-Tenant-Id': '7' } }))
  })
  it('本地实例搜索带分页和项目，显式捕获租户头', async () => {
    const signal = new AbortController().signal
    await gaussApi.instances({ keyword: ' db ', project_ref: 107, page: 2, page_size: 20 }, { signal, tenantId: 7 })
    expect(request.get).toHaveBeenCalledWith('/gaussdb/instances', { keyword: 'db', project_ref: 107, page: 2, page_size: 20 },
      { signal, silent: true, timeout: 120000, headers: { 'X-Tenant-Id': '7' } })
  })
  it('概况统计只读本地，带项目与租户头；状态分组随列表传递', async () => {
    const signal = new AbortController().signal
    await gaussApi.summary({ project_ref: 107 }, { signal, tenantId: null })
    expect(request.get).toHaveBeenCalledWith('/gaussdb/summary', { project_ref: 107 },
      { signal, silent: true, timeout: 120000, headers: { 'X-Tenant-Id': 'all' } })
    await gaussApi.instances({ status_group: 'abnormal', page: 1, page_size: 20 }, { signal, tenantId: 7 })
    expect(request.get.mock.calls.at(-1)![1]).toEqual({ status_group: 'abnormal', keyword: undefined, page: 1, page_size: 20 })
  })
  it.each(endpoints)('$name 只 GET，保留 signal 与原生实例路径', async endpoint => {
    const ctx = { tenantId: 7, signal: new AbortController().signal }
    await endpoint.run(ctx)
    expect(request.get.mock.calls[0][0]).toBe(`/gaussdb/instances/11${endpoint.suffix}`)
    expect(request.get.mock.calls[0][2]).toEqual({ signal: ctx.signal, silent: true, timeout: 120000, headers: { 'X-Tenant-Id': '7' } })
  })
  it.each(endpoints)('$name 全部租户显式 all，切换全局值不补写新租户', async endpoint => {
    const ctx: GaussRequestContext = { tenantId: null, signal: new AbortController().signal }
    const pending = endpoint.run(ctx)
    ctx.tenantId = 8
    await pending
    expect(request.get.mock.calls[0][2].headers).toEqual({ 'X-Tenant-Id': 'all' })
  })
  it('查询与详情保留带时区日期和重复节点数组，阈值不改单位', async () => {
    const ctx = { tenantId: 7, signal: new AbortController().signal }
    await gaussApi.slowSql(11, slow, ctx)
    expect(request.get.mock.calls[0][1]).toEqual(slow)
    expect(request.get.mock.calls[0][1].node_ids).not.toBe(slow.node_ids)
    await gaussApi.slowDetail(11, slow, ctx)
    expect(request.get.mock.calls[1][1].start_at).toBe(slow.start_at)
    expect(request.get.mock.calls[1][1].node_ids).not.toBe(slow.node_ids)
  })
  it.each(['overview', 'slow-sql', 'slow-sql-detail', 'sessions', 'inspections', 'inspection-detail'] as const)('%s 导出走后端 XLSX 下载，使用相同查询参数与范围', async panel => {
    const ctx: GaussRequestContext = { tenantId: null, signal: new AbortController().signal }
    const query = { ...slow, panel, record_id: 'report-1', node_id: 'node-1', component_id: 'cn-1', limit: 20, offset: 40 }
    request.download.mockResolvedValue('原生数据.xlsx')
    const result = gaussApi.export(11, query, ctx)
    ctx.tenantId = 8
    expect(await result).toBe('原生数据.xlsx')
    expect(request.download).toHaveBeenCalledWith('/gaussdb/instances/11/export', query, 'GaussDB诊断.xlsx',
      { signal: ctx.signal, silent: true, timeout: 120000, headers: { 'X-Tenant-Id': 'all' } })
    expect(request.download.mock.calls[0][1].node_ids).not.toBe(query.node_ids)
  })
  it('巡检记录作为一个路径片段编码', async () => {
    await gaussApi.inspectionDetail(11, 'record/a?b', { tenantId: 7, signal: new AbortController().signal })
    expect(request.get.mock.calls[0][0]).toBe('/gaussdb/instances/11/inspections/record%2Fa%3Fb')
  })
})
