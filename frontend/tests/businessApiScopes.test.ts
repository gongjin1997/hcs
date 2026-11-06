import { beforeEach, describe, expect, it, vi } from 'vitest'
const request = vi.hoisted(() => ({ get: vi.fn(), download: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { exportJobReport, fetchJobReport } from '../src/api/job-reports'
import { importLedgerFile } from '../src/api/ledger'
beforeEach(() => { vi.clearAllMocks() })
describe('业务接口固定范围与参数快照', () => {
  it.each(['query', 'export'] as const)('作业%s显式固定全部租户和状态数组', async kind => {
    const signal = new AbortController().signal
    const status = ['FAILED']
    if (kind === 'query') await fetchJobReport('mrs', { status, page: 3, page_size: 20 }, { signal, tenantId: null })
    else await exportJobReport('mrs', { status, page: 3, page_size: 20 }, { signal, tenantId: null })
    status.push('RUNNING')
    const call = kind === 'query' ? request.get.mock.calls[0] : request.download.mock.calls[0]
    expect(call[1].status).toEqual(['FAILED'])
    expect(call[kind === 'query' ? 2 : 3].headers).toEqual({ 'X-Tenant-Id': 'all' })
    if (kind === 'export') expect(call[1]).not.toHaveProperty('page')
  })

  it('导入预览绑定文件与租户、应用仍发送同一捕获范围', async () => {
    const file = new File(['QA'], 'QA.xlsx')
    const signal = new AbortController().signal
    await importLedgerFile(file, true, false, { tenantId: 7, signal })
    expect(request.post.mock.calls[0][1].get('file')).toBe(file)
    expect(request.post.mock.calls[0][2]).toMatchObject({ params: { dry_run: true, skip_errors: false }, signal, headers: { 'X-Tenant-Id': '7' } })
    await importLedgerFile(file, false, true, { tenantId: null })
    expect(request.post.mock.calls[1][2].headers).toEqual({ 'X-Tenant-Id': 'all' })
  })
})
