import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef, type EffectScope } from 'vue'
import { useEcsTrend } from '../src/views/ecs/useEcsTrend'
import { useEcsInstances } from '../src/views/ecs/useEcsInstances'
import { ecsTrendChart } from '../src/views/ecs/ecs-chart'
import { metricsSourceLabel } from '../src/utils/dict'
import { ECS_PRESETS, ecsLatestReadings, ecsRangeQuery, percentText } from '../src/views/ecs/ecs-utils'
import type { EcsHistoryExporter, EcsHistoryFetcher, EcsHistoryOut, EcsInstancesFetcher, EcsInstancesOut, EcsMetricKey } from '../src/types/ecs'
import { deferred } from './businessViewHarness'
import { ecsHistory, ecsInstance } from './ecsFixtures'
const scopes: EffectScope[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
const settle = async () => { await new Promise<void>(resolve => setImmediate(resolve)); await nextTick() }
function setupTrend(metric?: () => EcsMetricKey) {
  const tenant = shallowRef<number | null>(7), id = shallowRef<number | null>(11), visible = shallowRef(true), permission = shallowRef(true)
  const requests: { query: Parameters<typeof ecsHistory>[0]; signal: AbortSignal; tenantId: number | null; result: ReturnType<typeof deferred<EcsHistoryOut>> }[] = []
  const exporter = vi.fn<EcsHistoryExporter>(async () => '真实导出.xlsx')
  const scope = effectScope(); scopes.push(scope)
  const model = scope.run(() => useEcsTrend({ tenantId: () => tenant.value, instanceId: () => id.value, visible: () => visible.value, canExport: () => permission.value, metric,
    fetcher: (_id, query, context) => { const result = deferred<EcsHistoryOut>(); requests.push({ query, ...context, result }); return result.promise }, exporter }))!
  return { tenant, id, visible, permission, requests, exporter, model, scope }
}

describe('ECS 真实趋势数据', () => {
  it('读取权限撤销会取消查询并清空历史，导出权限撤销中止正在下载的请求', async () => {
    const view = shallowRef(true), exp = shallowRef(true)
    const pending = deferred<EcsHistoryOut>(), download = deferred<string>()
    const fetcher = vi.fn<EcsHistoryFetcher>(() => pending.promise), exporter = vi.fn<EcsHistoryExporter>(() => download.promise)
    const scope = effectScope(); scopes.push(scope)
    const model = scope.run(() => useEcsTrend({ tenantId: () => 7, instanceId: () => 11, visible: () => true,
      canView: () => view.value, canExport: () => exp.value, fetcher, exporter }))!
    pending.resolve(ecsHistory(fetcher.mock.calls[0][1])); await settle()
    const exporting = model.exportExcel(); exp.value = false
    expect(exporter.mock.calls[0][2].signal.aborted).toBe(true)
    download.resolve('旧权限.xlsx'); expect(await exporting).toBe(false)
    const late = deferred<EcsHistoryOut>(); fetcher.mockReturnValueOnce(late.promise)
    const loading = model.refresh(); view.value = false
    expect(fetcher.mock.calls.at(-1)![2].signal.aborted).toBe(true)
    expect(model.history.value).toBeNull(); expect(model.appliedQuery.value).toBeNull()
    late.resolve(ecsHistory(fetcher.mock.calls.at(-1)![1])); await loading
    expect(model.history.value).toBeNull(); expect(model.canExport.value).toBe(false)
  })
  it('保留真实 0 和 null 缺口，按来源分线，百分比轴固定 0..100', () => {
    const data = ecsHistory(ecsRangeQuery('1h'))
    data.instance.latest.cpu_util!.value = 99
    const chart = ecsTrendChart(data, 'cpu_util') as { yAxis: { min: number; max: number }; series: { id: string; name: string; connectNulls: boolean; data: [number, number | null][] }[] }
    expect(chart.yAxis).toMatchObject({ min: 0, max: 100 })
    expect(chart.series).toHaveLength(2)
    expect(chart.series.map(series => series.name)).toEqual(['CPU 使用率 · AOM（已停用，历史） · 时间语义未知', 'CPU 使用率 · PROMETHEUS · 时间语义未知'])
    expect(chart.series[0].data.map(point => point[1])).toEqual([0, null, 55])
    expect(chart.series[1].data.map(point => point[1])).toEqual([null, null, 60])
    expect(chart.series.every(series => series.connectNulls === false)).toBe(true)
    expect(chart.series[0].data.some(point => point[1] === 99)).toBe(false)
    expect(percentText(null)).toBe('—'); expect(percentText(0)).toBe('0.0%')
  })
  it('没有历史时不使用最新快照生成曲线；空样本不画为零', () => {
    const history = ecsHistory(ecsRangeQuery('1h')); history.series = []
    expect((ecsTrendChart(history, 'mem_util') as { series: unknown[] }).series).toEqual([])
    const malformed = ecsHistory(ecsRangeQuery('1h')); malformed.series[0].points[0].sample_count = 0
    expect((ecsTrendChart(malformed, 'cpu_util') as { series: { data: [number, number | null][] }[] }).series[0].data[0][1]).toBeNull()
  })
  it('趋势按历史来源标签区分 ManageOne 云硬盘和 Prometheus 最满分区，不按GiB推导', () => {
    const history = ecsHistory(ecsRangeQuery('1h'))
    history.series = [
      { ...history.series[0], metric: 'disk_util', source: 'MANAGEONE', label: '云硬盘使用率', timestamp_semantics: 'source_sample' },
      { ...history.series[0], metric: 'disk_util', source: 'PROMETHEUS', label: '最满分区使用率' },
    ]
    const option = ecsTrendChart(history, 'disk_util') as { series: { name: string; data: [number, number | null][] }[] }
    expect(option.series.map(series => series.name)).toEqual([
      '云硬盘使用率 · ManageOne · 配置源时间（须现场核实）',
      '最满分区使用率 · PROMETHEUS · 时间语义未知',
    ])
    expect(option.series[0].data[0][1]).toBe(0)
  })
  it('只将ManageOne来源名映射为产品名称，AOM历史标签和其他来源标签保持现状', () => {
    expect(metricsSourceLabel('MANAGEONE')).toBe('ManageOne')
    expect(metricsSourceLabel('PROMETHEUS')).toBe('PROMETHEUS')
    expect(metricsSourceLabel('AOM')).toBe('AOM（已停用，历史）')
  })
  it('最新读数保持 CPU/内存/磁盘顺序、真实零与缺失，旧来源不冒充当前采样', () => {
    const instance = ecsInstance()
    instance.latest.cpu_util = { value: 0, source: 'PROMETHEUS', state: 'FRESH', observed_at: '2026-11-02T00:00:00Z', collected_at: '2026-11-02T00:00:01Z' }
    instance.latest.disk_util = { value: 98, source: 'AOM', state: 'FRESH', observed_at: '2026-11-01T00:00:00Z', collected_at: '2026-11-01T00:00:01Z' }
    const readings = ecsLatestReadings(instance)
    expect(readings.map(item => item.key)).toEqual(['cpu_util', 'mem_util', 'disk_util'])
    expect(readings[0]).toMatchObject({ value: 0, state: 'FRESH', source: 'PROMETHEUS', observed_at: '2026-11-02T00:00:00Z' })
    expect(readings[1]).toMatchObject({ value: null, state: 'NO_DATA', source: null, observed_at: null })
    expect(readings[2]).toMatchObject({ value: null, state: 'SOURCE_UNCONFIGURED', source: null, observed_at: null })
    expect(instance.latest.disk_util.value).toBe(98)
  })
  it.each(ECS_PRESETS)('$value 保留精确时段，桶数不超过 2160', preset => {
    const now = Date.parse('2026-11-02T00:00:00Z'), query = ecsRangeQuery(preset.value, now)
    expect(Date.parse(query.end_at) - Date.parse(query.start_at)).toBe(preset.seconds * 1000)
    expect(preset.seconds / query.interval_sec).toBeLessThanOrEqual(2160)
    expect(query.end_at).toBe('2026-11-02T00:00:00.000Z')
  })
  it('切换实例和租户立即清空旧数据、abort，并拒绝迟到结果和错误', async () => {
    const { tenant, id, requests, model } = setupTrend()
    id.value = 22
    expect(requests[0].signal.aborted).toBe(true)
    requests[1].result.resolve(ecsHistory(requests[1].query, 22))
    await settle()
    requests[0].result.resolve(ecsHistory(requests[0].query, 11))
    await settle()
    expect(model.history.value?.instance.id).toBe(22)
    tenant.value = 8
    expect(model.history.value).toBeNull()
    expect(model.canExport.value).toBe(false)
    requests[2].result.reject(new Error('当前失败')); await settle()
    expect(model.error.value).toBe('当前失败')
    expect(model.loading.value).toBe(false)
  })
  it('旧错误不覆盖新查询，关闭抽屉后迟到数据不回填', async () => {
    const { id, visible, requests, model } = setupTrend()
    id.value = 22
    requests[0].result.reject(new Error('旧错误')); await settle()
    expect(model.error.value).toBe('')
    visible.value = false
    requests[1].result.resolve(ecsHistory(requests[1].query, 22)); await settle()
    expect(model.history.value).toBeNull(); expect(model.loading.value).toBe(false)
  })
  it('导出复用已查询时间，禁止只读用户导出、禁止加载中导出', async () => {
    const { requests, model, exporter, permission } = setupTrend()
    expect(await model.exportExcel()).toBe(false)
    requests[0].result.resolve(ecsHistory(requests[0].query)); await settle()
    expect(await model.exportExcel()).toBe(true)
    expect(exporter.mock.calls[0]).toEqual([11, requests[0].query, expect.objectContaining({ tenantId: 7 })])
    permission.value = false
    expect(model.canExport.value).toBe(false)
    expect(await model.exportExcel()).toBe(false)
    expect(exporter).toHaveBeenCalledTimes(1)
  })
  it('切换指标中止旧查询，导出使用新指标和已经成功查询的时段', async () => {
    const metric = shallowRef<EcsMetricKey>('cpu_util')
    const { requests, model, exporter } = setupTrend(() => metric.value)
    expect(requests[0].query.metrics).toEqual(['cpu_util'])
    metric.value = 'mem_util'
    expect(requests[0].signal.aborted).toBe(true)
    expect(requests[1].query.metrics).toEqual(['mem_util'])
    expect(await model.exportExcel()).toBe(false)
    requests[1].result.resolve(ecsHistory(requests[1].query)); await settle()
    requests[0].result.reject(new Error('旧 CPU 查询失败')); await settle()
    expect(model.error.value).toBe('')
    expect(await model.exportExcel()).toBe(true)
    expect(exporter.mock.calls[0]).toEqual([11, requests[1].query, expect.objectContaining({ tenantId: 7 })])
    expect(exporter.mock.calls[0][1].metrics).not.toBe(model.appliedQuery.value!.metrics)
  })
  it.each(['preset', 'tenant', 'close', 'dispose'])('%s 变化会取消正在进行的导出，迟到响应不报告成功', async change => {
    const { requests, model, exporter, tenant, visible, scope } = setupTrend()
    requests[0].result.resolve(ecsHistory(requests[0].query)); await settle()
    const pending = deferred<string>()
    exporter.mockImplementationOnce(() => pending.promise)
    const exporting = model.exportExcel()
    expect(model.exporting.value).toBe(true)
    if (change === 'preset') model.preset.value = '7d'
    else if (change === 'tenant') tenant.value = 8
    else if (change === 'close') visible.value = false
    else scope.stop()
    expect(exporter.mock.calls[0][2].signal.aborted).toBe(true)
    expect(model.exporting.value).toBe(false)
    pending.resolve('旧范围.xlsx')
    expect(await exporting).toBe(false)
    expect(model.exportError.value).toBe('')
  })
})

describe('ECS 监控列表范围', () => {
  it('租户切换中止前页查询、清空筛选，迟到结果不会串租户', async () => {
    const tenant = shallowRef<number | null>(7)
    const requests: { result: ReturnType<typeof deferred<EcsInstancesOut>>; tenantId: number | null; signal: AbortSignal }[] = []
    const scope = effectScope(); scopes.push(scope)
    const model = scope.run(() => useEcsInstances({ tenantId: () => tenant.value, fetcher: (_query, context) => { const result = deferred<EcsInstancesOut>(); requests.push({ result, ...context }); return result.promise } }))!
    model.filters.keyword = '旧实例'; model.page.value = 3
    tenant.value = 8
    expect(requests[0].signal.aborted).toBe(true)
    expect(model.items.value).toEqual([]); expect(model.page.value).toBe(1); expect(model.filters.keyword).toBe('')
    requests[1].result.resolve({ items: [ecsInstance(22, 8)], page: 1, page_size: 20, total: 1 }); await settle()
    requests[0].result.resolve({ items: [ecsInstance(11, 7)], page: 3, page_size: 20, total: 1 }); await settle()
    expect(model.items.value.map(item => item.tenant_id)).toEqual([8])
  })
  it('查询和重置回到首页，保留 false 过滤和用户所选分页大小', async () => {
    const fetcher = vi.fn<EcsInstancesFetcher>(async query => ({ items: [], total: 0, page: query.page, page_size: query.page_size }))
    const scope = effectScope(); scopes.push(scope)
    const model = scope.run(() => useEcsInstances({ tenantId: () => null, fetcher }))!
    await settle()
    await model.changePage(3)
    model.filters.keyword = ' vm-11 '; model.filters.critical = 'no'; model.filters.monitored = 'no'
    await model.search()
    expect(fetcher.mock.calls[fetcher.mock.calls.length - 1]).toEqual([{ page: 1, page_size: 20, keyword: 'vm-11', critical: false, monitored: false }, expect.objectContaining({ tenantId: null })])
    await model.changePageSize(50)
    await model.changePage(2)
    await model.resetFilters()
    expect(fetcher.mock.calls[fetcher.mock.calls.length - 1][0]).toEqual({ page: 1, page_size: 50, keyword: undefined, critical: undefined, monitored: undefined })
    expect(model.filters).toEqual({ keyword: '', critical: '', monitored: '' })
  })
})
