import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TaskDefinition, TaskExecution, TaskExecutionTargets, TaskExecutionRun, TaskParameterSchema, TaskParameterValue } from '../src/types/task-executions'
import { executionCount, executionPercentage, executionStatus, isFinalExecution } from '../src/views/system/composables/taskExecutionPresentation'
import { taskParameterErrors, taskParameterUpdate } from '../src/views/system/composables/taskParameters'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'
import { execution } from './taskExecutionFixtures'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
function card(item: TaskExecution = execution(), props: Record<string, unknown> = {}) {
  const view = mountPersonnel('system/TaskExecutionCard.vue', { props: { execution: item, canCancel: true, canRetry: true, ...props } })
  dispose.push(view.unmount)
  return view
}
const task: TaskDefinition = { code: 'mrs_job_status', name: 'MRS 巡检', service: 'mrs', interval_sec: 120, cron: null, enabled: true, params: {}, runs: [],
  next_run: null, next_run_source: 'scheduler', types: ['mrs_job'], note: null, min_interval: 30, skipped_tenants: [] }

describe('执行实例的真实展示与操作', () => {
  it('展示阶段、进度、计数、心跳和后端耗时；运行百分比到100也不判成功', () => {
    const view = card(execution({ current: 8, total: 8 }))
    for (const value of ['巡检中', 'RUNNING', '采集', '8 / 8', 'API 次数', '9', '4 秒', '心跳']) expect(textOf(view.root)).toContain(value)
    expect(findNode(view.root, 'Progress').props.percentage).toBe(100)
    expect(textOf(view.root)).not.toContain('SUCCESS')
  })

  it('旧心跳和长耗时不在前端推断超时，最终原因取自后端新状态', async () => {
    const view = card(execution({ heartbeat_at: '2020-01-01T00:00:00Z', elapsed_sec: 99000 }))
    expect(textOf(view.root)).toContain('RUNNING')
    expect(textOf(view.root)).not.toContain('TIMEOUT')
    view.updateProps({ execution: execution({ status: 'INTERRUPTED', error_summary: '执行租约已失效', finished_at: '2026-11-10T01:10:00Z' }) })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('INTERRUPTED')
    expect(textOf(view.root)).toContain('执行租约已失效')
    expect(textOf(view.root)).toContain('新建重试')
    expect(textOf(view.root)).not.toContain('仅停止本地巡检')
  })

  it('停止与重试事件独立，只有后台活跃状态允许停止', async () => {
    const onCancel = vi.fn(), onRetry = vi.fn()
    const view = card(execution(), { onCancel, onRetry })
    await clickButton(view.root, '仅停止本地巡检')
    expect(onCancel).toHaveBeenCalledOnce()
    view.updateProps({ execution: execution({ status: 'FAILED' }) }); await settlePersonnel()
    await clickButton(view.root, '新建重试')
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('后端已接受停止时按钮禁用；只读用户只保留执行详情', () => {
    const stopping = card(execution({ cancel_requested: true }))
    expect(findNode(stopping.root, 'Button', node => textOf(node) === '仅停止本地巡检').props.disabled).toBe(true)
    expect(textOf(stopping.root)).toContain('等待后台确认')
    const readonly = card(execution(), { canCancel: false, canRetry: false })
    expect(allNodes(readonly.root).filter(node => node.tag === 'Button').map(textOf)).toEqual(['执行详情'])
  })

  it('未知/缺失数量不冒充零，未知状态不归为 FINAL', () => {
    expect(executionPercentage({ current: null, total: 20 })).toBeNull()
    expect(executionPercentage({ current: 0, total: 0 })).toBeNull()
    expect(executionCount(undefined)).toBe('—')
    expect(executionCount(0)).toBe('0')
    expect(executionStatus('constructor')).toEqual({ label: 'constructor', type: 'info' })
    expect(isFinalExecution('FUTURE_STATUS')).toBe(false)
  })

  it('同一 request_id 的多项目执行归组，保持独立 execution ID', () => {
    const items = [execution(), execution({ id: 'b', execution_id: 'b', project_ref: 102 }), execution({ id: 'c', execution_id: 'c', request_id: 'another-request' })]
    const view = mountPersonnel('system/TaskExecutionList.vue', { props: { items, mode: 'recent', queueStatus: 'QUEUED', total: 3, page: 1, pageSize: 20,
      loading: false, error: '', canCancel: false, canRetry: false, pendingActions: {} } })
    dispose.push(view.unmount)
    expect(allNodes(view.root).filter(node => node.props['aria-label'] === '一次请求的执行批次')).toHaveLength(2)
    expect(allNodes(view.root).filter(node => node.tag === 'Component' && node.props.execution).map(node => (node.props.execution as TaskExecution).execution_id)).toEqual(['execution-old', 'b', 'c'])
  })

  it('任务列表中的排队运行接受 started_at=null，不伪造开始时间或耗时', () => {
    const queued: TaskExecutionRun = { ...execution({ status: 'QUEUED', started_at: null, finished_at: null }), id: 11, stats: {} }
    const view = mountPersonnel('system/TaskRunHistory.vue', { props: { task: { ...task, runs: [queued] }, describeRun: () => '',
      status: () => ({ label: '排队中' }), projectLabel: () => '项目 A' } })
    dispose.push(view.unmount)
    expect(textOf(view.root)).toContain('排队中')
    expect(textOf(view.root)).toContain('尚未开始')
    expect(textOf(view.root)).not.toContain('NaN')
    expect(queued.started_at).toBeNull()
  })

  it('历史运行保留数值 run ID，详情入口使用新增 execution_id', async () => {
    const item = execution()
    const run: TaskExecutionRun = { id: item.run_id, execution_id: item.execution_id, run_id: item.run_id,
      tenant_id: item.tenant_id, tenant_name: item.tenant_name, project_ref: item.project_ref, trigger_type: item.trigger_type,
      started_at: item.started_at!, finished_at: null, status: item.status, resource_total: item.resource_total,
      abnormal_total: item.abnormal_total, api_calls: item.api_calls, error_summary: null, stats: {}, phase: item.phase, heartbeat_at: item.heartbeat_at }
    const onOpenExecution = vi.fn()
    const view = mountPersonnel('system/TaskRunHistory.vue', { props: { task: { ...task, runs: [run] }, describeRun: () => '',
      status: () => ({ label: '巡检中' }), projectLabel: () => item.project_label, onOpenExecution } })
    dispose.push(view.unmount)
    await clickButton(view.root, '打开单次执行')
    expect(onOpenExecution).toHaveBeenCalledWith(item.execution_id)
    expect(run.id).toBe(item.run_id)
    expect(typeof run.id).toBe('number')
  })

  it('执行详情显示标识、后台时间与真实阶段记录', () => {
    const item = execution({ history: [{ phase: 'AUTH', phase_label: '认证', at: '2026-11-10T01:00:01Z', message: '认证完成' }, { phase: 'COLLECT', phase_label: '采集', current: 2, total: 8 }] })
    const view = mountPersonnel('system/TaskExecutionDrawer.vue', { props: { modelValue: true, execution: item, loading: false, error: '', canCancel: false, canRetry: false } })
    dispose.push(view.unmount)
    for (const label of ['execution-old', 'request-batch-1', '认证完成', '2 / 8', '300 秒', '云端作业']) expect(textOf(view.root)).toContain(label)
  })
})

describe('按项目执行的真实目标选择', () => {
  const targetData: TaskExecutionTargets = { task_code: task.code, task_enabled: true, targets: [
    { tenant_id: 7, tenant_name: '租户甲', project_ref: 101, project_label: '项目 A', enabled: true, executable: true, reason: null, active_execution_id: null },
    { tenant_id: 8, tenant_name: '租户乙', project_ref: 102, project_label: '项目 B', enabled: true, executable: false, reason: '已有排队或执行中的巡检', active_execution_id: 'already-running' },
    { tenant_id: 8, tenant_name: '租户乙', project_ref: 103, project_label: '项目 C', enabled: true, executable: true, reason: null, active_execution_id: null },
  ] }
  it('只提交选中的可执行项目，已有活跃执行提供打开入口', async () => {
    const onSubmit = vi.fn(), onExisting = vi.fn()
    const view = mountPersonnel('system/TaskRunTargetDialog.vue', { props: { modelValue: true, task, targets: targetData, loading: false, submitting: false, error: '', canRun: true, onSubmit, onExisting } })
    dispose.push(view.unmount)
    await clickButton(view.root, '加入后台队列')
    expect(onSubmit).toHaveBeenCalledWith([101, 103])
    await clickButton(view.root, '打开已有活跃执行')
    expect(onExisting).toHaveBeenCalledWith('already-running')
  })

  it('目标轮询刷新保留用户勾选，不把已排除项目重新加入提交', async () => {
    const onSubmit = vi.fn()
    const view = mountPersonnel('system/TaskRunTargetDialog.vue', { props: { modelValue: true, task, targets: targetData, loading: false, submitting: false, error: '', canRun: true, onSubmit } })
    dispose.push(view.unmount)
    const group = findNode(view.root, 'CheckboxGroup')
    ;(group.props['onUpdate:modelValue'] as (value: string[]) => void)(['103'])
    view.updateProps({ targets: structuredClone(targetData) }); await settlePersonnel()
    await clickButton(view.root, '加入后台队列')
    expect(onSubmit).toHaveBeenCalledWith([103])
  })

  it('只读目标弹窗不会产生提交事件', () => {
    const view = mountPersonnel('system/TaskRunTargetDialog.vue', { props: { modelValue: true, task, targets: targetData, loading: false, submitting: false, error: '', canRun: false } })
    dispose.push(view.unmount)
    expect(allNodes(view.root).some(node => node.tag === 'Button' && textOf(node) === '加入后台队列')).toBe(false)
    expect(findNode(view.root, 'CheckboxGroup').props.disabled).toBe(true)
  })
})

describe('后端参数 schema 驱动的 MRS 表单', () => {
  const schema: TaskParameterSchema = { properties: {
    query_mode: { type: 'string', title: '查询模式', enum: ['active_and_recent', 'active_only', 'recent_only', 'all_history'] },
    timeout_sec: { type: 'integer', title: '整轮超时', minimum: 15, maximum: 3600 },
    max_calls: { type: 'integer', title: '整轮调用预算', minimum: 1, maximum: 400 },
    submitted_window_hours: { type: 'number', title: '近期提交窗口', default: 24, exclusiveMinimum: 0, maximum: 744 },
    submitted_time_begin: { type: 'integer', title: '窗口起点', minimum: 100000000000 },
    submitted_time_end: { type: 'integer', title: '窗口终点', minimum: 100000000000 },
    elapsed_time_unit: { type: ['string', 'null'], title: '局点耗时单位', enum: [null, 'milliseconds', 'seconds'] },
  } }
  it('允许范围来自 schema，整数/有限数与显式窗口均校验', () => {
    expect(taskParameterErrors({ max_calls: 0 }, schema)).toContain('整轮调用预算超出后端允许范围')
    expect(taskParameterErrors({ max_calls: 1.5 }, schema)).toContain('整轮调用预算的类型不正确')
    expect(taskParameterErrors({ submitted_window_hours: Infinity }, schema)).not.toEqual([])
    expect(taskParameterErrors({ submitted_window_hours: 0 }, schema)).not.toEqual([])
    expect(taskParameterErrors({ submitted_time_end: 100000000001 }, schema)).toContain('显式时间窗需要填写起点')
    expect(taskParameterErrors({ secret: '不能编辑' }, schema)).toContain('参数 secret 未开放编辑')
  })

  it('活动模式不提交时间窗，显式窗口与小时窗口互斥', () => {
    const fields = schema.properties!
    expect(taskParameterUpdate({ query_mode: 'active_and_recent', submitted_window_hours: 24 }, 'query_mode', 'active_only', fields)).toEqual({ query_mode: 'active_only' })
    expect(taskParameterUpdate({ query_mode: 'recent_only', submitted_window_hours: 24 }, 'submitted_time_begin', 100000000001, fields)).not.toHaveProperty('submitted_window_hours')
    expect(taskParameterUpdate({ query_mode: 'recent_only', submitted_time_begin: 100000000001 }, 'submitted_window_hours', 48, fields)).not.toHaveProperty('submitted_time_begin')
  })

  it('真实表单呈现可编辑预算与单位，不渲染任意 params 字段', async () => {
    const updated = vi.fn()
    const values: Record<string, TaskParameterValue> = { query_mode: 'active_and_recent', timeout_sec: 300, max_calls: 30, submitted_window_hours: 24, elapsed_time_unit: 'seconds' }
    const view = mountPersonnel('system/TaskParameterFields.vue', { props: { schema, modelValue: values, disabled: false, 'onUpdate:modelValue': updated } })
    dispose.push(view.unmount)
    expect(findNode(view.root, 'InputNumber', node => node.props['aria-label'] === '整轮调用预算').props).toMatchObject({ min: 1, max: 400, precision: 0 })
    const mode = findNode(view.root, 'Select', node => node.props['aria-label'] === '查询模式')
    ;(mode.props['onUpdate:modelValue'] as (value: string) => void)('active_only')
    expect(updated).toHaveBeenLastCalledWith(expect.not.objectContaining({ submitted_window_hours: expect.anything() }))
    const unit = findNode(view.root, 'Select', node => node.props['aria-label'] === '局点耗时单位')
    ;(unit.props['onUpdate:modelValue'] as (value: string) => void)('')
    expect(updated).toHaveBeenLastCalledWith(expect.objectContaining({ elapsed_time_unit: null }))
    await settlePersonnel()
  })
})
