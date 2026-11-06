import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, reactive } from 'vue'
import type { GaussDiskApi, GaussDiskHistoryOut, GaussMetricSeries } from '../src/types/gaussdb'
import { useGaussDiskHistory } from '../src/views/gaussdb/useGaussDiskHistory'
import { gaussDiskChart, gaussReportChart } from '../src/views/gaussdb/gauss-chart'
import { gaussInstance } from './gaussFixtures'
import { deferred } from './businessViewHarness'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
const settle = () => new Promise<void>(resolve => setImmediate(resolve))
function setup() {
  const state = reactive({ instance: gaussInstance(), tenant: 7 as number | null, view: true, export: true })
  const api: GaussDiskApi = {
    history: vi.fn(async (_id, query) => ({ ...query, instance: { ...state.instance }, source: 'GAUSSDB', sampling_source: 'LOCAL_COLLECTOR',
      status: 'PARTIAL', queried_at: query.end_at, warning: null,
      points: [{ timestamp: query.start_at, observed_at: query.start_at, used_gb: 0, total_gb: 100, usage_percent: 0 },
        { timestamp: new Date(Date.parse(query.start_at) + 300000).toISOString(), observed_at: null, used_gb: null, total_gb: null, usage_percent: null }],
    })),
    export: vi.fn(async () => '存储趋势.xlsx'),
  }
  const scope = effectScope(); scopes.push(scope)
  const model = scope.run(() => useGaussDiskHistory({ instance: () => state.instance, tenantId: () => state.tenant,
    canView: () => state.view, canExport: () => state.export, api, now: () => Date.parse('2026-11-02T02:00:00Z') }))!
  return { state, api, model }
}

describe('GaussDB 本地存储趋势', () => {
  it('只查询所选实例本地历史，导出使用已查询的同一范围', async () => {
    const { model, api } = setup(); await settle()
    expect(api.history).toHaveBeenCalledWith(11, { start_at: '2026-11-01T02:00:00.000Z', end_at: '2026-11-02T02:00:00.000Z', period_sec: 300 }, expect.objectContaining({ tenantId: 7 }))
    expect(model.canExport.value).toBe(true)
    await model.exportExcel()
    expect(api.export).toHaveBeenCalledWith(11, model.applied.value, expect.objectContaining({ tenantId: 7 }))
    model.setRange([new Date('2026-11-02T00:00:00Z'), new Date('2026-11-02T01:00:00Z')])
    expect(model.history.value).toBeNull(); expect(model.canExport.value).toBe(false)
  })
  it('缺失点不补零，零值保留，存储配置量和已用量分别绘制', async () => {
    const { model } = setup(); await settle()
    const usage = gaussDiskChart(model.history.value!, 'usage')
    const series = usage.series as { data: unknown[][]; connectNulls: boolean }[]
    expect(series[0].data.map(point => point[1])).toEqual([0, null]); expect(series[0].connectNulls).toBe(false)
    const capacity = gaussDiskChart(model.history.value!, 'capacity')
    expect((capacity.series as { data: unknown[][] }[]).map(item => item.data[0][1])).toEqual([0, 100])
    expect(usage.tooltip).toMatchObject({ renderMode: 'richText' })
  })
  it('切租户中止在途查询，迟到数据不能恢复旧实例趋势', async () => {
    const { state, model, api } = setup(); await settle()
    const old = model.history.value!, pending = deferred<GaussDiskHistoryOut>()
    vi.mocked(api.history).mockReturnValueOnce(pending.promise)
    const querying = model.query(); const signal = vi.mocked(api.history).mock.calls.at(-1)![2].signal
    state.tenant = 8; state.instance = gaussInstance(12, 8)
    pending.resolve(old); await querying; await settle()
    expect(signal.aborted).toBe(true); expect(model.history.value?.instance.tenant_id).toBe(8)
  })
  it('响应实例或项目不匹配时不展示也不能导出', async () => {
    const { model, api } = setup(); await settle()
    const wrong = { ...model.history.value!, instance: { ...gaussInstance(), project_ref: 999 } }
    vi.mocked(api.history).mockResolvedValueOnce(wrong)
    await model.query()
    expect(model.history.value).toBeNull(); expect(model.error.value).toContain('范围不匹配'); expect(model.canExport.value).toBe(false)
  })
  it('无查看权限不请求，撤销导出权限中止导出', async () => {
    const { state, model, api } = setup(); await settle()
    const pending = deferred<string>(); vi.mocked(api.export).mockReturnValueOnce(pending.promise)
    const exporting = model.exportExcel(); const signal = vi.mocked(api.export).mock.calls[0][2].signal
    state.export = false; expect(signal.aborted).toBe(true)
    pending.resolve('过期.xlsx'); expect(await exporting).toBeNull()
    state.view = false; vi.mocked(api.history).mockClear(); await model.query()
    expect(api.history).not.toHaveBeenCalled(); expect(model.history.value).toBeNull()
  })
  it('无效日期与超过31天范围不请求，预设刷新时间范围', async () => {
    const { model, api } = setup(); await settle(); vi.mocked(api.history).mockClear()
    model.setRange([new Date('bad'), new Date()]); await model.query(); expect(api.history).not.toHaveBeenCalled()
    model.setRange([new Date('2026-01-01'), new Date('2026-11-02')]); await model.query(); expect(model.error.value).toContain('31 天')
    await model.setPreset(168); expect(vi.mocked(api.history).mock.calls[0][1].period_sec).toBe(3600)
  })
})

describe('GaussDB 报告图表口径', () => {
  it('保持实际单位和采样日期，缺失断线、非法数值不绘制、无HTML tooltip', () => {
    const metric: GaussMetricSeries = { metric: 'memory', check_name: null, check_item: null, check_status: null,
      name: '<img src=x>', unit: 'GB', node_name: 'node', node_ip: null, min: 0, max: 12, average: 6,
      points: [{ observed_at: '2025-01-01T01:00:00Z', value: 0 }, { observed_at: '2025-01-01T01:05:00Z', value: null },
        { observed_at: '2025-01-01T01:10:00Z', value: Number.POSITIVE_INFINITY }, { observed_at: 'invalid', value: 90 }] }
    const chart = gaussReportChart(metric)
    expect(chart.yAxis).toMatchObject({ name: 'GB' })
    expect(chart.tooltip).toMatchObject({ renderMode: 'richText' })
    const series = chart.series as { data: unknown[][]; connectNulls: boolean }[]
    expect(series[0].data.map(point => point[1])).toEqual([0, null, null]); expect(series[0].connectNulls).toBe(false)
    expect(series[0].data[0][0]).toBe(Date.parse('2025-01-01T01:00:00Z'))
  })
})
