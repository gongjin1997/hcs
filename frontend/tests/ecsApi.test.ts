import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ecsPolicy } from './ecsFixtures'
import { ecsRangeQuery } from '../src/views/ecs/ecs-utils'
const request = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), download: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { listEcsInstances, getEcsHistory, exportEcsHistory, getEcsPolicy, saveEcsPolicy, listEcsQuotas, refreshEcsQuota } from '../src/api/ecs'
beforeEach(() => vi.clearAllMocks())

describe('ECS API 合同', () => {
  const context = { tenantId: 7, signal: new AbortController().signal }
  it('实例查询保留租户、项目和布尔过滤', async () => {
    await listEcsInstances({ page: 2, page_size: 100, keyword: ' vm ', project_ref: 9, monitored: false, critical: true }, context)
    expect(request.get).toHaveBeenCalledWith('/ecs/instances', { page: 2, page_size: 100, keyword: 'vm', project_ref: 9, monitored: false, critical: true, tenant_id: 7 },
      { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
  })
  it('历史与真实 XLSX 下载使用完全相同的实例、时间、指标及粒度', async () => {
    const query = { ...ecsRangeQuery('7d'), metrics: ['cpu_util', 'mem_util'] as const }
    const payload = { ...query, metrics: [...query.metrics] }
    await getEcsHistory(11, payload, context)
    await exportEcsHistory(11, payload, context)
    expect(request.get.mock.calls[0][0]).toBe('/ecs/instances/11/history')
    expect(request.download.mock.calls[0][0]).toBe('/ecs/instances/11/export')
    expect(request.download.mock.calls[0][1]).toEqual(request.get.mock.calls[0][1])
    expect(request.download.mock.calls[0][1]).not.toHaveProperty('page')
    expect(request.get.mock.calls[0][1].metrics).not.toBe(payload.metrics)
    expect(request.download.mock.calls[0][2]).toMatch(/\.xlsx$/)
  })
  it('策略只保存精确 policy，不提交 master gate 或其他租户字段', async () => {
    const body = ecsPolicy().policy
    await getEcsPolicy(7, context)
    await saveEcsPolicy(7, body, context)
    expect(request.get.mock.calls[0][0]).toBe('/system/collection-policies/7/ecs_server')
    expect(request.put).toHaveBeenCalledWith('/system/collection-policies/7/ecs_server', body,
      { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
    expect(request.put.mock.calls[0][1]).not.toHaveProperty('master_enabled')
  })
  it('配额 GET 读缓存，云端刷新只 POST 单项目并捕获该租户', async () => {
    await listEcsQuotas({ page: 1, page_size: 20 }, context)
    expect(request.post).not.toHaveBeenCalled()
    expect(request.get.mock.calls[0][0]).toBe('/ecs/quotas')
    await refreshEcsQuota(15, context)
    expect(request.post).toHaveBeenCalledWith('/ecs/quotas/15/refresh', undefined,
      { params: { tenant_id: 7 }, signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' }, timeout: 120000 })
  })
  it('全部租户请求显式 all，避免请求层补上已切换的租户头', async () => {
    const allContext = { tenantId: null, signal: context.signal }
    await listEcsInstances({ page: 1, page_size: 20 }, allContext)
    await listEcsQuotas({ page: 1, page_size: 20 }, allContext)
    await getEcsHistory(11, ecsRangeQuery('1h'), allContext)
    expect(request.get.mock.calls.every(call => call[2].headers['X-Tenant-Id'] === 'all')).toBe(true)
  })
})
