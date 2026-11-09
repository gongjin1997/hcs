import { describe, expect, it, vi } from 'vitest'
const request = vi.hoisted(() => ({ get: vi.fn(async () => ({})), put: vi.fn(async () => ({})) }))
vi.mock('../src/api/request', () => request)
import { discoverSelectionJobs, getJobSelection, getJobSelectionTargets, saveJobSelection } from '../src/api/job-selection'

describe('监控名单 API 作用域', () => {
  it('租户项目工作空间显式传递，名单保存与 GET 发现使用独立接口', async () => {
    await getJobSelectionTargets()
    expect(request.get).toHaveBeenLastCalledWith('/system/job-selections/targets', undefined,
      expect.objectContaining({ headers: { 'X-Tenant-Id': 'all' } }))
    await getJobSelection(11, 'dgc_realtime_job', 101, 'ws-a')
    expect(request.get).toHaveBeenLastCalledWith('/system/job-selections/11/dgc_realtime_job',
      { project_ref: 101, workspace: 'ws-a' }, expect.objectContaining({ headers: { 'X-Tenant-Id': '11' } }))
    const query = { project_ref: 101, workspace: 'ws-a', page: 3, page_size: 100, keyword: 'job' }
    await discoverSelectionJobs(11, 'dgc_realtime_job', query)
    expect(request.get).toHaveBeenLastCalledWith('/system/job-selections/11/dgc_realtime_job/jobs', query,
      expect.objectContaining({ headers: { 'X-Tenant-Id': '11' } }))
    const body = { project_ref: 101, workspace: 'ws-a', mode: 'selected' as const, jobs: [] }
    await saveJobSelection(11, 'dgc_realtime_job', body)
    expect(request.put).toHaveBeenLastCalledWith('/system/job-selections/11/dgc_realtime_job', body,
      { headers: { 'X-Tenant-Id': '11' } })
  })
})
