import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef } from 'vue'
import type { TaskExecution, TaskExecutionContext } from '../src/types/task-executions'
import { useTaskExecutions } from '../src/views/system/composables/useTaskExecutions'
import { admission, execution } from './taskExecutionFixtures'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.useRealTimers() })
const settle = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve() }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function workspace(overrides: Partial<Parameters<typeof useTaskExecutions>[1]> = {}, allowed = true) {
  const scope = effectScope()
  const tenant = shallowRef<number | null>(7), projectScope = shallowRef('session'), enabled = shallowRef(true)
  const api = {
    listTaskExecutions: vi.fn(async () => ({ items: [execution()], total: 1, page: 1, page_size: 20 })),
    getTaskExecution: vi.fn(async (id: string) => execution({ id, execution_id: id })),
    getTaskExecutionTargets: vi.fn(async (code: string) => ({ task_code: code, task_enabled: true, targets: [] })),
    enqueueTaskExecution: vi.fn(async () => admission()),
    cancelTaskExecution: vi.fn(async () => execution({ cancel_requested: true })),
    retryTaskExecution: vi.fn(async () => admission()),
    ...overrides,
  }
  const notice = vi.fn(), changed = vi.fn(), confirm = vi.fn(async () => true), makeRequestKey = vi.fn(() => 'idempotency-1')
  const state = scope.run(() => useTaskExecutions({ scopeKey: () => `${tenant.value}:${projectScope.value}`, tenantId: () => tenant.value,
    enabled: () => enabled.value, canRun: () => allowed, notice, changed, confirmCancel: confirm, makeRequestKey,
    accepted: () => { enabled.value = true },
  }, api))!
  dispose.push(() => scope.stop())
  return { state, api, tenant, projectScope, enabled, notice, changed, confirm, makeRequestKey, stop: () => scope.stop() }
}

describe('真实执行生命周期与写操作', () => {
  it('重新进入工作区从后端恢复 RUNNING，而不是依赖按钮 loading', async () => {
    const view = workspace()
    await settle()
    expect(view.state.feed.data.value?.items[0]?.status).toBe('RUNNING')
    expect(view.state.pendingTasks.size).toBe(0)
    view.stop()
    const reopened = workspace()
    await settle()
    expect(reopened.state.feed.data.value?.items[0]?.execution_id).toBe('execution-old')
    expect(reopened.state.feed.data.value?.items[0]?.status).toBe('RUNNING')
  })

  it('202 accepted 立即释放提交按钮，执行仍真实保持 QUEUED', async () => {
    const view = workspace()
    const result = await view.state.run('mrs_job_status', [102, 101, 101])
    expect(result?.status).toBe('QUEUED')
    expect(view.state.pendingTasks.has('mrs_job_status')).toBe(false)
    expect(view.api.enqueueTaskExecution).toHaveBeenCalledWith('mrs_job_status', { project_refs: [101, 102], idempotency_key: 'idempotency-1' }, expect.objectContaining({ tenantId: 7, signal: expect.any(AbortSignal) }))
    expect(view.notice).toHaveBeenCalledWith('success', expect.stringContaining('已接受 1 条执行'))
  })

  it('同一任务的重复点击不重复 POST，未确认的再次提交复用幂等键', async () => {
    const pending = deferred<ReturnType<typeof admission>>()
    const enqueueTaskExecution = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(admission())
    const view = workspace({ enqueueTaskExecution })
    const first = view.state.run('mrs_job_status')
    await view.state.run('mrs_job_status')
    expect(enqueueTaskExecution).toHaveBeenCalledTimes(1)
    pending.reject(new Error('连接中断')); await first
    await view.state.run('mrs_job_status')
    expect(enqueueTaskExecution.mock.calls[0]![1]).toEqual(enqueueTaskExecution.mock.calls[1]![1])
    expect(view.makeRequestKey).toHaveBeenCalledOnce()
  })

  it('停止只请求本地取消；RUNNING 在后台确认前不被前端改为终态', async () => {
    const view = workspace()
    await settle()
    await view.state.cancel(execution())
    expect(view.confirm).toHaveBeenCalledOnce()
    expect(view.api.cancelTaskExecution).toHaveBeenCalledWith('execution-old', expect.objectContaining({ tenantId: 7 }))
    expect(view.state.feed.data.value?.items[0]?.status).toBe('RUNNING')
    expect(view.notice).toHaveBeenCalledWith('info', expect.stringContaining('云端作业不受影响'))
  })

  it('取消确认期间切换租户，不向新租户发送旧执行的写请求', async () => {
    const pending = deferred<boolean>()
    const view = workspace()
    view.confirm.mockImplementation(() => pending.promise)
    const stopping = view.state.cancel(execution())
    view.tenant.value = 8
    pending.resolve(true); await stopping
    expect(view.api.cancelTaskExecution).not.toHaveBeenCalled()
    expect(view.state.pendingActions).toEqual({})
  })

  it('已请求取消或已完成的执行不能再次取消，运行中的执行不能重试', async () => {
    const view = workspace()
    await view.state.cancel(execution({ cancel_requested: true }))
    await view.state.cancel(execution({ status: 'SUCCESS' }))
    await view.state.retry(execution())
    expect(view.api.cancelTaskExecution).not.toHaveBeenCalled()
    expect(view.api.retryTaskExecution).not.toHaveBeenCalled()
  })

  it('重试创建新执行并打开新 ID，原失败记录保持完整', async () => {
    const original = execution({ status: 'FAILED', finished_at: '2026-11-10T01:00:10Z', error_summary: '上游查询失败' })
    const view = workspace()
    const before = structuredClone(original)
    const result = await view.state.retry(original)
    expect(result?.execution_ids).toEqual(['execution-new'])
    expect(view.api.retryTaskExecution).toHaveBeenCalledWith('execution-old', expect.objectContaining({ tenantId: 7 }))
    expect(view.state.selectedId.value).toBe('execution-new')
    expect(original).toEqual(before)
  })

  it('只读用户只能 GET，直接调用所有写处理函数也不会发请求', async () => {
    const view = workspace({}, false)
    await view.state.run('mrs_job_status')
    await view.state.cancel(execution())
    await view.state.retry(execution({ status: 'FAILED' }))
    view.state.chooseTargets('mrs_job_status')
    await settle()
    expect(view.api.listTaskExecutions).toHaveBeenCalled()
    expect(view.api.enqueueTaskExecution).not.toHaveBeenCalled()
    expect(view.api.cancelTaskExecution).not.toHaveBeenCalled()
    expect(view.api.retryTaskExecution).not.toHaveBeenCalled()
    expect(view.makeRequestKey).not.toHaveBeenCalled()
    expect(view.state.targetTaskCode.value).toBe('')
  })

  it('旧租户的 accepted 迟到响应不通知、不打开详情、不污染新范围', async () => {
    const pending = deferred<ReturnType<typeof admission>>()
    const view = workspace({ enqueueTaskExecution: vi.fn(() => pending.promise) })
    const submitting = view.state.run('mrs_job_status')
    view.tenant.value = 8
    pending.resolve(admission()); await submitting
    expect(view.notice).not.toHaveBeenCalled()
    expect(view.state.pendingTasks.size).toBe(0)
    expect(view.state.selectedId.value).toBe('')
    expect(view.changed).not.toHaveBeenCalled()
  })

  it('切换租户立即停止旧详情查询，不带旧 execution ID 向新租户发请求', async () => {
    const pending = deferred<TaskExecution>()
    const getTaskExecution = vi.fn((_id: string, _context: TaskExecutionContext) => pending.promise)
    const view = workspace({ getTaskExecution })
    view.state.open('execution-old')
    expect(getTaskExecution).toHaveBeenCalledTimes(1)
    view.tenant.value = 8
    expect(getTaskExecution).toHaveBeenCalledTimes(1)
    expect(getTaskExecution.mock.calls[0]![1].signal?.aborted).toBe(true)
    pending.resolve(execution()); await settle()
    expect(view.state.detail.data.value).toBeNull()
  })

  it('列表真实区分 QUEUED/RUNNING，切换过滤恢复第一页，不伪造 FINAL 查询值', async () => {
    const view = workspace()
    view.state.page.value = 3
    view.state.queueStatus.value = 'RUNNING'
    view.state.taskCode.value = 'mrs_job_status'
    view.state.projectRef.value = 101
    await settle()
    const call = vi.mocked(view.api.listTaskExecutions).mock.calls.at(-1)!
    expect(call[0]).toMatchObject({ task_code: 'mrs_job_status', project_ref: 101, status: 'RUNNING', page: 1, page_size: 20 })
    view.state.mode.value = 'recent'; await settle()
    expect(vi.mocked(view.api.listTaskExecutions).mock.calls.at(-1)![0]).not.toHaveProperty('status')
  })

  it('旧目标提交的 accepted 不关闭后来打开的其他任务目标弹窗', async () => {
    const pending = deferred<ReturnType<typeof admission>>()
    const view = workspace({ enqueueTaskExecution: vi.fn(() => pending.promise) })
    view.state.chooseTargets('mrs_job_status')
    const submitting = view.state.run('mrs_job_status', [101])
    view.state.targetTaskCode.value = ''
    view.state.chooseTargets('ecs_status')
    pending.resolve(admission()); await submitting
    expect(view.state.targetTaskCode.value).toBe('ecs_status')
    expect(view.state.selectedId.value).toBe('')
  })

  it('项目筛选变化后，迟到的 accepted 不打开旧项目详情', async () => {
    const pending = deferred<ReturnType<typeof admission>>()
    const view = workspace({ enqueueTaskExecution: vi.fn(() => pending.promise) })
    view.state.projectRef.value = 101
    const submitting = view.state.run('mrs_job_status', [101])
    view.state.projectRef.value = 102
    pending.resolve(admission()); await submitting
    expect(view.state.projectRef.value).toBe(102)
    expect(view.state.selectedId.value).toBe('')
  })

  it('后端确认 FINAL 后停止单次详情轮询，列表仍能恢复其他执行', async () => {
    vi.useFakeTimers()
    const getTaskExecution = vi.fn(async (id: string) => execution({ id, execution_id: id, status: 'SUCCESS' }))
    const view = workspace({ getTaskExecution })
    view.state.open('finished')
    await settle()
    await vi.advanceTimersByTimeAsync(10000)
    expect(getTaskExecution).toHaveBeenCalledTimes(1)
    expect(vi.mocked(view.api.listTaskExecutions).mock.calls.length).toBeGreaterThan(1)
  })

  it('后端去重恢复已有执行，打开后端返回的真实 ID', async () => {
    const existing = execution()
    const view = workspace({ enqueueTaskExecution: vi.fn(async () => admission({ execution_ids: [existing.id], runs: [existing], deduplicated: true })) })
    await view.state.run('mrs_job_status')
    expect(view.state.selectedId.value).toBe(existing.id)
    expect(view.notice).toHaveBeenCalledWith('info', expect.stringContaining('后端已有活跃执行'))
  })

  it('轮询不自动重试写请求，连接失败不把 RUNNING 猜成超时或失败', async () => {
    vi.useFakeTimers()
    const listTaskExecutions = vi.fn().mockResolvedValueOnce({ items: [execution()], total: 1, page: 1, page_size: 20 }).mockRejectedValue(new Error('服务重启'))
    const view = workspace({ listTaskExecutions })
    await settle()
    await vi.advanceTimersByTimeAsync(15000)
    expect(view.state.feed.data.value?.items[0]?.status).toBe('RUNNING')
    expect(view.state.feedError.value).toContain('服务重启')
    expect(view.api.enqueueTaskExecution).not.toHaveBeenCalled()
  })
})
