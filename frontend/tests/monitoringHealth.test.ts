import * as workspaceNavigation from '../src/config/workspace-navigation'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { Summary } from '../src/types'
import { scoreColor } from '../src/utils/dict'
import { mountBusinessScript } from './businessViewHarness'

const unmounts: (() => void)[] = []
afterEach(() => {
  unmounts.splice(0).forEach(unmount => unmount())
  vi.unstubAllGlobals()
})

interface HealthView {
  load(): Promise<void>
  gaugeOption: { series: { progress: { show: boolean }; detail: { formatter: string }; data: { value: number; name: string }[] }[] }
}

describe.each(['dashboard/DashboardView.vue', 'bigscreen/BigScreenView.vue'])('健康分显示 %s', file => {
  it('没有有效巡检数据时显示空值，不显示满分或严重异常', async () => {
    vi.stubGlobal('window', { removeEventListener: vi.fn() })
    const fetchSummary = vi.fn().mockResolvedValue({ health_score: null, top_alarms: [] } as unknown as Summary)
    const view = mountBusinessScript<HealthView>(file, {
      api: { fetchSummary },
      dependencies: { '@/config/workspace-navigation': workspaceNavigation, '@/composables/useAlarmStream': { useAlarmStream: () => ({ connected: ref(false), reopen: vi.fn() }) } },
    })
    unmounts.push(view.unmount)
    await view.state.load()
    const series = view.state.gaugeOption.series[0]
    expect(series.progress.show).toBe(false)
    expect(series.detail.formatter).toBe('—')
    expect(series.data[0].name).toBe('暂无有效巡检数据')
  })

  it('真实零分仍显示严重异常，不与无数据混淆', async () => {
    vi.stubGlobal('window', { removeEventListener: vi.fn() })
    const view = mountBusinessScript<HealthView>(file, {
      api: { fetchSummary: vi.fn().mockResolvedValue({ health_score: 0, top_alarms: [] }) },
      dependencies: { '@/config/workspace-navigation': workspaceNavigation, '@/composables/useAlarmStream': { useAlarmStream: () => ({ connected: ref(false), reopen: vi.fn() }) } },
    })
    unmounts.push(view.unmount)
    await view.state.load()
    const series = view.state.gaugeOption.series[0]
    expect(series.progress.show).toBe(true)
    expect(series.detail.formatter).toBe('{value}')
    expect(series.data[0]).toEqual({ value: 0, name: '严重异常' })
    expect(scoreColor(null)).not.toBe(scoreColor(0))
  })
})
