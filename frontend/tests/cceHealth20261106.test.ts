import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CceList, CceSettingsOut, CceSnapshot } from '../src/types/cce'
import * as cceDisplay from '../src/views/pods/cce-display'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'

interface State {
  rows: CceSnapshot[]; detail: CceSnapshot | null; config: CceSettingsOut | null
  configTenantId: number | null; selected: string; total: number; reload: () => Promise<void>; loadDetail: (key: string) => Promise<unknown>
  openConfig: () => Promise<void>; save: () => Promise<void>; toggleTask: (enabled: boolean) => Promise<void>
  collect: () => Promise<void>; exportData: () => Promise<void>; canEditTask: boolean
  form: { enabled: boolean; resolve_owners: boolean; resource_metrics: boolean }
}
const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
const cluster = (key: string): CceSnapshot => ({
  key, tenant_id: 7, tenant_name: 'test', project_ref: 70, project_name: 'test', cluster_id: key,
  last_seen: '2026-11-06T10:00:00Z', freshness: 'fresh', missing: false, sources: {},
  cluster: { cluster_id: key, name: key, version: null, phase: null, reason: null, message: null, job_id: null,
    capacity: { total_nodes: null, active_nodes: null, total_cpu_raw: null, total_memory_raw: null, memory_unit: 'unverified' },
    endpoints: [], addons: [] }, nodes: [], nodepools: [], native: {},
})
const listing = (key: string): CceList => ({ items: [cluster(key)], total: 1, page: 1, page_size: 50 })
function view(api: Record<string, unknown>) {
  const result = mountBusinessScript<State>('pods/CceHealthPanel.vue', { api,
    dependencies: { './cce-display': cceDisplay } })
  dispose.push(result.unmount)
  return result
}

describe('CCE 集群健康范围与异步操作', () => {
  it('租户切换丢弃旧列表和旧详情', async () => {
    const first = deferred<CceList>(), second = deferred<CceList>()
    const listCceClusters = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const getCceCluster = vi.fn(async (key: string) => cluster(key))
    const v = view({ listCceClusters, getCceCluster })
    const old = v.state.reload()
    v.appState.tenantId = 8
    await settleView()
    second.resolve(listing('new'))
    await settleView(); await settleView()
    first.resolve(listing('old'))
    await old; await settleView()
    expect(v.state.rows[0].key).toBe('new')
    expect(v.state.detail?.key).toBe('new')
    expect(getCceCluster).not.toHaveBeenCalledWith('old')
  })
  it('快速切集群和清空选择不接受迟到详情', async () => {
    const first = deferred<CceSnapshot>(), second = deferred<CceSnapshot>()
    const v = view({ getCceCluster: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) })
    const old = v.state.loadDetail('a')
    const newer = v.state.loadDetail('b')
    second.resolve(cluster('b')); await newer
    first.resolve(cluster('a')); await old
    expect(v.state.detail?.key).toBe('b')
    const late = deferred<CceSnapshot>()
    const v2 = view({ getCceCluster: () => late.promise })
    const pending = v2.state.loadDetail('old')
    await v2.state.loadDetail('')
    late.resolve(cluster('old')); await pending
    expect(v2.state.detail).toBeNull()
  })
  it('只保存当前租户的配置，迟到保存不回填其他租户', async () => {
    const saved = deferred<CceSettingsOut>()
    const config: CceSettingsOut = { tenant_id: 7, settings: { enabled: false, resolve_owners: false, resource_metrics: false },
      master_enabled: false, task_enabled: false, effective_enabled: false, interval_sec: 120 }
    const saveCceSettings = vi.fn(() => saved.promise)
    const v = view({ getCceSettings: async () => config, saveCceSettings,
      listCceClusters: async () => ({ items: [], total: 0 }), getCceCluster: vi.fn() })
    await v.state.openConfig()
    v.state.form.enabled = true
    const pending = v.state.save()
    expect(saveCceSettings).toHaveBeenCalledWith(7, { enabled: true, resolve_owners: false, resource_metrics: false })
    v.appState.tenantId = 8
    await settleView()
    saved.resolve({ ...config, settings: { ...config.settings, enabled: true } })
    await pending
    expect(v.state.config).toBeNull()
    expect(v.messages.success).not.toHaveBeenCalled()
  })
  it('全部租户视图打开配置时选择一个可访问租户', async () => {
    const config: CceSettingsOut = { tenant_id: 7, settings: { enabled: false, resolve_owners: false, resource_metrics: false },
      master_enabled: false, task_enabled: false, effective_enabled: false, interval_sec: 120 }
    const getCceSettings = vi.fn(async () => config)
    const v = mountBusinessScript<State>('pods/CceHealthPanel.vue', {
      api: { getCceSettings }, app: { tenantId: null, tenants: [{ id: 7, name: '租户甲' }] },
      dependencies: { './cce-display': cceDisplay },
    })
    dispose.push(v.unmount)
    await v.state.openConfig()
    expect(v.state.configTenantId).toBe(7)
    expect(getCceSettings).toHaveBeenCalledWith(7)
  })
  it('全局任务管理员可直接启停任务，租户范围用户不可写', async () => {
    const config: CceSettingsOut = { tenant_id: 7, settings: { enabled: true, resolve_owners: false, resource_metrics: false },
      master_enabled: true, task_enabled: false, effective_enabled: false, interval_sec: 120 }
    const updateTask = vi.fn(async () => ({ enabled: true }))
    const v = view({ getCceSettings: async () => config, updateTask })
    await v.state.openConfig()
    await v.state.toggleTask(true)
    expect(updateTask).toHaveBeenCalledWith('cce_cluster_health', { enabled: true })
    expect(v.state.config?.effective_enabled).toBe(true)

    const denied = mountBusinessScript<State>('pods/CceHealthPanel.vue', {
      api: { getCceSettings: async () => config, updateTask }, dependencies: { './cce-display': cceDisplay,
        '@/stores/user': { useUserStore: () => ({ user: { tenant_ids: [7] }, can: () => true, canAny: () => true }) } },
    })
    dispose.push(denied.unmount)
    await denied.state.openConfig()
    await denied.state.toggleTask(true)
    expect(denied.state.canEditTask).toBe(false)
    expect(updateTask).toHaveBeenCalledTimes(1)
  })
  it('所有租户都关闭时按未执行提示，并打开开关状态', async () => {
    const config: CceSettingsOut = { tenant_id: 7, settings: { enabled: false, resolve_owners: false, resource_metrics: false },
      master_enabled: false, task_enabled: false, effective_enabled: false, interval_sec: 120 }
    const runTask = vi.fn(async () => { throw { code: 40901, rawMessage: '当前范围内的租户都已关闭该任务的采集' } })
    const getCceSettings = vi.fn(async () => config)
    const v = view({ runTask, getCceSettings })
    await v.state.collect()
    expect(runTask).toHaveBeenCalledWith('cce_cluster_health', { silent: true })
    expect(v.messages.info).toHaveBeenCalledWith(expect.stringContaining('未执行'))
    expect(getCceSettings).toHaveBeenCalledWith(7)
    expect(v.state.config).toEqual(config)
  })
  it('导出使用选中快照且切租户取消下载', async () => {
    const pending = deferred<string>()
    const exportCceCluster = vi.fn((_key: string, _cfg: { signal: AbortSignal }) => pending.promise)
    const v = view({ exportCceCluster, getCceCluster: async () => cluster('a'),
      listCceClusters: async () => ({ items: [], total: 0 }) })
    await v.state.loadDetail('a')
    const exporting = v.state.exportData()
    expect(exportCceCluster.mock.calls[0][0]).toBe('a')
    v.appState.tenantId = 8
    expect(exportCceCluster.mock.calls[0][1].signal.aborted).toBe(true)
    pending.reject(new Error('cancelled'))
    await exporting
  })
  it('可扩容为否不显示故障，未知和过期不显示正常', () => {
    expect(cceDisplay.cceCondition('Scalable', 'False')).toBe('允许扩容：否')
    expect(cceDisplay.cceState('stale')).toBe('已过期')
    expect(cceDisplay.cceState('unknown')).toBe('未知')
  })
})
