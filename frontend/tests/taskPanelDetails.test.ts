import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import dayjs from 'dayjs'
import { reactive } from 'vue'
import type { TaskDefinition } from '../src/types/task-executions'
import type { InspectionTask, ReportSchedule, TaskRun } from '../src/api'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })

function run(overrides: Partial<TaskRun> = {}): TaskRun {
  return { id: 1, tenant_id: 7, tenant_name: '测试租户', project_ref: 11, trigger_type: 'SCHEDULE',
    started_at: '2026-11-09T01:00:00Z', finished_at: '2026-11-09T01:00:01Z', status: 'SUCCESS',
    resource_total: 5, abnormal_total: 0, api_calls: 1, error_summary: null, stats: {}, ...overrides }
}
function task(code = 'gaussdb_status', overrides: Partial<InspectionTask> = {}): InspectionTask {
  return { code, name: 'GaussDB 实例状态', service: 'gaussdb', interval_sec: 120, cron: null, enabled: true, params: {}, runs: [],
    next_run: null, next_run_source: 'scheduler', types: ['gaussdb_instance'], note: null, min_interval: 30, skipped_tenants: [],
    checks: ['instance_status'], steps: ['查询实例清单', '检查实例状态'], ...overrides }
}
const gauss = { phase: 'storage', instances: 12, online: 10, queried: 9, failed: 1, concurrency: 4,
  list_ms: 10.5, detail_ms: 30, detail_total_ms: 85, detail_max_ms: 24, wall_ms: 42 }
const schedule: ReportSchedule = { cron: { DAILY: '30 3 * * *' }, enabled: { DAILY: true }, mail_to: [], recipient_user_ids: [], timezone: 'Asia/Shanghai',
  kinds: { DAILY: '日报' }, jobs: [], tenants: [], note: '租户报表范围说明', editable: true, updated_by: null, updated_at: null, customized: false }

interface Panel {
  tasks: InspectionTask[]; filteredTasks: InspectionTask[]; expanded: string[]
  filters: { service: string; enabled: string; abnormal: boolean; keyword: string }
  serviceOptions: { value: string; label: string }[]
  run(task: InspectionTask): Promise<void>
  load(): Promise<void>; resetFilters(): void; hasRecentAbnormal(task: InspectionTask): boolean
  taskDetails(task: InspectionTask): { codes: string[]; checks: string[]; steps: string[]; provided: boolean }
  gaussStatsText(code: string, stats: unknown): string; statsText(code: string, run: TaskRun): string
  taskStatus(code: string): { label: string; type: string }; serviceLabel(code: string): string
  openEdit(task: InspectionTask): void; saveEdit(): Promise<void>; toggle(task: InspectionTask, on: boolean): Promise<void>
  edit: { value: number; unit: number; enabled: boolean; cron: string; p90MinSamples: number; visible: boolean; parameters: Record<string, string | number | null> }
  canEdit: boolean; canReportConfig: boolean
  openEcsPolicy(): void; ecsPolicy: { tenantId: number | null; tenantName: string; visible: boolean }
  successRate(task: InspectionTask): number | null
}
async function panel(rows: InspectionTask[] = [], options: { tenantIds?: number[] | null; permissions?: string[]; confirm?: () => Promise<unknown> } = {}) {
  const api = { listTasks: vi.fn(async () => rows), reportSchedule: vi.fn(async () => schedule), updateTask: vi.fn(async () => ({ changed: true })), runTask: vi.fn() }
  const confirm = options.confirm ?? vi.fn(async () => 'confirm')
  const identity = reactive({ tenant_ids: options.tenantIds ?? null })
  const can = (permission: string) => !options.permissions || options.permissions.includes(permission)
  const view = mountBusinessScript<Panel>('system/TaskPanel.vue', { api, confirm,
    app: { currentTenant: { id: 7, name: '测试租户', short_name: '测试' } },
    dependencies: { '@/stores/user': { useUserStore: () => ({ user: identity, can }) } },
  })
  dispose.push(view.unmount)
  await view.state.load()
  return { ...view, api, confirm, identity }
}

describe('TaskPanel 检查项、步骤及组合筛选', () => {
  it('服务、启停、最近异常与多个搜索词同时生效，保留后端顺序和原任务', async () => {
    const rows = [task(), task('gaussdb_storage', { name: 'GaussDB 磁盘容量', checks: ['disk'], steps: ['并发查询磁盘用量'], runs: [run({ status: 'PARTIAL' })] }),
      task('gaussdb_components', { name: 'GaussDB 组件健康', enabled: false, checks: ['components'], runs: [run({ status: 'FAILED' })] }),
      task('ecs_status', { service: 'ecs', name: 'ECS 状态', runs: [run({ status: 'FAILED' })] })]
    const { state } = await panel(rows)
    Object.assign(state.filters, { service: 'gaussdb', enabled: 'enabled', abnormal: true, keyword: ' GAUSSdb 磁盘 ' })
    expect(state.filteredTasks.map(row => row.code)).toEqual(['gaussdb_storage'])
    expect(state.tasks).toEqual(rows)
    state.expanded = ['gaussdb_components']
    state.resetFilters()
    expect(state.filteredTasks.map(row => row.code)).toEqual(rows.map(row => row.code))
    expect(state.expanded).toEqual(['gaussdb_components'])
    state.filters.enabled = 'disabled'
    expect(state.filteredTasks.map(row => row.code)).toEqual(['gaussdb_components'])
  })

  it.each(['组件状态', 'components', '请求详情', '原生只读', 'gaussdb_components', 'GaussDB'])('搜索覆盖检查中文/code、步骤、说明、任务code和服务：%s', async keyword => {
    const { state } = await panel([task('gaussdb_components', { checks: ['components'], steps: ['并发请求详情'], note: '原生只读接口' })])
    state.filters.keyword = keyword
    expect(state.filteredTasks).toHaveLength(1)
  })

  it('旧返回、明确空检查项、错误类型及未知/原型 code 安全兼容，不打乱顺序', async () => {
    const { state } = await panel()
    expect(state.taskDetails(task('old', { checks: undefined, steps: undefined }))).toEqual({ codes: [], checks: [], steps: [], provided: false })
    expect(state.taskDetails(task('empty', { checks: [], steps: [] })).provided).toBe(true)
    const bad = task('new', { checks: ['disk', '__proto__', 'constructor', 'future_check', 1, null] as unknown as string[],
      steps: ['步骤二', {}, '步骤一', '', 7] as unknown as string[] })
    expect(state.taskDetails(bad).checks).toEqual(['磁盘使用率', '__proto__', 'constructor', 'future_check'])
    expect(state.taskDetails(bad).steps).toEqual(['步骤二', '步骤一'])
    expect(state.taskDetails(task('bad', { checks: {} as string[], steps: '不可迭代' as unknown as string[] })).checks).toEqual([])
    const inherited = Object.assign(Object.create({ checks: ['disk'], steps: ['不可信继承字段'] }), task())
    delete inherited.checks; delete inherited.steps
    expect(state.taskDetails(inherited)).toEqual({ codes: [], checks: [], steps: [], provided: false })
    expect(state.serviceLabel('__proto__')).toBe('__proto__')
    expect(state.taskStatus('constructor')).toEqual({ label: 'constructor', type: 'info' })
  })

  it('异常按各租户/项目最新记录，旧失败恢复后、运行中及跳过均不误报', async () => {
    const { state } = await panel()
    expect(state.hasRecentAbnormal(task('recovered', { runs: [run(), run({ status: 'FAILED' })] }))).toBe(false)
    expect(state.hasRecentAbnormal(task('other-project', { runs: [run(), run({ status: 'FAILED', project_ref: 12 })] }))).toBe(true)
    expect(state.hasRecentAbnormal(task('other-tenant', { runs: [run(), run({ status: 'TIMEOUT', tenant_id: 8 })] }))).toBe(true)
    expect(state.hasRecentAbnormal(task('unknown-project', { runs: [run(), run({ status: 'PARTIAL', project_ref: null })] }))).toBe(true)
    for (const status of ['QUEUED', 'RUNNING', 'SKIPPED']) expect(state.hasRecentAbnormal(task('skipped', { runs: [run({ status, abnormal_total: 3 })] }))).toBe(false)
    expect(state.hasRecentAbnormal(task('ledger_sync', { runs: [run({ abnormal_total: 5 })] }))).toBe(false)
    expect(state.hasRecentAbnormal(task('data_cleanup', { runs: [run({ abnormal_total: 5 })] }))).toBe(false)
    expect(state.hasRecentAbnormal(task('gaussdb_storage', { runs: [run({ stats: { gaussdb: gauss } })] }))).toBe(true)
    expect(state.hasRecentAbnormal(task('ecs_status', { runs: [run({ abnormal_total: 1 })] }))).toBe(true)
    expect(state.hasRecentAbnormal(task('interrupted', { runs: [run({ status: 'INTERRUPTED' })] }))).toBe(true)
    expect(state.hasRecentAbnormal(task())).toBe(false)
  })

  it('服务选项来自实际任务，原型 key 也是普通文本；无匹配结果不改原列表', async () => {
    const { state } = await panel([task(), task('dup'), task('future', { service: '__proto__' })])
    expect(state.serviceOptions).toEqual([{ value: 'gaussdb', label: 'GaussDB' }, { value: '__proto__', label: '__proto__' }])
    state.filters.keyword = '不存在的关键字'
    expect(state.filteredTasks).toEqual([])
    expect(state.tasks).toHaveLength(3)
  })
})

describe('TaskPanel Gauss 运行统计白名单', () => {
  it.each(['gaussdb_status', 'gaussdb_storage', 'gaussdb_components'])('%s 明确展示实例、失败、各阶段耗时和并发', async code => {
    const { state } = await panel()
    const value = state.statsText(code, run({ stats: { gaussdb: gauss } }))
    for (const text of ['磁盘容量', '实例 12', '在线 10', '已查询 9', '详情失败 1', '并发 4', '清单 10.5 ms', '明细 30 ms', '详情累计 85 ms', '单次详情最大 24 ms', '墙钟 42 ms']) expect(value).toContain(text)
  })

  it('缺少新统计、畸形值、未知阶段及字段不补零、不序列化、不读取原型', async () => {
    const { state } = await panel()
    for (const stats of [undefined, null, {}, { gaussdb: [] }, { gaussdb: 'bad' }, Object.create({ gaussdb: gauss })]) expect(state.gaussStatsText('gaussdb_storage', stats)).toBe('')
    const stats = { gaussdb: { phase: { toString() { throw Error('不得强转未知字段') } }, instances: '12', online: NaN, queried: -1,
      failed: 1.5, concurrency: Infinity, list_ms: -1, detail_ms: '30', detail_total_ms: NaN, detail_max_ms: Infinity, wall_ms: null,
      secret: '不得展示的扩展字段', __proto__: { instances: 99 } } }
    const value = state.gaussStatsText('gaussdb_components', stats)
    expect(value).toBe('未知阶段 · 实例 — · 在线 — · 已查询 — · 详情失败 — · 并发 — · 清单 — · 明细 — · 详情累计 — · 单次详情最大 — · 墙钟 —')
    expect(value).not.toContain('不得展示')
    expect(state.gaussStatsText('gaussdb_status', { gaussdb: Object.create(gauss) })).toContain('实例 —')
    expect(state.gaussStatsText('__proto__', { gaussdb: gauss })).toBe('')
    expect(state.gaussStatsText('ecs_status', { gaussdb: gauss })).toBe('')
    expect(state.gaussStatsText('gaussdb_storage', { gaussdb: { ...gauss, instances: 0, failed: 0, detail_total_ms: 0 } })).toContain('详情失败 0')
  })
})

describe('TaskPanel 保留授权、独立周期及已有管理行为', () => {
  it('三个 Gauss 任务独立保存后端周期与启停，不覆盖其他任务或参数', async () => {
    const rows = [task(), task('gaussdb_storage', { interval_sec: 600, cron: '0 2 * * *', enabled: false }),
      task('gaussdb_components', { interval_sec: 900, enabled: false })]
    const { state, api } = await panel(rows)
    for (const row of rows) {
      state.openEdit(row)
      expect(state.edit.enabled).toBe(row.enabled)
      expect(state.edit.value * state.edit.unit).toBe(row.interval_sec)
      expect(state.edit.cron).toBe(row.cron ?? '')
      await state.saveEdit()
      expect(api.updateTask).toHaveBeenLastCalledWith(row.code, row.cron
        ? { enabled: row.enabled, cron: row.cron } : { enabled: row.enabled, interval_sec: row.interval_sec, cron: '' })
    }
  })

  it('全局/租户权限与报表可编辑性保留，租户管理员不能修改共享任务', async () => {
    const scoped = await panel([task()], { tenantIds: [7] })
    expect(scoped.state.canEdit).toBe(false)
    expect(scoped.state.canReportConfig).toBe(false)
    scoped.state.openEdit(task()); await scoped.state.saveEdit(); await scoped.state.toggle(task(), false)
    expect(scoped.api.updateTask).not.toHaveBeenCalled()
    const readonly = await panel([task()], { permissions: ['task:view'] })
    expect(readonly.state.canEdit).toBe(false)
    expect(readonly.api.reportSchedule).not.toHaveBeenCalled()
  })

  it('只有白名单 params 的任务仍展示后端整轮超时', async () => {
    const rows = [task('mrs_job_status', { service: 'mrs', params: { timeout_sec: 180 } })]
    const view = mountPersonnel('system/TaskPanel.vue', { api: { listTasks: vi.fn(async () => rows), reportSchedule: vi.fn(async () => null) },
      dependencies: { dayjs: { default: dayjs } } })
    dispose.push(view.unmount)
    await settlePersonnel()
    const card = findNode(view.root, 'Component', node => (node.props.task as InspectionTask | undefined)?.code === 'mrs_job_status' && 'checks' in node.props)
    expect(card.props['timeout-label']).toBe('180 秒')
  })

  it('MRS 原生参数使用真实 schema 与 mrs_options，仅提交白名单并保留周期', async () => {
    const mrs: TaskDefinition = { ...task('mrs_job_status', { service: 'mrs', params: { internal_secret: '不得回传', query_mode: 'active_and_recent' } }),
      effective_params: { timeout_sec: 300, query_mode: 'active_only', max_calls: 30 },
      parameter_schema: { properties: { timeout_sec: { type: 'integer', minimum: 15, maximum: 3600 }, query_mode: { type: 'string', enum: ['active_only'] }, max_calls: { type: 'integer', minimum: 1, maximum: 400 } } } }
    const view = await panel([mrs])
    view.state.openEdit(mrs)
    expect(view.state.edit.parameters).not.toHaveProperty('internal_secret')
    view.state.edit.parameters.timeout_sec = 180
    view.state.edit.parameters.max_calls = 50
    await view.state.saveEdit()
    expect(view.api.updateTask).toHaveBeenCalledExactlyOnceWith('mrs_job_status', { enabled: true, interval_sec: 120, cron: '', timeout_sec: 180, mrs_options: { query_mode: 'active_only', max_calls: 50 } })
  })

  it('只读执行守卫不会发写请求，即使直接触发处理函数', async () => {
    const readonly = await panel([task()], { permissions: ['task:view'] })
    await readonly.state.run(task())
    expect(readonly.api.runTask).not.toHaveBeenCalled()
  })

  it('切换租户立即清空旧任务，并丢弃旧租户的迟到列表响应', async () => {
    const view = await panel([task()])
    const old = deferred<InspectionTask[]>(), current = deferred<InspectionTask[]>()
    view.api.listTasks.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const previousLoad = view.state.load()
    view.appState.tenantId = 8
    expect(view.state.tasks).toEqual([])
    current.resolve([task('new-tenant')]); await settleView(); await settleView()
    old.resolve([task('stale-tenant')]); await previousLoad
    expect(view.state.tasks.map(task => task.code)).toEqual(['new-tenant'])
  })

  it('配置写入失败显示实际错误，保留编辑内容和原启停状态', async () => {
    const row = task()
    const view = await panel([row])
    view.state.openEdit(row)
    view.api.updateTask.mockRejectedValueOnce(new Error('参数存在冲突'))
    await expect(view.state.saveEdit()).resolves.toBeUndefined()
    expect(view.messages.error).toHaveBeenCalledWith('参数存在冲突')
    expect(view.state.edit.visible).toBe(true)
    view.api.updateTask.mockRejectedValueOnce(new Error('暂时无法更新'))
    await expect(view.state.toggle(row, false)).resolves.toBeUndefined()
    expect(view.messages.error).toHaveBeenCalledWith('暂时无法更新')
    expect(view.state.tasks[0]!.enabled).toBe(true)
  })

  it('停用仍先确认，取消不更新；确认后只提交当前任务', async () => {
    const confirm = vi.fn<() => Promise<unknown>>().mockRejectedValueOnce('cancel').mockResolvedValue('confirm')
    const { state, api } = await panel([task()], { confirm })
    await expect(state.toggle(task(), false)).resolves.toBeUndefined()
    expect(api.updateTask).not.toHaveBeenCalled()
    await state.toggle(task(), false)
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('确认停用'), '停用任务', { type: 'warning' })
    expect(api.updateTask).toHaveBeenCalledExactlyOnceWith('gaussdb_status', { enabled: false })
  })

  it('P90 校验、租户 ECS 策略与已展示成功率口径不回退', async () => {
    const { state, api } = await panel()
    state.openEdit(task('dgc_batch_instance', { service: 'dataarts', params: { p90_min_samples: 7 } }))
    expect(state.edit.p90MinSamples).toBe(7)
    state.edit.p90MinSamples = 0; await state.saveEdit()
    expect(api.updateTask).not.toHaveBeenCalled()
    state.edit.p90MinSamples = 9; await state.saveEdit()
    expect(api.updateTask).toHaveBeenCalledWith('dgc_batch_instance', expect.objectContaining({ p90_min_samples: 9 }))
    state.openEcsPolicy()
    expect(state.ecsPolicy).toEqual({ visible: true, tenantId: 7, tenantName: '测试' })
    expect(state.successRate(task('rates', { runs: [run(), run({ status: 'FAILED' }), run({ status: 'RUNNING' }), run({ status: 'SKIPPED' })] }))).toBe(50)
  })
})

describe('TaskPanel 实际 Vue 模板的筛选和详情连接', () => {
  it('实际 SFC 模板无解析错误，样式限制在组件作用域', () => {
    const source = readFileSync(new URL('../src/views/system/TaskPanel.vue', import.meta.url), 'utf8')
    const parsed = parse(source)
    expect(parsed.errors).toEqual([])
    expect(parsed.descriptor.styles.every(style => style.scoped)).toBe(true)
  })

  it('任务卡片承载检查项，详情接收步骤和运行记录，筛选可重置', async () => {
    const rows = [task(), task('gaussdb_storage', { name: 'GaussDB 磁盘容量', checks: ['disk'], steps: ['筛选在线实例', '并发查询磁盘用量'], runs: [run({ stats: { gaussdb: gauss } })] })]
    const view = mountPersonnel('system/TaskPanel.vue', { api: { listTasks: vi.fn(async () => rows), reportSchedule: vi.fn(async () => schedule) },
      dependencies: { dayjs: { default: dayjs }, '@/stores/app': { useAppStore: () => ({ currentTenant: { name: '测试租户' } }) } } })
    dispose.push(view.unmount)
    await settlePersonnel()
    const card = findNode(view.root, 'Component', node => (node.props.task as InspectionTask | undefined)?.code === 'gaussdb_storage' && 'checks' in node.props)
    expect(card.props.checks).toEqual(['磁盘使用率'])
    expect(card.props['scope-label']).toContain('测试租户')
    ;(card.props.onDetail as () => void)()
    await settlePersonnel()
    const drawer = findNode(view.root, 'Component', node => 'steps' in node.props && 'model-value' in node.props)
    expect(drawer.props.steps).toEqual(['筛选在线实例', '并发查询磁盘用量'])
    expect(drawer.props['model-value']).toBe(true)
    const search = findNode(view.root, 'Input', node => node.props['aria-label'] === '搜索巡检任务')
    ;(search.props['onUpdate:modelValue'] as (value: string) => void)('不存在')
    await settlePersonnel()
    expect(findNode(view.root, 'Empty').props.description).toBe('无匹配任务，请调整筛选条件')
    await clickButton(view.root, '重置')
    expect(textOf(view.root)).toContain('显示 2 / 2 项')
  })

  it('ECS 巡检任务行有明确的监控配置入口并带入当前租户', async () => {
    const rows = [task('ecs_status', { service: 'ecs', name: 'ECS 云服务器状态' })]
    const view = mountPersonnel('system/TaskPanel.vue', { api: {
      listTasks: vi.fn(async () => rows), reportSchedule: vi.fn(async () => schedule),
    }, dependencies: {
      dayjs: { default: dayjs },
      '@/stores/app': { useAppStore: () => ({ currentTenant: { id: 7, name: '测试租户', short_name: '测试' }, tenants: [{ id: 7, name: '测试租户' }] }) },
    } })
    dispose.push(view.unmount)
    await settlePersonnel()
    const card = findNode(view.root, 'Component', item => item.props['has-ecs-policy'] === true)
    ;(card.props.onEcs as () => void)()
    await settlePersonnel()
    const dialog = findNode(view.root, 'Component', item => 'tenant-id' in item.props && 'tenant-name' in item.props)
    expect(dialog.props).toMatchObject({ modelValue: true, 'tenant-id': 7, 'tenant-name': '测试' })
  })
})
