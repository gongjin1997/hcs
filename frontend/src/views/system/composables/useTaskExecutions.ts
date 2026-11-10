import { computed, onScopeDispose, reactive, shallowRef, watch } from 'vue'
import type * as executionApi from '../../../api/task-executions'
import type { TaskExecution, TaskExecutionAdmission, TaskExecutionContext, TaskExecutionQuery } from '../../../types/task-executions'
import { useTaskScopedResource } from './useTaskScopedResource'
import { isActiveExecution, isFinalExecution, isRetryableExecution } from './taskExecutionPresentation'

type Transport = Pick<typeof executionApi, 'listTaskExecutions' | 'getTaskExecution' | 'getTaskExecutionTargets' | 'enqueueTaskExecution' | 'cancelTaskExecution' | 'retryTaskExecution'>
interface Options {
  scopeKey: () => string
  tenantId: () => number | null
  enabled: () => boolean
  canRun: (taskCode?: string) => boolean
  makeRequestKey: () => string
  confirmCancel: (execution: TaskExecution) => Promise<boolean>
  notice: (kind: 'success' | 'warning' | 'info', message: string) => void
  changed: () => unknown
  accepted: () => void
}
const message = (error: unknown) => error instanceof Error ? error.message : '请求未确认，请重新读取后端状态'

export function useTaskExecutions(options: Options, api: Transport) {
  const mode = shallowRef<'active' | 'recent'>('active')
  const queueStatus = shallowRef<'QUEUED' | 'RUNNING'>('QUEUED')
  const page = shallowRef(1)
  const pageSize = 20
  const taskCode = shallowRef('')
  const projectRef = shallowRef<number | null>(null)
  const selectedId = shallowRef('')
  const targetTaskCode = shallowRef('')
  const detailScope = shallowRef('')
  const targetScope = shallowRef('')
  const admissionError = shallowRef('')
  const pendingTasks = reactive(new Set<string>())
  const pendingActions = reactive<Record<string, 'cancel' | 'retry' | undefined>>({})
  const idempotencyKeys = new Map<string, string>()
  const actionControllers = new Set<AbortController>()
  let targetVersion = 0
  watch(targetTaskCode, () => { ++targetVersion }, { flush: 'sync' })
  let version = 0
  let disposed = false

  const feedKey = computed(() => options.enabled() ? JSON.stringify({ scope: options.scopeKey(), tenantId: options.tenantId(),
    task_code: taskCode.value || undefined, project_ref: projectRef.value ?? undefined,
    status: mode.value === 'active' ? queueStatus.value : undefined, page: page.value, page_size: pageSize }) : null)
  const feed = useTaskScopedResource(feedKey, (key, signal) => {
    const { scope: _scope, tenantId, ...query } = JSON.parse(key) as TaskExecutionQuery & { scope: string; tenantId: number | null }
    return api.listTaskExecutions(query, { tenantId, signal })
  }, 2500)
  const detailKey = computed(() => selectedId.value && detailScope.value === options.scopeKey() ? JSON.stringify([options.scopeKey(), options.tenantId(), selectedId.value]) : null)
  const detail = useTaskScopedResource(detailKey, (key, signal) => {
    const [, tenantId, id] = JSON.parse(key) as [string, number | null, string]
    return api.getTaskExecution(id, { tenantId, signal })
  }, 2000, value => !value || !isFinalExecution(value.status))
  const targetKey = computed(() => {
    const code = targetTaskCode.value && targetScope.value === options.scopeKey() ? targetTaskCode.value : options.enabled() ? taskCode.value : ''
    return code ? JSON.stringify([options.scopeKey(), options.tenantId(), code]) : null
  })
  const targets = useTaskScopedResource(targetKey, (key, signal) => {
    const [, tenantId, code] = JSON.parse(key) as [string, number | null, string]
    return api.getTaskExecutionTargets(code, { tenantId, signal })
  }, 5000)
  const feedError = computed(() => feed.error.value ? `执行记录读取失败：${message(feed.error.value)}` : '')
  const detailError = computed(() => detail.error.value ? `执行详情读取失败：${message(detail.error.value)}` : '')
  const targetError = computed(() => targets.error.value ? `执行目标读取失败：${message(targets.error.value)}` : admissionError.value)

  function invalidateActions() {
    ++version
    actionControllers.forEach(controller => controller.abort())
    actionControllers.clear()
    pendingTasks.clear()
    Object.keys(pendingActions).forEach(key => { delete pendingActions[key] })
    idempotencyKeys.clear()
    admissionError.value = ''
  }
  watch(options.scopeKey, () => {
    invalidateActions()
    selectedId.value = ''
    targetTaskCode.value = ''
    projectRef.value = null
    page.value = 1
  }, { flush: 'sync' })
  watch([mode, queueStatus, taskCode, projectRef], () => { page.value = 1 }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; invalidateActions() })

  function actionContext() {
    const requestVersion = version
    const scope = options.scopeKey()
    const controller = new AbortController()
    actionControllers.add(controller)
    return { context: { tenantId: options.tenantId(), signal: controller.signal } satisfies TaskExecutionContext,
      current: () => !disposed && requestVersion === version && scope === options.scopeKey() && !controller.signal.aborted,
      finish: () => { actionControllers.delete(controller) } }
  }
  function refreshAfterAction() {
    void feed.refresh()
    void targets.refresh()
    void detail.refresh()
    void options.changed()
  }
  function captureFocus() {
    const key = () => JSON.stringify([targetVersion, taskCode.value, projectRef.value, selectedId.value, mode.value, queueStatus.value, page.value, options.enabled()])
    const captured = key()
    return () => captured === key()
  }
  function accept(result: TaskExecutionAdmission, allowDetails = true) {
    if (result.execution_ids.length) {
      mode.value = 'recent'
      queueStatus.value = result.runs.some(run => run.status === 'QUEUED') ? 'QUEUED' : 'RUNNING'
      page.value = 1
      options.accepted()
      const first = result.runs[0]
      const matchesFilters = (!taskCode.value || first?.task_code === taskCode.value) && (projectRef.value === null || first?.project_ref === projectRef.value)
      if (allowDetails && matchesFilters && result.execution_ids.length === 1) open(result.execution_ids[0]!)
      const active = result.runs.some(run => isActiveExecution(run.status))
      options.notice(result.deduplicated ? 'info' : 'success', result.deduplicated
        ? active ? '后端已有活跃执行，未重复创建。' : '已恢复后端已有执行记录，未重复创建。'
        : `后台已接受 ${result.execution_ids.length} 条执行，可离开页面后回来查看。`)
    } else options.notice('warning', '本次未创建执行，请查看跳过原因。')
    if (result.skipped.length) options.notice('info', result.skipped.join('；'))
    refreshAfterAction()
  }

  async function run(code: string, projectRefs?: number[]) {
    if (disposed || !options.canRun(code) || pendingTasks.has(code)) return
    const action = actionContext()
    const focused = captureFocus()
    const targetAtStart = targetTaskCode.value
    const targetVersionAtStart = targetVersion
    const refs = projectRefs ? [...new Set(projectRefs)].sort((a, b) => a - b) : undefined
    const key = JSON.stringify([options.scopeKey(), code, refs])
    const idempotency = idempotencyKeys.get(key) ?? options.makeRequestKey()
    idempotencyKeys.set(key, idempotency)
    pendingTasks.add(code)
    admissionError.value = ''
    try {
      const result = await api.enqueueTaskExecution(code, { ...(refs ? { project_refs: refs } : {}), idempotency_key: idempotency }, action.context)
      if (!action.current()) return
      idempotencyKeys.delete(key)
      const allowDetails = focused()
      if (targetVersionAtStart === targetVersion && targetAtStart === code) targetTaskCode.value = ''
      accept(result, allowDetails)
      return result
    } catch (error) {
      if (action.current()) {
        const errorMessage = `提交未确认：${message(error)}。请查看已有执行后再操作。`
        if (targetVersionAtStart === targetVersion && targetAtStart === targetTaskCode.value) admissionError.value = errorMessage
        options.notice('warning', errorMessage)
        void feed.refresh()
      }
    } finally {
      if (action.current()) pendingTasks.delete(code)
      action.finish()
    }
  }

  async function cancel(execution: TaskExecution) {
    if (disposed || !options.canRun(execution.task_code) || !isActiveExecution(execution.status) || execution.cancel_requested || pendingActions[execution.execution_id]) return
    const action = actionContext()
    pendingActions[execution.execution_id] = 'cancel'
    try {
      if (!await options.confirmCancel(execution) || !action.current() || !options.canRun(execution.task_code)) return
      const result = await api.cancelTaskExecution(execution.execution_id, action.context)
      if (!action.current()) return
      feed.updateFromServer(previous => previous ? { ...previous, items: previous.items.map(item => item.execution_id === result.execution_id ? result : item) } : previous)
      detail.updateFromServer(previous => previous?.execution_id === result.execution_id ? result : previous)
      options.notice('info', isActiveExecution(result.status) ? '已请求停止本地巡检，等待后台确认；云端作业不受影响。' : `后台状态已更新为 ${result.status}；云端作业不受影响。`)
      refreshAfterAction()
    } catch (error) {
      if (action.current()) options.notice('warning', `停止请求未确认：${message(error)}`)
    } finally {
      if (action.current()) delete pendingActions[execution.execution_id]
      action.finish()
    }
  }

  async function retry(execution: TaskExecution) {
    if (disposed || !options.canRun(execution.task_code) || !isRetryableExecution(execution.status) || pendingActions[execution.execution_id]) return
    const action = actionContext()
    pendingActions[execution.execution_id] = 'retry'
    const focused = captureFocus()
    try {
      const result = await api.retryTaskExecution(execution.execution_id, action.context)
      if (!action.current()) return
      accept(result, focused())
      return result
    } catch (error) {
      if (action.current()) options.notice('warning', `重试请求未确认：${message(error)}。请先查看后台已有执行。`)
    } finally {
      if (action.current()) delete pendingActions[execution.execution_id]
      action.finish()
    }
  }
  function open(id: string) { detailScope.value = options.scopeKey(); selectedId.value = id }
  function chooseTargets(code: string) { if (options.canRun(code)) { admissionError.value = ''; targetScope.value = options.scopeKey(); targetTaskCode.value = code } }
  return { mode, queueStatus, page, pageSize, taskCode, projectRef, selectedId, targetTaskCode, pendingTasks, pendingActions,
    feed, detail, targets, feedError, detailError, targetError, run, cancel, retry, open, chooseTargets }
}
