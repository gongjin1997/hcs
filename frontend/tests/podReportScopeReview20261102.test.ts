import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import type { PodEventOptions } from '../src/types'

interface State {
  query: { tenant_id?: number; project_ref?: number; unknown_project: boolean; workload_kind: string; cluster_id: string }
  scopeError: string
  loadEvents: () => Promise<unknown>; loadStats: () => Promise<unknown>; loadPods: () => Promise<unknown>
  onCluster: () => void
  rows: { id: number }[]
}
const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
const emptyOptions: PodEventOptions = { clusters: [], namespaces: [], namespaces_by_cluster: {}, workloads: [],
  event_types: [], levels: [], sources: [] }
function view(query: Record<string, string> = {}) {
  const route = reactive({ query: { ...query } })
  const api = {
    listPodEvents: vi.fn(async (_params?: Record<string, unknown>) => ({ items: [] as { id: number }[], total: 0 })),
    podEventStats: vi.fn(async (_params?: Record<string, unknown>) => ({ by_type: [], pods: {}, top_workloads: [] })),
    podEventOptions: vi.fn(async (_params?: Record<string, unknown>) => emptyOptions), listProbes: vi.fn(async () => []),
  }
  const replace = vi.fn(async (_next: { query: Record<string, string | undefined> }) => undefined)
  const result = mountBusinessScript<State>('pods/PodEventsView.vue', {
    app: { tenantId: null }, api,
    dependencies: { 'vue-router': { useRoute: () => route, useRouter: () => ({ replace }) } },
  })
  dispose.push(result.unmount)
  return { ...result, api, route, replace }
}
describe('报表跳转容器事件的精确范围', () => {
  it('事件、统计、Pod元数据均转发租户/项目，统计与事件支持kind精确匹配', async () => {
    const result = view({ tenant_id: '1', project_ref: '101', cluster_id: 'same', namespace: 'ns',
      workload: 'worker', workload_kind: 'Job' })
    await Promise.all([result.state.loadEvents(), result.state.loadStats(), result.state.loadPods()])
    expect(result.api.listPodEvents.mock.calls.every(([params]) => params?.tenant_id === 1 && params?.project_ref === 101)).toBe(true)
    expect(result.api.podEventStats).toHaveBeenCalledWith(expect.objectContaining({ tenant_id: 1, project_ref: 101,
      workload_kind: 'Job', workload: 'worker' }))
    expect(result.api.podEventOptions).toHaveBeenCalledWith(expect.objectContaining({ tenant_id: 1, project_ref: 101 }))
  })
  it('更换集群清除旧报表项目与租户限定并清理URL，不能在新集群携带旧项目', () => {
    const result = view({ tenant_id: '1', project_ref: '101', workload_kind: 'Job' })
    result.state.query.cluster_id = 'new-cluster'
    result.state.onCluster()
    expect(result.state.query.tenant_id).toBeUndefined()
    expect(result.state.query.project_ref).toBeUndefined()
    expect(result.state.query.workload_kind).toBe('')
    expect(result.replace).toHaveBeenCalled()
  })
  it('顶栏租户变化时清理旧URL限定、旧数据和迟到响应', async () => {
    const result = view({ tenant_id: '1', project_ref: '101' })
    const request = deferred<{ items: { id: number }[]; total: number }>()
    result.api.listPodEvents.mockImplementationOnce(() => request.promise)
    const pending = result.state.loadEvents()
    result.appState.tenantId = 2
    await settleView()
    request.resolve({ items: [{ id: 999 }], total: 1 })
    await pending
    expect(result.state.query.tenant_id).toBeUndefined()
    expect(result.state.query.project_ref).toBeUndefined()
    expect(result.state.rows).not.toEqual([{ id: 999 }])
  })
  it('非法项目URL不能静默退回全部项目查询', async () => {
    const result = view({ tenant_id: '1', project_ref: '101oops' })
    await result.state.loadEvents()
    expect(result.state.scopeError).toBeTruthy()
    expect(result.api.listPodEvents).not.toHaveBeenCalled()
  })
  it('历史未知项目必须转发显式unknown_project', async () => {
    const result = view({ tenant_id: '1', unknown_project: '1' })
    await result.state.loadEvents()
    expect(result.api.listPodEvents).toHaveBeenCalledWith(expect.objectContaining({ tenant_id: 1, unknown_project: true }))
  })
})
