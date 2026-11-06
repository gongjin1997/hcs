import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PodEvent, PodTimeline } from '../src/types'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import { groupPodClusters, podNamespaces } from '../src/views/pods/cluster-options'
import type { PodEventOptions } from '../src/types'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
interface DrawerState { data: PodTimeline | null; load: () => Promise<boolean>; loading: boolean }
function drawer() {
  const pending: ReturnType<typeof deferred<PodTimeline>>[] = []
  const podTimeline = vi.fn(() => { const request = deferred<PodTimeline>(); pending.push(request); return request.promise })
  const view = mountBusinessScript<DrawerState>('pods/PodTimelineDrawer.vue', {
    props: { modelValue: true, podKey: 'same-cluster/default/same-pod', eventId: 11 }, api: { podTimeline },
  })
  dispose.push(view.unmount)
  return { ...view, pending, podTimeline }
}
const timeline = (n: number): PodTimeline => ({ state: null, events: [], restarts_in_window: n })
describe('容器事件精确身份', () => {
  it('时间线携带本地事件ID，不能仅凭同名集群与Pod定位', () => {
    const view = drawer()
    expect(view.podTimeline).toHaveBeenCalledWith('same-cluster/default/same-pod', 7, 11)
  })
  it('同key换到另一事件时重新加载并忽略旧响应', async () => {
    const view = drawer()
    view.props.eventId = 22
    await settleView()
    expect(view.podTimeline).toHaveBeenLastCalledWith('same-cluster/default/same-pod', 7, 22)
    view.pending[1].resolve(timeline(2)); await settleView()
    view.pending[0].resolve(timeline(9)); await settleView()
    expect(view.state.data?.restarts_in_window).toBe(2)
  })
  it('关闭抽屉后迟到的事件不回填', async () => {
    const view = drawer()
    view.props.modelValue = false
    await settleView()
    view.pending[0].resolve(timeline(9)); await settleView()
    expect(view.state.data).toBeNull()
  })
  it('列表行将正确事件ID传到抽屉，节点级事件不改变已有选择', () => {
    const view = mountBusinessScript<{ openPod: (event: PodEvent) => void; drawer: { eventId?: number; key: string; visible: boolean } }>('pods/PodEventsView.vue')
    dispose.push(view.unmount)
    view.state.openPod({ id: 17, cluster_id: 'cluster', namespace: 'default', pod: 'pod', cluster_name: 'cluster' } as PodEvent)
    expect(view.state.drawer).toMatchObject({ eventId: 17, key: 'cluster/default/pod', visible: true })
    view.state.openPod({ id: 18, pod: '' } as PodEvent)
    expect(view.state.drawer.eventId).toBe(17)
  })
})

describe('同ID集群筛选元数据', () => {
  const cluster = (tenant: number, project: number, namespaces = ['default']) => ({
    tenant_id: tenant, project_ref: project, cluster_id: 'shared-id', cluster_name: 'cce', tenant_name: `租户${tenant}`, namespaces,
  })
  const options = (clusters: PodEventOptions['clusters']): PodEventOptions => ({
    clusters, namespaces: ['a', 'b'], namespaces_by_cluster: { 'shared-id': ['a', 'b'] }, workloads: [], event_types: [], levels: [], sources: [],
  })
  it('同ID跨tenant不会产生重复的select值，标签说明两个tenant', () => {
    const result = groupPodClusters([cluster(1, 11), cluster(2, 22)])
    expect(result).toHaveLength(1)
    expect(result[0].scopeCount).toBe(2)
    expect(result[0].cluster_name).toContain('租户1、租户2')
  })
  it('同tenant的同ID项目合并显示，完全重复元数据不重复计数', () => {
    const result = groupPodClusters([cluster(1, 11), cluster(1, 11), cluster(1, 12)])
    expect(result[0].cluster_name).toBe('cce（2 个项目）')
  })
  it('探针只列自身tenant命名空间，包括没有workload的裸Pod', () => {
    const data = options([cluster(1, 11, ['a', 'standalone']), cluster(2, 22, ['b'])])
    expect(podNamespaces(data, 1, 'shared-id')).toEqual(['a', 'standalone'])
    expect(podNamespaces(data, 2, '*')).toEqual(['b'])
    expect(podNamespaces(data, 3, '*')).toEqual([])
  })
  it('兼容旧API时仅从当前tenant集群聚合namespace字典', () => {
    const { namespaces: _namespaces, ...old } = cluster(1, 11)
    expect(podNamespaces(options([old]), 1, '*')).toEqual(['a', 'b'])
    expect(podNamespaces(options([old]), 2, '*')).toEqual([])
  })
})
