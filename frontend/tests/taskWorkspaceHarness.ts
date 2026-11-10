import { vi } from 'vitest'
import { useTaskExecutions } from '../src/views/system/composables/useTaskExecutions'
import { taskParameterErrors, taskParameterUpdate } from '../src/views/system/composables/taskParameters'
import * as taskExecutionPresentation from '../src/views/system/composables/taskExecutionPresentation'
import { newRequestId } from '../src/utils/request-id'

/** Bridge the task view's focused transport while retaining existing configuration fixtures. */
export function taskWorkspaceDependencies(api: Record<string, unknown> = {}) {
  const fallback = (name: string, result: unknown) => api[name] ?? vi.fn(async () => result)
  const transport = {
    listTaskDefinitions: api.listTaskDefinitions ?? (() => (api.listTasks as (() => Promise<unknown>))?.() ?? Promise.resolve([])),
    updateTaskDefinition: api.updateTaskDefinition ?? ((code: string, body: object) => (api.updateTask as ((code: string, body: object) => Promise<unknown>))?.(code, body)),
    enqueueTaskExecution: api.enqueueTaskExecution ?? api.runTask ?? vi.fn(),
    listTaskExecutions: fallback('listTaskExecutions', { items: [], total: 0, page: 1, page_size: 20 }),
    getTaskExecution: fallback('getTaskExecution', null),
    getTaskExecutionTargets: fallback('getTaskExecutionTargets', { task_code: '', task_enabled: true, targets: [] }),
    cancelTaskExecution: fallback('cancelTaskExecution', null),
    retryTaskExecution: fallback('retryTaskExecution', null),
  }
  return {
    '@/api/task-executions': transport,
    './composables/useTaskExecutions': { useTaskExecutions },
    './composables/taskParameters': { taskParameterErrors, taskParameterUpdate },
    './composables/taskExecutionPresentation': taskExecutionPresentation,
    '@/utils/request-id': { newRequestId },
  }
}
