import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import type { PodEventStats, PodSnapshotFreshness, PodSnapshotScope, PodState, PodTimeline } from '../src/types'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import { groupPodClusters } from '../src/views/pods/cluster-options'
import { deferred } from './businessViewHarness'
import { allNodes, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
const freshness = (status: PodSnapshotFreshness['status'], age: number | null): PodSnapshotFreshness => ({
  status, age_sec: age, stale_after_sec: 900, last_observed_at: age == null ? null : '2026-11-06T03:00:00Z',
})
const scope = (project: number, status: PodSnapshotFreshness['status'], age: number | null): PodSnapshotScope => ({
  tenant_id: 1, tenant_name: '租户甲', project_ref: project, cluster_id: 'same', cluster_name: '同名集群',
  total: 1, fresh: Number(status === 'FRESH'), stale: Number(status === 'STALE'), unknown: Number(status === 'UNKNOWN'),
  ready: Number(status === 'FRESH'), not_ready: 0, crashloop: 0, restarts: 0, freshness: freshness(status, age),
})
function summary(scopes: PodSnapshotScope[]): PodEventStats {
  return { hours: 24, total: 0, restarts: 0, ongoing: 0, critical: 0, by_type: [], by_level: [], top_workloads: [],
    trend: { labels: [], series: { restart: [], backpressure: [], other: [] }, step: 'hour' },
    pods: { total: scopes.length, fresh: scopes.reduce((n, row) => n + row.fresh, 0),
      stale: scopes.reduce((n, row) => n + row.stale, 0), unknown: scopes.reduce((n, row) => n + row.unknown, 0),
      ready: scopes.reduce((n, row) => n + row.ready, 0), not_ready: 0, crashloop: 0, restarts: 0,
      clusters: scopes.length, scopes } }
}
const dependencies = {
  '@/composables/useLatestRequest': { useLatestRequest },
  './cluster-options': { groupPodClusters },
  vue: { ...Vue, resolveDirective: (name: string) => name === 'perm' ? {} : Vue.resolveDirective(name) },
}
const Card = Vue.defineComponent({
  props: ['title', 'value', 'sub'],
  setup(props) { return () => Vue.h('StatCard', { title: props.title, value: props.value, sub: props.sub },
    `${props.title} ${props.value} ${props.sub}`) },
})
function page(stats = summary([scope(11, 'STALE', 72 * 3600)]), initialTab = 'events') {
  const reload = vi.fn<() => Promise<void>>(async () => undefined)
  const probeReload = vi.fn<() => Promise<void>>(async () => undefined)
  const mounts = vi.fn(), unmounts = vi.fn()
  const Health = Vue.defineComponent({
    setup(_props, { expose }) {
      expose({ reload })
      Vue.onMounted(() => { mounts(); void reload() })
      Vue.onUnmounted(unmounts)
      return () => Vue.h('HealthPanel', '集群健康快照')
    },
  })
  const Probe = Vue.defineComponent({
    setup(_props, { expose }) { expose({ reload: probeReload }); return () => Vue.h('ProbePanel') },
  })
  const api = {
    podEventStats: vi.fn(async () => stats), listPodEvents: vi.fn(async () => ({ items: [], total: 0 })),
    podEventOptions: vi.fn(async () => ({ clusters: [], namespaces: [], namespaces_by_cluster: {}, workloads: [] })),
    listProbes: vi.fn(async () => []), collectPodEvents: vi.fn(),
  }
  const view = mountPersonnel('pods/PodEventsView.vue', { api, dependencies: {
    ...dependencies, '@/components/StatCard.vue': { default: Card }, './CceHealthPanel.vue': { default: Health },
    './ProbePanel.vue': { default: Probe },
    'vue-router': { useRoute: () => ({ query: { tab: initialTab } }),
      useRouter: () => ({ replace: vi.fn(async () => undefined) }) },
  } })
  dispose.push(view.unmount)
  const header = () => findNode(view.root, 'Component', node => node.props.title === '容器事件')
  const tab = async (value: string) => {
    ;(findNode(view.root, 'Tabs').props['onUpdate:modelValue'] as (value: string) => void)(value)
    await settlePersonnel()
  }
  const card = (title: string) => findNode(view.root, 'StatCard', node => node.props.title === title)
  return { ...view, api, reload, probeReload, mounts, unmounts, header, tab, card }
}

function drawer(status: PodSnapshotFreshness['status'] | null, age: number | null = 901) {
  const state: PodState = { key: 'same/ns/worker-0', tenant_id: 1, tenant_name: '租户甲', project_ref: 11,
    cluster_id: 'same', cluster_name: '同名集群', namespace: 'ns', pod: 'worker-0', workload_kind: 'StatefulSet',
    workload: 'worker', node: 'node', pod_ip: '127.0.0.1', phase: 'Running', ready: false, status_reason: null,
    restarts: 7, started_at: null, labels: {}, deleted: status === 'DELETED', first_seen: '2026-11-01T03:00:00Z',
    last_seen: '2026-11-06T03:00:00Z', ...(status ? { freshness: freshness(status, age) } : {}),
    containers: [{ name: 'main', image: 'synthetic', restart_count: 7, ready: true, state: 'running', reason: null,
      started_at: null, last_reason: null, last_exit_code: null, last_started: null, last_finished: null, memory_limit: null }],
  }
  const data: PodTimeline = { state, events: [], restarts_in_window: 2 }
  const Drawer = Vue.defineComponent({
    setup(_props, { slots }) { return () => Vue.h('Drawer', [slots.header?.(), slots.default?.()]) },
  })
  const api = { podTimeline: vi.fn(async () => data) }
  const view = mountPersonnel('pods/PodTimelineDrawer.vue', { api,
    props: { modelValue: true, podKey: state.key, eventId: 17 }, dependencies: {
      ...dependencies, vue: { ...dependencies.vue,
        resolveComponent: (name: string) => name === 'el-drawer' ? Drawer : Vue.resolveComponent(name) },
    },
  })
  dispose.push(view.unmount)
  return { ...view, api }
}

describe('Pod 快照时效：实际模板', () => {
  it('72 小时前的 Ready 快照显示过期，当前就绪和 CrashLoop 显示未知', async () => {
    const view = page()
    await settlePersonnel()
    expect(view.card('Pod 就绪（当前）').props.value).toBe('未知')
    expect(view.card('CrashLoopBackOff').props.value).toBe('未知')
    expect(textOf(view.root)).toContain('过期 1 · 未知 0')
    expect(textOf(view.root)).toContain('已过期')
    expect(textOf(view.root)).toContain('最近观测 3 天 0 小时前')
    expect(textOf(view.root)).not.toContain('0/1')
  })
  it('相同集群的不同项目分别展示时效，只用有效快照作就绪分母', async () => {
    const view = page(summary([scope(11, 'FRESH', 5), scope(12, 'STALE', 3600), scope(13, 'UNKNOWN', null)]))
    await settlePersonnel()
    expect(view.card('Pod 就绪（当前）').props.value).toBe('1/1')
    expect(view.card('CrashLoopBackOff').props.value).toBe(0)
    expect(textOf(view.root)).toContain('过期 1 · 未知 1')
    const rows = allNodes(view.root).filter(node => node.props.class === 'snapshot-scope')
    expect(rows).toHaveLength(3)
    expect(textOf(rows[0])).toContain('项目 11 / 同名集群当前最近观测 5 秒前')
    expect(textOf(rows[1])).toContain('项目 12 / 同名集群已过期最近观测 1 小时前')
    expect(textOf(rows[2])).toContain('项目 13 / 同名集群未知最近观测 时间未知')
  })
  it('全未知和旧 API 缺少时效字段时都不宣称当前正常', async () => {
    const stats = summary([scope(11, 'UNKNOWN', null)])
    delete stats.pods.fresh
    const view = page(stats)
    await settlePersonnel()
    expect(view.card('Pod 就绪（当前）').props.value).toBe('未知')
    expect(view.card('CrashLoopBackOff').props.value).toBe('未知')
  })
  it.each(['STALE', 'UNKNOWN', 'DELETED', null] as const)('抽屉 %s 保留快照，当前状态标签和颜色不复用历史值', async status => {
    const view = drawer(status)
    await settlePersonnel()
    expect(textOf(view.root)).toContain('历史快照')
    expect(textOf(view.root)).toContain('历史 运行中')
    expect(textOf(view.root)).not.toContain('未就绪')
    expect(allNodes(view.root).some(node => node.props.class === 'cstate running')).toBe(false)
    expect(view.api.podTimeline).toHaveBeenCalledWith('same/ns/worker-0', 7, 17)
  })
  it('抽屉有效快照保留当前未就绪状态，并显示观测年龄', async () => {
    const view = drawer('FRESH', 30)
    await settlePersonnel()
    expect(textOf(view.root)).toContain('未就绪')
    expect(textOf(view.root)).toContain('30 秒前')
    expect(textOf(view.root)).not.toContain('历史快照')
    expect(allNodes(view.root).some(node => node.props.class === 'cstate running')).toBe(true)
  })
})

describe('集群与节点页签集成', () => {
  it('仅激活健康页签才挂载，隐藏 Pod 工具栏，统一刷新调用健康面板', async () => {
    const view = page()
    await settlePersonnel()
    expect(view.mounts).not.toHaveBeenCalled()
    expect(allNodes(view.root).some(node => node.tag === 'TabPane' && node.props.name === 'clusters'
      && node.props.label === '集群与节点')).toBe(true)
    await view.tab('clusters')
    expect(view.mounts).toHaveBeenCalledTimes(1)
    expect(view.reload).toHaveBeenCalledTimes(1)
    expect(allNodes(view.root).some(node => node.tag === 'HealthPanel')).toBe(true)
    expect(allNodes(view.root).filter(node => node.tag === 'Button').map(textOf)).not.toContain('立即采集')
    expect(allNodes(view.root).some(node => node.props['aria-label'] === '事件时间范围')).toBe(false)
    const before = view.api.podEventStats.mock.calls.length
    await (view.header().props.refresh as () => Promise<void>)()
    expect(view.reload).toHaveBeenCalledTimes(2)
    expect(view.api.podEventStats).toHaveBeenCalledTimes(before)
    expect(view.api.collectPodEvents).not.toHaveBeenCalled()
    await view.tab('events')
    expect(view.unmounts).toHaveBeenCalledTimes(1)
    expect(view.api.podEventStats).toHaveBeenCalledTimes(before + 1)
    await (view.header().props.refresh as () => Promise<void>)()
    expect(view.api.podEventStats).toHaveBeenCalledTimes(before + 2)
    expect(view.reload).toHaveBeenCalledTimes(2)
  })
  it('直接打开健康页签只使用子组件首屏加载，不重复刷新或查询 Pod 列表', async () => {
    const view = page(undefined, 'clusters')
    await settlePersonnel()
    expect(view.reload).toHaveBeenCalledTimes(1)
    expect(view.api.podEventStats).not.toHaveBeenCalled()
    expect(view.api.listPodEvents).not.toHaveBeenCalled()
    expect(view.header()).toBeTruthy()
    expect(allNodes(view.root).some(node => node.tag === 'Tabs')).toBe(true)
    expect(allNodes(view.root).some(node => node.props.class === 'hi-card')).toBe(true)
    expect(allNodes(view.root).some(node => node.tag === 'StatCard')).toBe(false)
    expect(allNodes(view.root).some(node => ['charts', 'snapshot-scopes'].includes(String(node.props.class)))).toBe(false)
    expect(textOf(view.root)).not.toContain('次数基于本系统')
    expect(textOf(view.root)).not.toContain('报表限定')
  })
  it.each(['events', 'workloads', 'probes'])('健康页签首屏后切到 %s 自动补齐统计、列表与筛选数据', async target => {
    const view = page(undefined, 'clusters')
    await settlePersonnel()
    expect(view.api.podEventStats).not.toHaveBeenCalled()
    await view.tab(target)
    expect(view.api.podEventStats).toHaveBeenCalledTimes(1)
    expect(view.api.podEventOptions).toHaveBeenCalledTimes(1)
    expect(view.api.listPodEvents).toHaveBeenCalledTimes(2) // Events and ongoing backpressure summary.
    expect(view.card('Pod 就绪（当前）').props.value).toBe('未知')
    expect(view.probeReload).toHaveBeenCalledTimes(target === 'probes' ? 1 : 0)
    expect(view.unmounts).toHaveBeenCalledTimes(1)
    expect(view.reload).toHaveBeenCalledTimes(1)
  })
  it('健康面板异步刷新及失败都会正确结束顶栏刷新状态', async () => {
    const view = page(undefined, 'clusters')
    await settlePersonnel()
    const request = deferred<void>()
    view.reload.mockImplementationOnce(() => request.promise)
    const refreshing = (view.header().props.refresh as () => Promise<void>)()
    await Vue.nextTick()
    expect(view.header().props.refreshing).toBe(true)
    request.resolve()
    await refreshing
    await settlePersonnel()
    expect(view.header().props.refreshing).toBe(false)
    view.reload.mockRejectedValueOnce(new Error('synthetic'))
    await expect((view.header().props.refresh as () => Promise<void>)()).rejects.toThrow('synthetic')
    await settlePersonnel()
    expect(view.header().props.refreshing).toBe(false)
  })
})
