import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import type { ScopeCandidate, ScopePreview, ScopeRule, ScopeSaveResult, ScopeType } from '../src/types'
import type { RuleDraft } from '../src/views/system/collect-scope'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'

interface DialogState {
  mode: 'follow' | 'own'; drafts: RuleDraft[]; candidates: Record<string, ScopeCandidate[]>
  preview: ScopePreview | null; previewing: boolean; previewStale: boolean; saving: boolean
  dirty: boolean; errors: string[]
  loadCandidates: (dim: string) => Promise<void>; runPreview: () => Promise<void>; save: () => Promise<void>
}

const rule = (name: string): ScopeRule => ({ effect: 'exclude', match: { name: [name] } })
function scopeType(code = 'ecs_server'): ScopeType {
  return {
    code, name: code, service: 'ecs', category: 'compute', dimensions: [{ key: 'name', label: '名称' }],
    global: { rules: [rule('global-*')], updated_by: null, updated_at: null },
    tenants: Object.fromEntries([7, 8].map(id => [String(id), {
      setting: [rule(`saved-${id}`)], source: 'tenant', rules: [rule(`saved-${id}`)],
      resources: 10, excluded: 1, updated_by: null, updated_at: null,
    }])),
  }
}
const previewResult = (excluded = 1, closing = 0): ScopePreview => ({
  total: 10, included: 10 - excluded, excluded, newly_excluded: excluded, newly_included: 0,
  closing_alarms: closing, truncated: false, samples: { excluded: [], included: [] }, rules: [],
})
const saveResult = (): ScopeSaveResult => ({
  changed: true, before: [], after: [rule('draft-*')], excluded: 2, restored: 0, closed_alarms: 0,
})
const dispose: (() => void)[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.useRealTimers() })

async function dialog(options: {
  props?: Record<string, unknown>; confirm?: () => Promise<unknown>; preview?: () => Promise<ScopePreview>
} = {}) {
  const api = {
    collectScopeCandidates: vi.fn(async (_tenant: number, _type: string, _dim: string, _keyword = '', _limit = 50): Promise<ScopeCandidate[]> => [{ value: 'current', count: 1 }]),
    previewCollectScope: vi.fn(async (_tenant: number, _type: string, _rules: ScopeRule[] | null): Promise<ScopePreview> => previewResult()),
    saveCollectScope: vi.fn(async (_tenant: number, _type: string, _rules: ScopeRule[] | null): Promise<ScopeSaveResult> => saveResult()),
  }
  if (options.preview) api.previewCollectScope.mockImplementation(options.preview)
  const confirm = vi.fn(options.confirm ?? (async () => 'confirm'))
  const updateVisible = vi.fn(), saved = vi.fn()
  const view = mountBusinessScript<DialogState>('system/CollectScopeDialog.vue', {
    props: { modelValue: true, tenantId: 7, tenantName: '租户7', type: reactive(scopeType()), canManage: true,
      limits: { rules: 20, patterns: 20, pattern_length: 200 }, ...options.props,
      'onUpdate:modelValue': updateVisible, onSaved: saved },
    api, confirm,
  })
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; view.unmount() } }
  dispose.push(unmount)
  await settleView()
  return { ...view, unmount, api, confirm, updateVisible, saved }
}
type Dialog = Awaited<ReturnType<typeof dialog>>
const contexts = [
  { change: 'close', active: false }, { change: 'reopen', active: true },
  { change: 'tenant', active: true }, { change: 'tenant-roundtrip', active: true },
  { change: 'type', active: true }, { change: 'type-refresh', active: true },
  { change: 'type-code', active: true }, { change: 'missing-type', active: false },
  { change: 'permission', active: false }, { change: 'unmount', active: false },
] as const
type ContextChange = typeof contexts[number]['change']
async function changeContext(view: Dialog, change: ContextChange) {
  switch (change) {
    case 'close': view.props.modelValue = false; break
    case 'reopen': view.props.modelValue = false; view.props.modelValue = true; break
    case 'tenant': view.props.tenantId = 8; break
    case 'tenant-roundtrip': view.props.tenantId = 8; view.props.tenantId = 7; break
    case 'type': view.props.type = scopeType('cce_pod'); break
    case 'type-refresh': view.props.type = scopeType(); break
    case 'type-code': (view.props.type as ScopeType).code = 'cce_pod'; break
    case 'missing-type': view.props.type = null; break
    case 'permission': view.props.canManage = false; break
    case 'unmount': view.unmount(); break
  }
  await settleView()
}
async function edit(view: Dialog) {
  view.state.drafts[0].match.name = ['draft-*']
  await settleView()
}
function expectNoSavedFeedback(view: Dialog) {
  expect(view.updateVisible).not.toHaveBeenCalled()
  expect(view.saved).not.toHaveBeenCalled()
  expect(view.messages.success).not.toHaveBeenCalled()
  expect(view.messages.warning).not.toHaveBeenCalled()
  expect(view.messages.error).not.toHaveBeenCalled()
}

describe('采集范围候选的异步上下文', () => {
  it('当前上下文缓存候选；当前请求失败仍保留原有空候选降级', async () => {
    const view = await dialog()
    await view.state.loadCandidates('name')
    await view.state.loadCandidates('name')
    expect(view.api.collectScopeCandidates).toHaveBeenCalledExactlyOnceWith(7, 'ecs_server', 'name', '', 200)
    expect(view.state.candidates.name).toEqual([{ value: 'current', count: 1 }])
    view.api.collectScopeCandidates.mockRejectedValueOnce(new Error('候选失败'))
    await view.state.loadCandidates('owner')
    expect(view.state.candidates.owner).toEqual([])
  })

  describe.each(['resolve', 'reject'] as const)('旧候选 %s', outcome => {
    it.each(contexts)('$change 后不能回填或清空新候选', async ({ change, active }) => {
      const view = await dialog()
      const request = deferred<ScopeCandidate[]>()
      view.api.collectScopeCandidates.mockImplementationOnce(() => request.promise)
      const pending = view.state.loadCandidates('name')
      await changeContext(view, change)
      if (active) await view.state.loadCandidates('name')
      if (outcome === 'resolve') request.resolve([{ value: 'old-tenant', count: 99 }])
      else request.reject(new Error('旧候选失败'))
      await pending
      expect(view.state.candidates.name).toEqual(active ? [{ value: 'current', count: 1 }] : undefined)
      expectNoSavedFeedback(view)
    })
  })
})

describe('采集范围预估的异步上下文', () => {
  describe.each(['resolve', 'reject'] as const)('旧预估 %s', outcome => {
    it.each(contexts)('$change 后不覆盖当前预估和加载状态', async ({ change, active }) => {
      const view = await dialog()
      const old = deferred<ScopePreview>(), current = deferred<ScopePreview>()
      view.api.previewCollectScope.mockImplementationOnce(() => old.promise)
      const pending = view.state.runPreview()
      view.api.previewCollectScope.mockImplementation(() => current.promise)
      await changeContext(view, change)
      const before = view.state.preview
      if (outcome === 'resolve') old.resolve(previewResult(9))
      else old.reject(new Error('旧预估失败'))
      await pending
      expect(view.state.preview).toEqual(before)
      const previewActive = active
      expect(view.state.previewing).toBe(previewActive)
      if (previewActive) {
        current.resolve(previewResult(2)); await settleView()
        expect(view.state.preview?.excluded).toBe(2)
        expect(view.state.previewing).toBe(false)
      }
    })
  })

  it('较新手动预估优先，旧失败不能清空成功结果', async () => {
    const view = await dialog(), old = deferred<ScopePreview>()
    view.api.previewCollectScope.mockImplementationOnce(() => old.promise)
    const pending = view.state.runPreview()
    view.api.previewCollectScope.mockResolvedValueOnce(previewResult(2))
    await view.state.runPreview()
    old.reject(new Error('较旧预估失败')); await pending
    expect(view.state.preview?.excluded).toBe(2)
    expect(view.state.previewing).toBe(false)
  })

  it('草稿变化立即作废在途预估，700ms 防抖期间不能标记为最新', async () => {
    const view = await dialog(), old = deferred<ScopePreview>()
    view.api.previewCollectScope.mockImplementationOnce(() => old.promise)
    const pending = view.state.runPreview()
    await edit(view)
    old.resolve(previewResult(9)); await pending
    expect(view.state.preview?.excluded).toBe(1)
    expect(view.state.previewStale).toBe(true)
    expect(view.state.previewing).toBe(false)
    const count = view.api.previewCollectScope.mock.calls.length
    await vi.advanceTimersByTimeAsync(699)
    expect(view.api.previewCollectScope).toHaveBeenCalledTimes(count)
    await vi.advanceTimersByTimeAsync(1)
    expect(view.api.previewCollectScope).toHaveBeenLastCalledWith(7, 'ecs_server', [rule('draft-*')])
    expect(view.state.previewStale).toBe(false)
  })

  it.each(['close', 'unmount'] as const)('%s 清理草稿自动预估定时器且拒绝新操作', async change => {
    const view = await dialog()
    await edit(view)
    await changeContext(view, change)
    const count = view.api.previewCollectScope.mock.calls.length
    await vi.advanceTimersByTimeAsync(1000)
    await view.state.loadCandidates('name')
    await view.state.runPreview()
    await view.state.save()
    expect(view.api.previewCollectScope).toHaveBeenCalledTimes(count)
    expect(view.api.collectScopeCandidates).not.toHaveBeenCalled()
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expectNoSavedFeedback(view)
  })

  it('切租户重置草稿与候选，并立即按新租户重新预估', async () => {
    const view = await dialog()
    await edit(view)
    await view.state.loadCandidates('name')
    await changeContext(view, 'tenant')
    expect(view.state.drafts[0].match.name).toEqual(['saved-8'])
    expect(view.state.candidates).toEqual({})
    expect(view.state.dirty).toBe(false)
    expect(view.api.previewCollectScope).toHaveBeenLastCalledWith(8, 'ecs_server', [rule('saved-8')])
  })
})

describe('采集范围保存的异步上下文', () => {
  it.each(contexts)('保存预估期间 $change 不写入、不打开旧确认', async ({ change }) => {
    const view = await dialog(), request = deferred<ScopePreview>()
    await edit(view)
    view.api.previewCollectScope.mockImplementationOnce(() => request.promise)
    const pending = view.state.save()
    await changeContext(view, change)
    request.resolve(previewResult(2, 3)); await pending
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expect(view.confirm).not.toHaveBeenCalled()
    expect(view.state.saving).toBe(false)
    expectNoSavedFeedback(view)
  })

  it.each(contexts)('保存确认期间 $change 不把旧规则写到新上下文', async ({ change }) => {
    const confirmation = deferred<unknown>()
    const view = await dialog({ confirm: () => confirmation.promise })
    await edit(view)
    view.api.previewCollectScope.mockResolvedValueOnce(previewResult(2, 3))
    const pending = view.state.save()
    await settleView()
    expect(view.confirm).toHaveBeenCalledOnce()
    await changeContext(view, change)
    confirmation.resolve('confirm'); await pending
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expect(view.state.saving).toBe(false)
    expectNoSavedFeedback(view)
  })

  it.each(['preview', 'confirm'] as const)('%s 期间编辑规则后取消旧保存，即使随后恢复原草稿', async phase => {
    const request = deferred<ScopePreview>(), confirmation = deferred<unknown>()
    const view = await dialog({ confirm: () => confirmation.promise })
    await edit(view)
    view.api.previewCollectScope.mockImplementationOnce(() => request.promise)
    const pending = view.state.save()
    if (phase === 'confirm') { request.resolve(previewResult(2, 3)); await settleView() }
    view.state.drafts[0].match.name = ['changed-*']
    view.state.drafts[0].match.name = ['draft-*']
    await settleView()
    if (phase === 'preview') request.resolve(previewResult(2))
    else confirmation.resolve('confirm')
    await pending
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expect(view.state.saving).toBe(false)
    expectNoSavedFeedback(view)
  })

  describe.each(['resolve', 'reject'] as const)('旧写入 %s', outcome => {
    it.each(contexts)('$change 后不关闭新弹窗、不通知、不发 saved', async ({ change }) => {
      const view = await dialog(), request = deferred<ScopeSaveResult>()
      await edit(view)
      view.api.saveCollectScope.mockImplementationOnce(() => request.promise)
      const pending = view.state.save()
      await settleView()
      expect(view.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(7, 'ecs_server', [rule('draft-*')])
      await changeContext(view, change)
      if (outcome === 'resolve') request.resolve(saveResult())
      else request.reject(new Error('旧写入失败'))
      await expect(pending).resolves.toBeUndefined()
      expect(view.state.saving).toBe(false)
      expectNoSavedFeedback(view)
    })
  })

  it.each(['preview', 'confirm'] as const)('失效的 %s 拒绝不会向新上下文抛错', async phase => {
    const request = deferred<ScopePreview>(), confirmation = deferred<unknown>()
    const view = await dialog({ confirm: () => confirmation.promise })
    await edit(view)
    view.api.previewCollectScope.mockImplementationOnce(() => request.promise)
    const pending = view.state.save()
    if (phase === 'confirm') { request.resolve(previewResult(2, 3)); await settleView() }
    await changeContext(view, 'tenant')
    if (phase === 'preview') request.reject(new Error('旧保存预估失败'))
    else confirmation.reject(new Error('旧确认失败'))
    await expect(pending).resolves.toBeUndefined()
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expectNoSavedFeedback(view)
  })

  it('旧保存的 finally 不清除新上下文的 saving，重复保存只发一次', async () => {
    const view = await dialog(), old = deferred<ScopePreview>(), current = deferred<ScopeSaveResult>()
    await edit(view)
    view.api.previewCollectScope.mockImplementationOnce(() => old.promise)
    const pending = view.state.save()
    await changeContext(view, 'tenant')
    await edit(view)
    view.api.saveCollectScope.mockImplementationOnce(() => current.promise)
    const next = view.state.save()
    await settleView()
    expect(view.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(8, 'ecs_server', [rule('draft-*')])
    old.resolve(previewResult(9)); await pending
    expect(view.state.saving).toBe(true)
    await view.state.save()
    expect(view.api.saveCollectScope).toHaveBeenCalledOnce()
    current.resolve(saveResult()); await next
    expect(view.state.saving).toBe(false)
    expect(view.saved).toHaveBeenCalledOnce()
    expect(view.updateVisible).toHaveBeenCalledExactlyOnceWith(false)
    expect(view.messages.success).toHaveBeenCalledOnce()
  })

  it('当前上下文正常确认与保存使用同一份快照并只通知一次', async () => {
    const view = await dialog()
    await edit(view)
    view.api.previewCollectScope.mockResolvedValueOnce(previewResult(2, 3))
    await view.state.save()
    expect(view.api.previewCollectScope).toHaveBeenLastCalledWith(7, 'ecs_server', [rule('draft-*')])
    expect(view.confirm).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('3 条活动告警'),
      '确认保存采集范围', expect.objectContaining({ type: 'warning' }))
    expect(view.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(7, 'ecs_server', [rule('draft-*')])
    expect(view.messages.success).toHaveBeenCalledOnce()
    expect(view.updateVisible).toHaveBeenCalledExactlyOnceWith(false)
    expect(view.saved).toHaveBeenCalledOnce()
    expect(view.state.saving).toBe(false)
  })

  it('保留 null 跟随全局、[] 租户不限制以及全局默认的保存语义', async () => {
    const follow = await dialog()
    follow.state.mode = 'follow'; await settleView(); await follow.state.save()
    expect(follow.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(7, 'ecs_server', null)
    const unrestricted = await dialog()
    unrestricted.state.drafts = []; await settleView(); await unrestricted.state.save()
    expect(unrestricted.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(7, 'ecs_server', [])
    const global = await dialog({ props: { tenantId: 0 } })
    await edit(global); await global.state.save()
    expect(global.api.saveCollectScope).toHaveBeenCalledExactlyOnceWith(0, 'ecs_server', [rule('draft-*')])
  })

  it('只读、未修改和校验失败时不保存；只读不发起预估', async () => {
    const readonly = await dialog({ props: { canManage: false } })
    await readonly.state.save(); await readonly.state.loadCandidates('name')
    expect(readonly.api.previewCollectScope).not.toHaveBeenCalled()
    expect(readonly.api.saveCollectScope).not.toHaveBeenCalled()
    expect(readonly.api.collectScopeCandidates).not.toHaveBeenCalled()
    const view = await dialog()
    await view.state.save()
    view.state.drafts[0].match.name = []; await settleView()
    expect(view.state.errors).not.toHaveLength(0)
    await view.state.save()
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
  })

  it('当前保存失败保留拒绝语义并恢复 saving；取消确认不写入或通知', async () => {
    const view = await dialog()
    await edit(view)
    const error = new Error('当前保存失败')
    view.api.saveCollectScope.mockRejectedValueOnce(error)
    await expect(view.state.save()).rejects.toBe(error)
    expect(view.state.saving).toBe(false)
    expectNoSavedFeedback(view)
    const cancelled = await dialog({ confirm: async () => { throw 'cancel' } })
    await edit(cancelled)
    cancelled.api.previewCollectScope.mockResolvedValueOnce(previewResult(2, 3))
    await cancelled.state.save()
    expect(cancelled.api.saveCollectScope).not.toHaveBeenCalled()
    expect(cancelled.state.saving).toBe(false)
    expectNoSavedFeedback(cancelled)
  })
})

describe('task:view 只读查看采集范围', () => {
  it.each([
    { label: '租户单独设置', tenantId: 7, follow: false, name: 'saved-7' },
    { label: '租户跟随全局', tenantId: 7, follow: true, name: 'global-*' },
    { label: '全局默认', tenantId: 0, follow: false, name: 'global-*' },
  ])('$label 展示已有规则，初始化和操作均不触发受 collect:manage 保护的 POST', async ({ tenantId, follow, name }) => {
    const type = scopeType()
    if (follow) {
      type.tenants['7'].setting = null
      type.tenants['7'].source = 'global'
      type.tenants['7'].rules = type.global.rules
    }
    const forbiddenPreview = vi.fn(async (): Promise<ScopePreview> => { throw new Error('403: collect:manage required') })
    const view = await dialog({ props: { tenantId, type, canManage: false }, preview: forbiddenPreview })
    expect(view.state.mode).toBe(follow ? 'follow' : 'own')
    expect(view.state.drafts[0].match.name).toEqual([name])
    expect(view.state.dirty).toBe(false)
    await view.state.runPreview()
    await view.state.loadCandidates('name')
    await view.state.save()
    await vi.advanceTimersByTimeAsync(1000)
    expect(forbiddenPreview).not.toHaveBeenCalled()
    expect(view.api.previewCollectScope).not.toHaveBeenCalled()
    expect(view.api.collectScopeCandidates).not.toHaveBeenCalled()
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expect(view.state.preview).toBeNull()
    expect(view.state.previewing).toBe(false)
    expectNoSavedFeedback(view)
  })

  it('只读关闭重开与切换租户、类型只刷新已有规则，不触发预估或写入', async () => {
    const view = await dialog({ props: { canManage: false }, preview: async () => { throw new Error('403') } })
    for (const change of ['reopen', 'tenant', 'type', 'type-refresh', 'tenant-roundtrip'] as const) {
      await changeContext(view, change)
      await vi.advanceTimersByTimeAsync(1000)
      expect(view.state.drafts[0].match.name).toEqual([`saved-${String(view.props.tenantId)}`])
      expect(view.state.dirty).toBe(false)
      expect(view.api.previewCollectScope).not.toHaveBeenCalled()
      expect(view.api.collectScopeCandidates).not.toHaveBeenCalled()
      expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    }
    expectNoSavedFeedback(view)
  })

  it('收回 collect:manage 后取消待执行的自动预估，恢复权限后才重新预估', async () => {
    const view = await dialog()
    await edit(view)
    const count = view.api.previewCollectScope.mock.calls.length
    view.props.canManage = false
    await settleView()
    await view.state.runPreview()
    await view.state.loadCandidates('name')
    await view.state.save()
    await vi.advanceTimersByTimeAsync(1000)
    expect(view.api.previewCollectScope).toHaveBeenCalledTimes(count)
    expect(view.api.collectScopeCandidates).not.toHaveBeenCalled()
    expect(view.api.saveCollectScope).not.toHaveBeenCalled()
    expect(view.state.drafts[0].match.name).toEqual(['saved-7'])
    expect(view.state.preview).toBeNull()
    view.props.canManage = true
    await settleView()
    expect(view.api.previewCollectScope).toHaveBeenCalledTimes(count + 1)
    expect(view.api.previewCollectScope).toHaveBeenLastCalledWith(7, 'ecs_server', [rule('saved-7')])
    expect(view.state.preview?.excluded).toBe(1)
  })
})
