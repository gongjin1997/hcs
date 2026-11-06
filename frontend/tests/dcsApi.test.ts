import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DcsHistoryQuery } from '../src/types/dcs'

const request = vi.hoisted(() => ({ get: vi.fn(), download: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { exportDcsHistory, getDcsHistory, getDcsInstance, listDcsInstances } from '../src/api/dcs'

beforeEach(() => { vi.clearAllMocks() })
const query: DcsHistoryQuery = { resource_ids: [1, 2], start_at: '2026-10-26T00:03:04+08:00', end_at: '2026-10-27T00:03:04+08:00', interval_minutes: 15 }

describe('DCS API请求合同', () => {
  it('单实例深链使用本地主键和固定租户头，不使用搜索分页', async () => {
    const signal = new AbortController().signal
    await getDcsInstance(901, { signal, tenantId: 7 })
    expect(request.get).toHaveBeenCalledExactlyOnceWith('/dcs/instances/901', undefined,
      { signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
  })
  it('实例搜索分页使用当前租户/项目范围', async () => {
    const signal = new AbortController().signal
    await listDcsInstances({ keyword: ' cache ', page: 2, page_size: 50 }, { signal, tenantId: 7, projectRef: 9 })
    expect(request.get).toHaveBeenCalledWith('/dcs/instances', { keyword: 'cache', page: 2, page_size: 50, tenant_id: 7, project_ref: 9 },
      { signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
  })

  it('历史请求保留重复参数数组、带时区时间和120秒超时', async () => {
    const signal = new AbortController().signal
    await getDcsHistory(query, { signal, tenantId: 7 })
    expect(request.get).toHaveBeenCalledWith('/dcs/history', query, { timeout: 120000, signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
    expect(request.get.mock.calls[0][1].resource_ids).not.toBe(query.resource_ids)
  })

  it('全量导出使用真实下载接口与同一筛选，不构造文件或发送前端分页', async () => {
    const signal = new AbortController().signal
    request.download.mockResolvedValue('后端文件名.xlsx')
    expect(await exportDcsHistory(query, { signal, tenantId: null })).toBe('后端文件名.xlsx')
    expect(request.download).toHaveBeenCalledWith('/dcs/export', query, 'DCS内存指标.xlsx', { timeout: 120000, signal, silent: true, headers: { 'X-Tenant-Id': 'all' } })
    expect(request.download.mock.calls[0][1]).not.toHaveProperty('page')
    expect(request.download.mock.calls[0][1]).not.toHaveProperty('page_size')
    expect(request.download.mock.calls[0][1].resource_ids).not.toBe(query.resource_ids)
  })
})


describe('捕获全部租户范围', () => {
  it.each(['instances', 'history', 'export'] as const)('%s 显式传 all，不给全局租户头补写的机会', async endpoint => {
    const context = { signal: new AbortController().signal, tenantId: null as number | null }
    const pending = endpoint === 'instances'
      ? listDcsInstances({ page: 1, page_size: 50 }, context)
      : endpoint === 'history' ? getDcsHistory(query, context) : exportDcsHistory(query, context)
    context.tenantId = 99
    await pending
    if (endpoint === 'export') {
      expect(request.download.mock.calls[0][3].headers).toEqual({ 'X-Tenant-Id': 'all' })
    } else {
      expect(request.get.mock.calls[0][2].headers).toEqual({ 'X-Tenant-Id': 'all' })
      if (endpoint === 'instances') expect(request.get.mock.calls[0][1].tenant_id).toBeUndefined()
    }
  })
})
