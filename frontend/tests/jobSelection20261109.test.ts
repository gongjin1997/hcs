import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobDiscovery, JobSelection, JobSelectionMode, JobSelectionTarget } from '../src/api/job-selection'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'

interface State {
  tenantId: number | null; projectRef: number | null; workspace: string | null; mode: JobSelectionMode
  selected: string[]; ready: boolean; rows: JobDiscovery['items']; listing: boolean
  page: number; error: string; listError: string; keyword: string
  loadJobs: (page?: number) => Promise<void>; toggleJob: (name: string, checked: boolean) => void
  save: () => Promise<void>
}
const targets: JobSelectionTarget[] = [{ id: 11, name: '租户 11', enabled: true, projects: [
  { id: 101, project_id: 'p-101', name: '项目 A', enabled: true, workspaces: ['ws-a', 'ws-b'] },
  { id: 102, project_id: 'p-102', name: '项目 B', enabled: true, workspaces: ['ws-a'] },
] }]
const configuration = (project = 101, workspace = 'ws-a'): JobSelection => ({
  tenant_id: 11, resource_type: 'dgc_realtime_job', project_ref: project, workspace,
  mode: 'inherit', jobs: [], configured: false,
})
const page = (number: number, names = [`job-${number}`]): JobDiscovery => ({
  items: names.map(name => ({ name, job_type: 'REAL_TIME', status: 'NORMAL' })),
  total: 251, page: number, page_size: 100,
})
const disposers: (() => void)[] = []
afterEach(() => disposers.splice(0).forEach(dispose => dispose()))
async function dialog(overrides: Record<string, unknown> = {}) {
  const api = {
    getJobSelectionTargets: vi.fn(async () => targets),
    getJobSelection: vi.fn(async (_tenant: number, _type: string, project: number, workspace: string) => configuration(project, workspace)),
    discoverSelectionJobs: vi.fn(async (_tenant: number, _type: string, query: { page: number }) => page(query.page)),
    saveJobSelection: vi.fn(async () => ({ changed: true, closed_alarms: 0 })),
    ...overrides,
  }
  const saved = vi.fn(), updateVisible = vi.fn()
  const view = mountBusinessScript<State>('system/JobSelectionDialog.vue', {
    props: { modelValue: true, resourceType: 'dgc_realtime_job', tenantId: 11, onSaved: saved, 'onUpdate:modelValue': updateVisible },
    dependencies: { '@/api/job-selection': api },
  })
  disposers.push(view.unmount)
  for (let i = 0; i < 5; i++) await settleView()
  return { ...view, api, saved, updateVisible }
}

describe('DataArts 监控名单', () => {
  it('未配置保留通用范围，打开弹窗不自动查云端或保存名单', async () => {
    const view = await dialog()
    expect(view.state.ready).toBe(true)
    expect(view.state.mode).toBe('inherit')
    expect(view.api.discoverSelectionJobs).not.toHaveBeenCalled()
    await view.state.save()
    expect(view.api.saveJobSelection).not.toHaveBeenCalled()
  })

  it('指定空名单显式保存，绝不转为空通用规则或全部模式', async () => {
    const view = await dialog()
    view.state.mode = 'selected'
    await view.state.save()
    expect(view.api.saveJobSelection).toHaveBeenCalledWith(11, 'dgc_realtime_job', {
      project_ref: 101, workspace: 'ws-a', mode: 'selected', jobs: [],
    })
    expect(view.saved).toHaveBeenCalledOnce()
  })

  it('云端清单按页请求，勾选名单跨页保留且不把通配符解释成全量', async () => {
    const view = await dialog()
    await view.state.loadJobs(1)
    view.state.toggleJob('job-1', true)
    await view.state.loadJobs(2)
    view.state.toggleJob('job-*', true)
    expect(view.state.selected).toEqual(['job-1', 'job-*'])
    expect(view.api.discoverSelectionJobs).toHaveBeenLastCalledWith(11, 'dgc_realtime_job', {
      project_ref: 101, workspace: 'ws-a', page: 2, page_size: 100, keyword: '',
    }, expect.any(AbortSignal))
    await view.state.save()
    expect(view.api.saveJobSelection).toHaveBeenLastCalledWith(11, 'dgc_realtime_job', {
      project_ref: 101, workspace: 'ws-a', mode: 'selected', jobs: ['job-*', 'job-1'],
    })
  })

  it('云查询失败不清空已选名单、不允许失败变成空清单授权', async () => {
    const view = await dialog({ discoverSelectionJobs: vi.fn(async () => { throw new Error('synthetic') }) })
    view.state.toggleJob('approved', true)
    await view.state.loadJobs()
    expect(view.state.selected).toEqual(['approved'])
    expect(view.state.listError).toContain('失败')
  })

  it('切换项目后丢弃旧页面响应并清空旧勾选', async () => {
    const pending = deferred<JobDiscovery>()
    const view = await dialog({ discoverSelectionJobs: vi.fn(() => pending.promise) })
    view.state.toggleJob('project-a-job', true)
    const listing = view.state.loadJobs()
    view.state.projectRef = 102
    await settleView()
    pending.resolve(page(1, ['stale-from-project-a']))
    await listing
    for (let i = 0; i < 3; i++) await settleView()
    expect(view.state.selected).toEqual([])
    expect(view.state.rows).toEqual([])
    expect(view.state.projectRef).toBe(102)
  })

  it('切换工作空间后不能带着上一空间名单保存', async () => {
    const view = await dialog()
    view.state.toggleJob('workspace-a-job', true)
    view.state.workspace = 'ws-b'
    for (let i = 0; i < 3; i++) await settleView()
    expect(view.state.selected).toEqual([])
    expect(view.state.mode).toBe('inherit')
    await view.state.save()
    expect(view.api.saveJobSelection).not.toHaveBeenCalled()
  })

  it('配置读取失败时禁止保存未加载的空名单', async () => {
    const view = await dialog({ getJobSelection: vi.fn(async () => { throw new Error('synthetic') }) })
    view.state.mode = 'selected'
    await view.state.save()
    expect(view.state.ready).toBe(false)
    expect(view.api.saveJobSelection).not.toHaveBeenCalled()
  })
})
