import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, shallowRef } from 'vue'
import type { ImportResult, SilenceIn, SilencePreview } from '../src/types'
import { useLedgerImport } from '../src/views/ledger/useLedgerImport'
import { reportRegenerationDate } from '../src/views/reports/report-title'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'

const dispose: (() => void)[] = []
beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.clearAllTimers(); vi.useRealTimers() })
const importResult = (changed: number): ImportResult => ({ dry_run: true, total: 1, matched: 1, changed, unchanged: 0, error_count: 0, items: [], errors: [] })

function importer() {
  const tenant = shallowRef<number | null>(7)
  const requests: { file: File; dryRun: boolean; skip: boolean; tenantId: number | null; signal?: AbortSignal; result: ReturnType<typeof deferred<ImportResult>> }[] = []
  const scope = effectScope(); dispose.push(() => scope.stop())
  const model = scope.run(() => useLedgerImport({ tenantId: () => tenant.value, importer(file, dryRun, skip, context) {
    const result = deferred<ImportResult>()
    requests.push({ file, dryRun, skip, ...context, result })
    return result.promise
  } }))!
  return { model, tenant, requests, scope }
}

describe('Excel导入预览与应用', () => {
  it.each(['resolve', 'reject'] as const)('旧文件的%s不覆盖新文件预览或清除新文件', async outcome => {
    const { model, requests } = importer()
    const first = new File(['a'], 'A.xlsx'), second = new File(['b'], 'B.xlsx')
    const old = model.preview(first), current = model.preview(second)
    expect(requests[0].signal?.aborted).toBe(true)
    requests[1].result.resolve(importResult(2)); await current
    if (outcome === 'resolve') requests[0].result.resolve(importResult(1))
    else requests[0].result.reject(new Error('旧文件错误'))
    await old
    expect(model.file.value).toBe(second)
    expect(model.result.value?.changed).toBe(2)
    expect(model.parseError.value).toBe('')
  })

  it('无成功预览不能应用；应用时捕获文件/租户并阻止重复提交', async () => {
    const { model, requests } = importer()
    const file = new File(['fixture'], 'QA.xlsx')
    const preview = model.preview(file)
    expect(await model.apply()).toBeNull()
    requests[0].result.resolve(importResult(1)); await preview
    const apply = model.apply()
    expect(await model.apply()).toBeNull()
    expect(requests[1]).toMatchObject({ file, dryRun: false, tenantId: 7 })
    await model.preview(new File(['new'], 'new.xlsx'))
    expect(requests).toHaveLength(2)
    requests[1].result.resolve({ ...importResult(1), applied: 1 }); await apply
    expect(model.canApply.value).toBe(false)
    expect(await model.apply()).toBeNull()
  })

  it('失败可重试；有错误行时必须明确允许跳过', async () => {
    const { model, requests } = importer()
    const preview = model.preview(new File(['fixture'], 'QA.xlsx'))
    requests[0].result.resolve({ ...importResult(1), error_count: 1 }); await preview
    expect(await model.apply()).toBeNull()
    model.skipErrors.value = true
    const apply = model.apply()
    expect(requests[1].skip).toBe(true)
    requests[1].result.reject(new Error('写入失败')); await apply
    expect(model.parseError.value).toBe('写入失败')
    expect(model.canApply.value).toBe(true)
  })

  it.each(['tenant', 'close', 'dispose'] as const)('%s后旧预览不能重现或用于导入', async action => {
    const { model, requests, tenant, scope } = importer()
    const preview = model.preview(new File(['fixture'], 'QA.xlsx'))
    if (action === 'tenant') tenant.value = 8
    if (action === 'close') model.reset()
    if (action === 'dispose') scope.stop()
    requests[0].result.resolve(importResult(1)); await preview
    expect(model.result.value).toBeNull()
    expect(await model.apply()).toBeNull()
  })
})

type Report = { id: number; title: string; kind: 'DAILY'; period_start: string; tenant_id: number; sent_to: string[]; data: { period: { label: string } } }
function report(id: number): Report { return { id, title: `QA ${id}`, kind: 'DAILY', period_start: '2026-10-25T16:00:00Z', tenant_id: 7, sent_to: [], data: { period: { label: '2026-10-26 00:00 ~ 2026-10-27 00:00' } } } }
interface ReportsState {
  current: Report | null; onDelete(): Promise<void>; deleting: boolean; openSend(): Promise<void>; doSend(): Promise<void>
  send: { recipient_user_ids: number[]; reportId: number | null }; doGenerate(): Promise<void>
}
function reports(confirm: () => Promise<unknown> = vi.fn(async () => 'confirm')) {
  const api = { deleteReport: vi.fn(async () => undefined), listReports: vi.fn(async () => ({ items: [], total: 0 })),
    sendReport: vi.fn<(id: number, ids: number[]) => Promise<void>>(async () => undefined), getReport: vi.fn(async () => report(1)), createReport: vi.fn(async () => report(1)) }
  const view = mountBusinessScript<ReportsState>('reports/ReportView.vue', { api, confirm })
  dispose.push(view.unmount)
  view.state.current = report(1)
  return { ...view, api }
}

describe('报表操作目标与平台周期', () => {
  it('删除确认期间切换报表，仍只删除原对象，不清空新详情', async () => {
    const confirmation = deferred<string>()
    const { state, api } = reports(() => confirmation.promise)
    const pending = state.onDelete()
    state.current = report(2)
    confirmation.resolve('confirm'); await pending
    expect(api.deleteReport).toHaveBeenCalledWith(1)
    expect(state.current?.id).toBe(2)
  })

  it('删除确认只能打开一次；取消、卸载或换租户不再提交', async () => {
    const confirmation = deferred<string>()
    const confirm = vi.fn(() => confirmation.promise)
    const view = reports(confirm)
    const pending = view.state.onDelete()
    await view.state.onDelete()
    expect(confirm).toHaveBeenCalledTimes(1)
    view.appState.tenantId = 8
    confirmation.resolve('confirm'); await pending
    expect(view.api.deleteReport).not.toHaveBeenCalled()
    expect(view.state.deleting).toBe(false)
  })

  it('发送绑定打开弹窗时的报表，收件人复制且防重复', async () => {
    const view = reports()
    const sent = deferred<void>()
    view.api.sendReport.mockImplementationOnce(() => sent.promise)
    await view.state.openSend()
    view.state.current = report(2)
    view.state.send.recipient_user_ids = [101]
    const sending = view.state.doSend()
    await view.state.doSend()
    view.state.send.recipient_user_ids.push(102)
    expect(view.api.sendReport).toHaveBeenCalledTimes(1)
    expect(view.api.sendReport).toHaveBeenCalledWith(1, [101])
    sent.resolve(); await sending
    expect(view.state.current?.id).toBe(2)
  })

  it('重新生成使用平台周期日期，不依赖浏览器时区或固定UTC+8', () => {
    expect(reportRegenerationDate('DAILY', '2026-10-26 00:00 ~ 2026-10-27 00:00')).toBe('2026-10-26')
    expect(reportRegenerationDate('MONTHLY', '2026-10-01 00:00 ~ 2026-11-01 00:00')).toBe('2026-10-01')
    expect(reportRegenerationDate('LEDGER', '')).toBeUndefined()
    expect(() => reportRegenerationDate('DAILY', '')).toThrow()
  })
})

interface SwitchState {
  data: unknown; staged: Map<string, boolean | null>; saving: boolean
  confirm: { visible: boolean; items: { resource_type: string; tenant_id: number; enabled: boolean | null }[] }
  stage(type: string, tenant: number, value: boolean): void; onSave(): Promise<void>; doSave(): Promise<void>
}
function switches(impact = deferred<unknown>()) {
  const api = { switchImpact: vi.fn(() => impact.promise), saveCollectSwitches: vi.fn(async () => ({ changed: [], closed_alarms: 0 })),
    listTasks: vi.fn(async () => []), getCollectSwitches: vi.fn(async () => ({ types: [], tenants: [], categories: [] })) }
  const view = mountBusinessScript<SwitchState>('system/CollectSwitchPanel.vue', { api })
  dispose.push(view.unmount)
  view.state.data = { types: [{ code: 'ecs_server', global: true, tenants: { 7: { setting: true } } }], tenants: [], categories: [] }
  return { ...view, api, impact }
}

describe('采集开关预估与保存一致性', () => {
  it('预估期间被刷新或修改的草稿不可进入确认', async () => {
    const view = switches()
    view.state.stage('ecs_server', 7, false)
    const preview = view.state.onSave()
    view.state.staged.clear()
    view.impact.resolve({ stop: [], start: [], alarms: 0 }); await preview
    expect(view.state.confirm.visible).toBe(false)
    expect(view.api.saveCollectSwitches).not.toHaveBeenCalled()
  })

  it('只保存已确认批次，并阻止确认期间修改和重复保存', async () => {
    const view = switches()
    view.state.stage('ecs_server', 7, false)
    const preview = view.state.onSave()
    view.impact.resolve({ stop: [], start: [], alarms: 0 }); await preview
    view.state.stage('ecs_server', 7, true)
    expect(view.state.confirm.items).toEqual([{ resource_type: 'ecs_server', tenant_id: 7, enabled: false }])
    const pending = deferred<{ changed: []; closed_alarms: number }>()
    view.api.saveCollectSwitches.mockImplementationOnce(() => pending.promise)
    const save = view.state.doSave()
    await view.state.doSave()
    expect(view.api.saveCollectSwitches).toHaveBeenCalledTimes(1)
    expect(view.api.saveCollectSwitches).toHaveBeenCalledWith([{ resource_type: 'ecs_server', tenant_id: 7, enabled: false }])
    pending.resolve({ changed: [], closed_alarms: 0 }); await save
  })
})

interface SilenceState {
  m: { services: string[] }; f: { name: string }; formRef: { validate(): Promise<boolean> } | undefined
  pv: SilencePreview | null; pvError: string; submit(): Promise<void>; saving: boolean
}
const safeImpact = (): SilencePreview => ({ total: 0, critical: 0, by_severity: { P1: 0, P2: 0, P3: 0, P4: 0 }, items: [] })
function silence() {
  const previews: ReturnType<typeof deferred<SilencePreview>>[] = []
  const api = { previewSilence: vi.fn(() => { const value = deferred<SilencePreview>(); previews.push(value); return value.promise }),
    listRules: vi.fn(async () => []), createSilence: vi.fn(async (_body: SilenceIn) => ({ id: 1, suppressed: 0 })),
    updateSilence: vi.fn(async (_id: number, _body: SilenceIn) => ({ id: 1, suppressed: 0 })) }
  const confirm = vi.fn(async () => 'confirm')
  const view = mountBusinessScript<SilenceState>('silences/SilenceDialog.vue', { api, props: { visible: false }, confirm })
  dispose.push(view.unmount)
  view.props.visible = true
  view.state.formRef = { validate: async () => true }
  return { ...view, api, previews, confirm }
}

describe('静默预览与提交', () => {
  it('匹配条件变化后，迟到的旧低风险预览不覆盖当前P1影响', async () => {
    const view = silence()
    await settleView()
    view.state.m.services = ['ecs']
    await vi.advanceTimersByTimeAsync(350)
    view.state.m.services = ['dcs']
    await vi.advanceTimersByTimeAsync(350)
    view.previews[1].resolve({ ...safeImpact(), total: 1, by_severity: { P1: 1, P2: 0, P3: 0, P4: 0 } }); await settleView()
    view.previews[0].resolve(safeImpact()); await settleView()
    expect(view.state.pv?.total).toBe(1)
    expect(view.state.pv?.by_severity.P1).toBe(1)
  })

  it('保存获取当前预估，预估失败不创建；同时阻止重复提交', async () => {
    const view = silence()
    await settleView()
    view.state.m.services = ['ecs']; view.state.f.name = 'QA silence'
    const submit = view.state.submit(); await settleView()
    await view.state.submit()
    expect(view.api.previewSilence).toHaveBeenCalledTimes(1)
    view.previews[0].reject(new Error('预估失败')); await submit
    expect(view.api.createSilence).not.toHaveBeenCalled()
    expect(view.state.pvError).toBe('预估失败')
    expect(view.state.saving).toBe(false)
  })

  it('关闭弹窗清除防抖，未开始预估不再发送', async () => {
    const view = silence()
    await settleView()
    view.state.m.services = ['ecs']
    view.props.visible = false
    await settleView(); await vi.advanceTimersByTimeAsync(350)
    expect(view.api.previewSilence).not.toHaveBeenCalled()
  })
})

interface AlarmListState {
  selected: { id: number }[]; onBatchAck(): Promise<void>
}
describe('告警批量确认快照', () => {
  it('SSE或分页清空选择时，仍只认领提示中的原始ID；重复确认只提交一次', async () => {
    const question = deferred<{ value: string }>()
    const api = { batchAck: vi.fn(async () => ({ acked: 2 })), listAlarms: vi.fn(async () => ({ items: [], total: 0 })) }
    const prompt = vi.fn(() => question.promise)
    const view = mountBusinessScript<AlarmListState>('alarms/AlarmListView.vue', { api, prompt })
    dispose.push(view.unmount)
    view.state.selected = [{ id: 1 }, { id: 2 }]
    const action = view.state.onBatchAck()
    await view.state.onBatchAck()
    view.state.selected = [{ id: 9 }]
    question.resolve({ value: 'QA note' }); await action
    expect(prompt).toHaveBeenCalledTimes(1)
    expect(api.batchAck).toHaveBeenCalledWith([1, 2], 'QA note')
  })
})

describe('通知模拟和记录筛选', () => {
  it('修改模拟输入后清除原结论，旧响应不能回填', async () => {
    const pending = deferred<unknown>()
    const api = { simulateRoute: vi.fn(() => pending.promise) }
    const view = mountBusinessScript<{ q: { service: string }; res: unknown; run(): Promise<unknown> }>('notify/SimulatePanel.vue', { api, app: { tenants: [{ id: 7, name: 'QA tenant' }], currentTenant: { id: 7 } } })
    dispose.push(view.unmount)
    const run = view.state.run()
    expect(api.simulateRoute).toHaveBeenCalledTimes(1)
    view.state.q.service = 'ecs'
    pending.resolve({ routes: ['old'] }); await run
    expect(view.state.res).toBeNull()
  })

  it('发送记录切换筛选后不显示旧筛选的迟到结果', async () => {
    const first = deferred<unknown[]>(), second = deferred<unknown[]>()
    const api = { listNotifications: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) }
    const view = mountBusinessScript<{ records: unknown[]; recordStatus: string; loadRecords(): Promise<unknown> }>('notify/NotifyView.vue', { api })
    dispose.push(view.unmount)
    view.state.recordStatus = 'FAILED'; const old = view.state.loadRecords()
    view.state.recordStatus = 'SENT'; const current = view.state.loadRecords()
    second.resolve([{ id: 2 }]); await current
    first.resolve([{ id: 1 }]); await old
    expect(view.state.records).toEqual([{ id: 2 }])
  })
})

describe('系统与容器重复提交', () => {
  it('手动运行任务不得重叠并覆盖运行中标记', async () => {
    const pending = deferred<unknown>()
    const api = { runTask: vi.fn(() => pending.promise), listTasks: vi.fn(async () => []), reportSchedule: vi.fn(async () => null) }
    const view = mountBusinessScript<{ run(task: { code: string; name: string }): Promise<void>; running: string }>('system/TaskPanel.vue', { api })
    dispose.push(view.unmount)
    const run = view.state.run({ code: 'ecs_status', name: 'ECS' })
    await view.state.run({ code: 'dcs_status', name: 'DCS' })
    expect(api.runTask).toHaveBeenCalledTimes(1)
    expect(view.state.running).toBe('ecs_status')
    pending.resolve({ status: 'SUCCESS', runs: [] }); await run
    expect(view.state.running).toBe('')
  })

  it('探针异步验证通过后第二次提交仍不能重复创建', async () => {
    const validation = deferred<boolean>(), created = deferred<unknown>()
    const api = { createProbe: vi.fn(() => created.promise), listProbes: vi.fn(async () => []) }
    const view = mountBusinessScript<{ formRef: { validate(): Promise<boolean> }; save(): Promise<void> }>('pods/ProbePanel.vue', { api, props: { options: null } })
    dispose.push(view.unmount)
    view.state.formRef = { validate: () => validation.promise }
    const first = view.state.save(), second = view.state.save()
    validation.resolve(true); await settleView()
    expect(api.createProbe).toHaveBeenCalledTimes(1)
    created.resolve({ id: 1 }); await Promise.all([first, second])
  })

  it('字典仍在加载时不得提交空列表覆盖全局字典', async () => {
    const pending = deferred<unknown>()
    const api = { getLedgerDict: vi.fn(() => pending.promise), saveLedgerDict: vi.fn(async () => undefined) }
    const view = mountBusinessScript<{ save(): Promise<void> }>('ledger/LedgerDictDialog.vue', { api, props: { visible: false } })
    dispose.push(view.unmount)
    view.props.visible = true; await settleView()
    await view.state.save()
    expect(api.saveLedgerDict).not.toHaveBeenCalled()
    pending.resolve({ business: [], owners: [], business_outside: [], owners_outside: [], enforce: false }); await settleView()
  })
})
