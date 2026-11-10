import { get, post, put } from './request'
import { scopeHeaders } from './scope'
import type { Page } from '../types'
import type { TaskDefinition, TaskDefinitionPatch, TaskExecution, TaskExecutionAdmission, TaskExecutionContext, TaskExecutionQuery, TaskExecutionRequest, TaskExecutionTargets } from '../types/task-executions'

const context = ({ tenantId, signal }: TaskExecutionContext) => ({ headers: scopeHeaders(tenantId), signal, silent: true })
const taskPath = (code: string) => `/system/tasks/${encodeURIComponent(code)}`
const executionPath = (id: string) => `/system/executions/${encodeURIComponent(id)}`

export const listTaskDefinitions = (scope: TaskExecutionContext) => get<TaskDefinition[]>('/system/tasks', undefined, context(scope))
export const updateTaskDefinition = (code: string, body: TaskDefinitionPatch, scope: TaskExecutionContext) =>
  put<{ code: string; changed: boolean }>(taskPath(code), body, context(scope))
/** Only database admission: use the normal short request timeout, never the old run-all timeout. */
export const enqueueTaskExecution = (code: string, body: TaskExecutionRequest | undefined, scope: TaskExecutionContext) =>
  post<TaskExecutionAdmission>(`${taskPath(code)}/run`, body, context(scope))
export const listTaskExecutions = (query: TaskExecutionQuery, scope: TaskExecutionContext) =>
  get<Page<TaskExecution>>('/system/executions', query, context(scope))
export const getTaskExecution = (id: string, scope: TaskExecutionContext) => get<TaskExecution>(executionPath(id), undefined, context(scope))
export const getTaskExecutionTargets = (code: string, scope: TaskExecutionContext) => get<TaskExecutionTargets>(`${taskPath(code)}/targets`, undefined, context(scope))
export const cancelTaskExecution = (id: string, scope: TaskExecutionContext) => post<TaskExecution>(`${executionPath(id)}/cancel`, undefined, context(scope))
export const retryTaskExecution = (id: string, scope: TaskExecutionContext) => post<TaskExecutionAdmission>(`${executionPath(id)}/retry`, undefined, context(scope))
