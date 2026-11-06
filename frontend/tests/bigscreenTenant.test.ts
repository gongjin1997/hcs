import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { Alarm, Summary } from '../src/types'
import { deferred, mountBusinessScript, settleView } from './businessViewHarness'

const unmounts: (() => void)[] = []
afterEach(() => {
  unmounts.splice(0).forEach(unmount => unmount())
  vi.unstubAllGlobals()
})

interface BigScreenState {
  data?: Summary
  feed: { key: string; alarm: Alarm }[]
  loadError: string
  load(): Promise<void>
  onTenant(id: number): void
}

const summary = (tenant: number): Summary => ({
  health_score: tenant,
  top_alarms: [{ id: tenant, tenant_id: tenant } as Alarm],
} as Summary)

function bigscreen() {
  vi.stubGlobal('window', { removeEventListener: vi.fn() })
  const fetchSummary = vi.fn<() => Promise<Summary>>().mockResolvedValue(summary(0))
  const reopen = vi.fn()
  let app: { tenantId: number | null; tenantSeq: number }
  const setTenant = vi.fn((id: number | null) => {
    if (id === app.tenantId) return
    app.tenantId = id
    app.tenantSeq++
  })
  const view = mountBusinessScript<BigScreenState>('bigscreen/BigScreenView.vue', {
    api: { fetchSummary },
    app: { tenantId: null, tenantSeq: 0, tenants: [{ id: 1, name: '租户一' }, { id: 2, name: '租户二' }], setTenant },
    dependencies: { '@/composables/useAlarmStream': { useAlarmStream: () => ({ connected: ref(true), reopen }) } },
  })
  app = view.appState as unknown as typeof app
  unmounts.push(view.unmount)
  return { ...view, fetchSummary, reopen, setTenant }
}

describe('大屏租户切换', () => {
  it('仅允许切换到可见租户或全部租户', () => {
    const { state, setTenant } = bigscreen()
    state.onTenant(1)
    expect(setTenant).toHaveBeenLastCalledWith(1)
    state.onTenant(0)
    expect(setTenant).toHaveBeenLastCalledWith(null)
    state.onTenant(999)
    expect(setTenant).toHaveBeenCalledTimes(2)
  })

  it('切换时立即清除旧统计和告警，并重新订阅', async () => {
    const { state, fetchSummary, reopen } = bigscreen()
    fetchSummary.mockResolvedValueOnce(summary(1))
    await state.load()
    expect(state.feed[0].alarm.id).toBe(1)
    const next = deferred<Summary>()
    fetchSummary.mockReturnValueOnce(next.promise)
    state.onTenant(2)
    expect(state.data).toBeUndefined()
    expect(state.feed).toEqual([])
    expect(reopen).toHaveBeenCalledOnce()
    next.resolve(summary(2))
    await settleView()
    expect(state.data?.health_score).toBe(2)
    expect(state.feed[0].alarm.id).toBe(2)
  })

  it('连续切换时，旧租户的晚返回结果不会覆盖新租户', async () => {
    const { state, fetchSummary, reopen } = bigscreen()
    const first = deferred<Summary>(), second = deferred<Summary>()
    fetchSummary.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    state.onTenant(1)
    state.onTenant(2)
    second.resolve(summary(2))
    await settleView()
    first.resolve(summary(1))
    await settleView()
    expect(state.data?.health_score).toBe(2)
    expect(state.feed.map(item => item.alarm.id)).toEqual([2])
    expect(reopen).toHaveBeenCalledTimes(2)
  })

  it('旧租户请求失败不污染新租户的状态', async () => {
    const { state, fetchSummary } = bigscreen()
    const first = deferred<Summary>()
    fetchSummary.mockReturnValueOnce(first.promise).mockResolvedValueOnce(summary(2))
    state.onTenant(1)
    state.onTenant(2)
    await settleView()
    first.reject(new Error('旧租户请求超时'))
    await settleView()
    expect(state.loadError).toBe('')
    expect(state.data?.health_score).toBe(2)
  })

  it('切回全部租户会重新加载；重复选择不重复请求', async () => {
    const { state, fetchSummary, reopen } = bigscreen()
    state.onTenant(1)
    await settleView()
    state.onTenant(1)
    expect(fetchSummary).toHaveBeenCalledOnce()
    state.onTenant(0)
    await settleView()
    expect(fetchSummary).toHaveBeenCalledTimes(2)
    expect(reopen).toHaveBeenCalledTimes(2)
  })
})
