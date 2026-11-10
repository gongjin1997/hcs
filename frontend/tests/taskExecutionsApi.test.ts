import { beforeEach, describe, expect, it, vi } from 'vitest'
const request = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { cancelTaskExecution, enqueueTaskExecution, getTaskExecution, getTaskExecutionTargets, listTaskDefinitions, listTaskExecutions, retryTaskExecution, updateTaskDefinition } from '../src/api/task-executions'

beforeEach(() => vi.clearAllMocks())
describe('持久巡检执行的真实 API 契约', () => {
  const signal = new AbortController().signal
  const context = { tenantId: 7, signal }
  const config = { headers: { 'X-Tenant-Id': '7' }, signal, silent: true }
  it('入队只提交项目与幂等键，不等待旧 run_all 长请求', async () => {
    await enqueueTaskExecution('mrs_job_status', { project_refs: [11, 12], idempotency_key: 'request-1' }, context)
    expect(request.post).toHaveBeenCalledExactlyOnceWith('/system/tasks/mrs_job_status/run', { project_refs: [11, 12], idempotency_key: 'request-1' }, config)
    expect(request.post.mock.calls[0]![2]).not.toHaveProperty('timeout')
  })
  it('省略和 null 继承 Scope，显式空数组不得被前端转换成全范围', async () => {
    await enqueueTaskExecution('mrs_job_status', undefined, context)
    await enqueueTaskExecution('mrs_job_status', { project_refs: null, idempotency_key: null }, context)
    await enqueueTaskExecution('mrs_job_status', { project_refs: [] }, context)
    expect(request.post.mock.calls.map(call => call[1])).toEqual([
      undefined, { project_refs: null, idempotency_key: null }, { project_refs: [] },
    ])
  })
  it('分页、任务、项目与单个真实状态完整传入查询', async () => {
    const query = { task_code: 'mrs_job_status', project_ref: 12, status: 'QUEUED' as const, page: 2, page_size: 20 }
    await listTaskExecutions(query, context)
    expect(request.get).toHaveBeenCalledExactlyOnceWith('/system/executions', query, config)
  })
  it('目标与单次详情均显式固定租户及取消信号', async () => {
    await getTaskExecutionTargets('mrs_job_status', context)
    await getTaskExecution('execution-1', context)
    expect(request.get.mock.calls).toEqual([
      ['/system/tasks/mrs_job_status/targets', undefined, config], ['/system/executions/execution-1', undefined, config],
    ])
  })
  it('停止仅调用本地 execution/cancel，重试调用独立的新执行入口', async () => {
    await cancelTaskExecution('execution-1', context)
    await retryTaskExecution('execution-1', context)
    expect(request.post.mock.calls).toEqual([
      ['/system/executions/execution-1/cancel', undefined, config], ['/system/executions/execution-1/retry', undefined, config],
    ])
    expect(request.post.mock.calls.every(call => !String(call[0]).includes('/mrs/'))).toBe(true)
  })
  it('MRS 完整配置的空对象重置原样提交，不转换成 null', async () => {
    await updateTaskDefinition('mrs_job_status', { mrs_options: {} }, context)
    expect(request.put).toHaveBeenCalledExactlyOnceWith('/system/tasks/mrs_job_status', { mrs_options: {} }, config)
  })
  it('全部租户显式 all；共享配置使用已确认的 timeout_sec 和 mrs_options', async () => {
    const all = { tenantId: null, signal }
    await listTaskDefinitions(all)
    const body = { timeout_sec: 180, mrs_options: { query_mode: 'active_only', max_calls: 40 } }
    await updateTaskDefinition('mrs_job_status', body, all)
    expect(request.get.mock.calls[0]![2]).toEqual({ headers: { 'X-Tenant-Id': 'all' }, signal, silent: true })
    expect(request.put).toHaveBeenCalledExactlyOnceWith('/system/tasks/mrs_job_status', body, { headers: { 'X-Tenant-Id': 'all' }, signal, silent: true })
  })
})
