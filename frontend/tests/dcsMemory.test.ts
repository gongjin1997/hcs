import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef, type EffectScope } from 'vue'
import type {
  DcsHistoryOut, DcsHistoryQuery, DcsInstance, DcsInstancesOut, DcsInstancesQuery,
  DcsIntervalMinutes, DcsMemoryPoint, DcsRequestContext,
} from '../src/types/dcs'
import { useDcsMemory } from '../src/views/dcs/useDcsMemory'
import { DCS_SOURCE_LABELS, historyQuery, metricPage, metricText } from '../src/views/dcs/dcs-utils'
import { memoryChart } from '../src/views/dcs/memory-chart'

const now = Date.parse('2026-10-27T02:03:04.123Z')
const scopes: EffectScope[] = []
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(now) })
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.clearAllTimers(); vi.useRealTimers() })
const settle = async () => { await Promise.resolve(); await nextTick() }

function instance(id = 1, tenantId: number | null = 7): DcsInstance {
  return { id, resource_id: `dcs-${id}`, name: `缓存 ${id}`, tenant_id: tenantId, tenant_name: `租户 ${tenantId}`,
    project_ref: null, project_name: null, region: null, status_raw: 'RUNNING', memory_usage: null,
    used_memory_mb: null, max_memory_mb: null, snapshot_at: null }
}
function point(at: string, value: number | null = 65): DcsMemoryPoint {
  return { at, memory_usage: value, memory_max: value == null ? null : value + 5,
    used_memory_mb: value == null ? null : 512, max_memory_mb: value == null ? null : 1024, sample_count: value == null ? 0 : 2 }
}
function history(query: DcsHistoryQuery, tenantId: number | null = 7): DcsHistoryOut {
  return { start_at: query.start_at, end_at: query.end_at, interval_minutes: query.interval_minutes,
    timezone: 'UTC', generated_at: new Date(now).toISOString(), demo: false,
    series: query.resource_ids.map(id => ({ instance: instance(id, tenantId), source: 'LOCAL', status: 'AVAILABLE', warning: null,
      valid_points: 2, total_points: 2, summary: { average_usage: 65, peak_usage: 70, latest_usage: 65, latest_at: query.start_at },
      points: [point(query.start_at), point(new Date(Date.parse(query.start_at) + query.interval_minutes * 60000).toISOString())] })) }
}
function pending<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup() {
  const tenant = shallowRef<number | null>(7)
  const project = shallowRef<number | null>(null)
  const instances: { query: DcsInstancesQuery; context: DcsRequestContext; result: ReturnType<typeof pending<DcsInstancesOut>> }[] = []
  const histories: { query: DcsHistoryQuery; context: DcsRequestContext; result: ReturnType<typeof pending<DcsHistoryOut>> }[] = []
  const exports: { query: DcsHistoryQuery; context: DcsRequestContext; result: ReturnType<typeof pending<string>> }[] = []
  const scope = effectScope()
  scopes.push(scope)
  const model = scope.run(() => useDcsMemory({
    tenantId: () => tenant.value, projectRef: () => project.value,
    instancesFetcher(query, context) { const result = pending<DcsInstancesOut>(); instances.push({ query, context, result }); return result.promise },
    historyFetcher(query, context) { const result = pending<DcsHistoryOut>(); histories.push({ query, context, result }); return result.promise },
    exporter(query, context) { const result = pending<string>(); exports.push({ query, context, result }); return result.promise },
  }))!
  async function finishInstances(index = 0, items = [instance(1), instance(2)], total = items.length) {
    const query = instances[index].query
    instances[index].result.resolve({ items, total, page: query.page, page_size: query.page_size })
    await settle()
  }
  async function finishHistory(index = 0, result = history(histories[index].query, histories[index].context.tenantId)) {
    histories[index].result.resolve(result)
    await settle()
  }
  async function ready() { await finishInstances(); await finishHistory() }
  return { model, instances, histories, exports, tenant, project, scope, finishInstances, finishHistory, ready }
}

describe('DCS 实例和八类空态/错误态', () => {
  it.each(['AOM', 'LOCAL', 'CES', 'NONE'] as const)('来源 %s 原样保留，缺失时段不补0、不从快照伪造', async source => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    const data = history(histories[0].query)
    const series = data.series[0]
    series.source = source
    series.status = 'PARTIAL'
    series.warning = source === 'LOCAL' ? '仅包含已保存的 DCS 接口采样' : '来源限制保留'
    series.points[0] = point(series.points[0].at, null)
    series.valid_points = 1
    series.instance.memory_usage = 99
    const original = structuredClone(data)
    await finishHistory(0, data)
    expect(model.history.value).toEqual(original)
    expect(model.tableRows.value[0]).toMatchObject({ source, memory_usage: null, memory_max: null, sample_count: 0 })
    expect(model.tableRows.value[1].source).toBe(source)
    const line = (memoryChart(model.history.value!, 'memory_usage').series as { data: [number, number | null][]; connectNulls: boolean }[])[0]
    expect(line.data[0]).toEqual([Date.parse(series.points[0].at), null])
    expect(line.connectNulls).toBe(false)
    expect(DCS_SOURCE_LABELS[source]).toBe({ AOM: 'AOM（已停用，历史）', LOCAL: 'DCS 接口采样', CES: 'CES（历史兼容）', NONE: '无数据源' }[source])
  })

  it('加载首个可见实例并查询最近一天，而不使用快照值填趋势', async () => {
    const { model, instances, histories, finishInstances, finishHistory } = setup()
    expect(model.instancesLoading.value).toBe(true)
    expect(model.history.value).toBeNull()
    expect(instances[0].query).toMatchObject({ page: 1, page_size: 50 })
    expect(instances[0].context.tenantId).toBe(7)
    await finishInstances(0, [instance(3), instance(1)])
    expect(model.selectedIds.value).toEqual([3])
    expect(histories[0].query).toEqual({ resource_ids: [3], interval_minutes: 5,
      start_at: new Date(now - 86400000).toISOString(), end_at: new Date(now).toISOString() })
    expect(model.history.value).toBeNull()
    await finishHistory()
    expect(model.history.value?.series[0].instance.id).toBe(3)
  })

  it('空态1：当前范围无实例，不发历史请求', async () => {
    const { model, histories, finishInstances } = setup()
    await finishInstances(0, [])
    expect(model.instancesTotal.value).toBe(0)
    expect(model.selectedIds.value).toEqual([])
    expect(histories).toHaveLength(0)
    expect(model.canExport.value).toBe(false)
  })

  it('空态2：实例加载失败可重试，失败不伪装成指标零值', async () => {
    const { model, instances, histories, finishInstances } = setup()
    instances[0].result.reject(new Error('实例连接失败'))
    await settle()
    expect(model.instancesError.value).toBe('实例连接失败')
    expect(model.instancesLoading.value).toBe(false)
    expect(histories).toHaveLength(0)
    const retry = model.loadInstances()
    await finishInstances(1)
    await retry
    expect(model.instancesError.value).toBe('')
    expect(histories).toHaveLength(1)
  })

  it('空态3：搜索无匹配不丢失已选实例或改变历史筛选', async () => {
    const { model, instances, histories, ready, finishInstances } = setup()
    await ready()
    const previous = model.history.value
    model.searchInstances(' no-match ')
    await vi.advanceTimersByTimeAsync(250)
    expect(instances[1].query.keyword).toBe('no-match')
    await finishInstances(1, [])
    expect(model.instancesTotal.value).toBe(0)
    expect(model.instanceOptions.value.map(item => item.id)).toEqual([1])
    expect(model.history.value).toBe(previous)
    expect(histories).toHaveLength(1)
  })

  it('空态4：手动清空选择后清除旧指标，刷新也不重新默认选中', async () => {
    const { model, histories, ready, finishInstances } = setup()
    await ready()
    await model.setSelectedIds([])
    expect(model.history.value).toBeNull()
    expect(model.appliedQuery.value).toBeNull()
    expect(model.totalRows.value).toBe(0)
    expect(model.canExport.value).toBe(false)
    const refresh = model.refresh()
    await finishInstances(1)
    await refresh
    expect(model.selectedIds.value).toEqual([])
    expect(histories).toHaveLength(1)
  })

  it('空态5：无序列响应不编造指标且不允许导出旧筛选', async () => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    await finishHistory(0, { ...history(histories[0].query), series: [] })
    expect(model.history.value?.series).toEqual([])
    expect(model.totalRows.value).toBe(0)
    expect(model.canExport.value).toBe(false)
  })

  it('空态6：全缺失点保持 null 和来源限制，可导出真实的空窗口', async () => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    const result = history(histories[0].query)
    const series = result.series[0]
    series.source = 'LOCAL'; series.status = 'NO_DATA'; series.warning = '本地采集无保存数据'; series.valid_points = 0
    series.summary = { average_usage: null, peak_usage: null, latest_usage: null, latest_at: null }
    series.points = series.points.map(item => point(item.at, null))
    result.demo = true
    await finishHistory(0, result)
    expect(model.tableRows.value.map(row => row.memory_usage)).toEqual([null, null])
    expect(model.tableRows.value.map(row => row.sample_count)).toEqual([0, 0])
    expect(model.history.value?.series[0].warning).toBe('本地采集无保存数据')
    expect(model.history.value?.demo).toBe(true)
    expect(model.canExport.value).toBe(true)
  })

  it('空态7：指标查询失败无旧值，重试后恢复', async () => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    histories[0].result.reject(new Error('指标源超时'))
    await settle()
    expect(model.historyLoading.value).toBe(false)
    expect(model.historyError.value).toBe('指标源超时')
    expect(model.history.value).toBeNull()
    const retry = model.queryHistory()
    expect(model.historyError.value).toBe('')
    await finishHistory(1)
    await retry
    expect(model.history.value).not.toBeNull()
  })

  it('空态8：取消查询后忽略迟到数据，不显示成功或可导出状态', async () => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    model.cancelHistory()
    expect(histories[0].context.signal.aborted).toBe(true)
    expect(model.cancelled.value).toBe(true)
    await finishHistory()
    expect(model.history.value).toBeNull()
    expect(model.canExport.value).toBe(false)
    expect(model.historyError.value).toBe('')
    expect(model.historyLoading.value).toBe(false)
  })
})

describe('实例分页、选择和过期请求', () => {
  it('实例分页追加并去重，历史查询不受加载更多影响', async () => {
    const { model, instances, histories, finishInstances } = setup()
    await finishInstances(0, Array.from({ length: 50 }, (_, index) => instance(index + 1)), 51)
    expect(model.hasMoreInstances.value).toBe(true)
    const more = model.loadMoreInstances()
    expect(instances[1].query.page).toBe(2)
    await finishInstances(1, [instance(50), instance(51)], 51)
    await more
    expect(model.instanceOptions.value).toHaveLength(51)
    expect(model.hasMoreInstances.value).toBe(false)
    expect(histories).toHaveLength(1)
  })

  it('最多20个、拒绝未知ID，并复制外部选择数组', async () => {
    const { model, histories, finishInstances } = setup()
    await finishInstances(0, Array.from({ length: 21 }, (_, index) => instance(index + 1)))
    const ids = Array.from({ length: 20 }, (_, index) => index + 1)
    const query = model.setSelectedIds(ids)
    ids.push(21)
    expect(model.selectedIds.value).toHaveLength(20)
    expect(histories[1].query.resource_ids).toHaveLength(20)
    await model.setSelectedIds(ids)
    expect(model.validationError.value).toContain('20')
    expect(histories).toHaveLength(2)
    histories[1].result.resolve(history(histories[1].query))
    await query
    expect(model.history.value).toBeNull()
    await model.setSelectedIds([999])
    expect(model.validationError.value).toContain('不可用')
    expect(histories).toHaveLength(2)
  })

  it.each(['resolve', 'reject'] as const)('实例切换后旧请求 %s 不影响新请求', async outcome => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    const next = model.setSelectedIds([2])
    expect(histories[0].context.signal.aborted).toBe(true)
    if (outcome === 'resolve') await finishHistory(0)
    else { histories[0].result.reject(new Error('旧错误')); await settle() }
    expect(model.history.value).toBeNull()
    expect(model.historyLoading.value).toBe(true)
    expect(model.historyError.value).toBe('')
    await finishHistory(1)
    await next
    expect(model.history.value?.series[0].instance.id).toBe(2)
  })

  it('最新查询完成后，旧成功仍不能回填', async () => {
    const { model, finishInstances, finishHistory } = setup()
    await finishInstances()
    const next = model.setSelectedIds([2])
    await finishHistory(1); await next; await finishHistory(0)
    expect(model.history.value?.series[0].instance.id).toBe(2)
  })

  it.each(['tenant', 'project'] as const)('切换%s同步清空选择、指标、导出并取消旧请求', async change => {
    const { model, instances, histories, tenant, project, ready, finishInstances } = setup()
    await ready()
    const current = model.queryHistory()
    if (change === 'tenant') tenant.value = 8
    else project.value = 19
    expect(histories[1].context.signal.aborted).toBe(true)
    expect(model.selectedIds.value).toEqual([])
    expect(model.history.value).toBeNull()
    expect(model.instanceOptions.value).toEqual([])
    expect(model.canExport.value).toBe(false)
    expect(instances[1].context).toMatchObject({ tenantId: tenant.value, projectRef: project.value })
    histories[1].result.resolve(history(histories[1].query))
    await current
    const item = { ...instance(101, tenant.value), project_ref: project.value }
    await finishInstances(1, [item])
    expect(model.selectedIds.value).toEqual([101])
    expect(histories[2].context.tenantId).toBe(tenant.value)
  })

  it('离开页面清除防抖与请求，重挂从自身首个实例开始', async () => {
    const previous = setup()
    await previous.finishInstances()
    previous.model.searchInstances('pending')
    previous.scope.stop()
    expect(previous.histories[0].context.signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(300)
    await previous.finishHistory()
    await previous.model.refresh()
    expect(previous.instances).toHaveLength(1)
    expect(previous.model.history.value).toBeNull()
    const current = setup()
    await current.finishInstances(0, [instance(9)])
    expect(current.model.selectedIds.value).toEqual([9])
  })

  it('迟到的实例列表不会覆盖新搜索', async () => {
    const { model, instances, finishInstances } = setup()
    model.searchInstances('new')
    expect(instances[0].context.signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(250)
    await finishInstances(1, [instance(9)])
    await finishInstances(0, [instance(1)])
    expect(model.selectedIds.value).toEqual([9])
    expect(model.instances.value.map(item => item.id)).toEqual([9])
  })
})

describe('范围、粒度和响应边界', () => {
  it.each([1, 2, 7] as const)('最近%d天保持实际起止时间，不按整点重对齐', day => {
    const query = historyQuery([1], `${day}d`, [null, null], 5, now)
    expect(Date.parse(query.end_at)).toBe(now)
    expect(Date.parse(query.start_at)).toBe(now - day * 86400000)
    expect(query.start_at).toContain('03:04.123Z')
  })

  it.each([5, 10, 15, 30, 60] as const)('支持%d分钟聚合，自定义带时区时间转为明确ISO', interval => {
    const query = historyQuery([1, 1, 2], 'custom', [new Date('2026-10-26T08:03:04+08:00'), new Date('2026-10-27T08:03:04+08:00')], interval, now)
    expect(query.interval_minutes).toBe(interval)
    expect(query.resource_ids).toEqual([1, 2])
    expect(query.start_at).toBe('2026-10-26T00:03:04.000Z')
  })

  it.each([
    { ids: [], dates: [new Date(now - 1000), new Date(now)], interval: 5 },
    { ids: [0], dates: [new Date(now - 1000), new Date(now)], interval: 5 },
    { ids: [1.5], dates: [new Date(now - 1000), new Date(now)], interval: 5 },
    { ids: Array.from({ length: 21 }, (_, index) => index + 1), dates: [new Date(now - 1000), new Date(now)], interval: 5 },
    { ids: [1], dates: [null, new Date(now)], interval: 5 },
    { ids: [1], dates: [new Date('invalid'), new Date(now)], interval: 5 },
    { ids: [1], dates: [new Date(now), new Date(now)], interval: 5 },
    { ids: [1], dates: [new Date(now + 1), new Date(now)], interval: 5 },
    { ids: [1], dates: [new Date(now - 31 * 86400000 - 1), new Date(now)], interval: 5 },
    { ids: [1], dates: [new Date(now - 1000), new Date(now)], interval: 7 },
  ])('拒绝无效选择/区间/粒度：%j', ({ ids, dates, interval }) => {
    expect(() => historyQuery(ids, 'custom', dates as [Date | null, Date | null], interval as DcsIntervalMinutes, now)).toThrow()
  })

  it('最长31天边界允许，切换无效区间立即清旧值且不发API', async () => {
    expect(historyQuery([1], 'custom', [new Date(now - 31 * 86400000), new Date(now)], 60, now).end_at).toBe(new Date(now).toISOString())
    const { model, histories, ready } = setup()
    await ready()
    void model.setRangePreset('custom')
    await model.setCustomDate(0, new Date(now - 32 * 86400000))
    expect(model.validationError.value).toContain('31')
    expect(model.history.value).toBeNull()
    expect(model.canExport.value).toBe(false)
    expect(histories).toHaveLength(2)
    expect(histories[1].context.signal.aborted).toBe(true)
  })

  it('聚合切换清旧值、取消旧请求并用新粒度查询', async () => {
    const { model, histories, ready, finishHistory } = setup()
    await ready()
    const request = model.setIntervalMinutes(30)
    expect(model.history.value).toBeNull()
    expect(histories[1].query.interval_minutes).toBe(30)
    await finishHistory(1); await request
    expect(model.appliedQuery.value?.interval_minutes).toBe(30)
  })

  it.each(['time', 'interval', 'instance', 'duplicate', 'tenant'] as const)('拒绝%s不符的返回数据', async kind => {
    const { model, histories, finishInstances, finishHistory } = setup()
    await finishInstances()
    const result = history(histories[0].query)
    if (kind === 'time') result.start_at = '2020-01-01T00:00:00Z'
    if (kind === 'interval') result.interval_minutes = 60
    if (kind === 'instance') result.series[0].instance.id = 999
    if (kind === 'duplicate') result.series.push(result.series[0])
    if (kind === 'tenant') result.series[0].instance.tenant_id = 99
    await finishHistory(0, result)
    expect(model.history.value).toBeNull()
    expect(model.historyError.value).toContain('不匹配')
  })
})

describe('真实指标、表格分页和XLSX筛选', () => {
  it('0是实际值、null不补0；均值与峰值直接使用各自来源字段', () => {
    const result = history(historyQuery([1, 2], '1d', [null, null], 5, now))
    result.series[0].points = [point(result.start_at, 0), point(new Date(Date.parse(result.start_at) + 300000).toISOString(), null)]
    const option = memoryChart(result, 'memory_usage')
    const lines = option.series as { data: [number, number | null][]; connectNulls: boolean; name: string }[]
    expect(lines[0].data).toEqual([[Date.parse(result.start_at), 0], [Date.parse(result.start_at) + 300000, null]])
    expect(lines.every(line => line.connectNulls === false)).toBe(true)
    expect(lines[0].name).not.toBe(lines[1].name)
    const peaks = memoryChart(result, 'memory_max').series as { data: [number, number | null][] }[]
    expect(peaks[0].data[0][1]).toBe(5)
    expect(metricText(null)).toBe('—')
    expect(metricText(0, '%')).toBe('0%')
    expect(metricText(Infinity)).toBe('—')
  })

  it('单个有效点含真实0值时显示标记，周围缺失点仍保持断线', () => {
    const result = history(historyQuery([1], '1d', [null, null], 5, now))
    const start = Date.parse(result.start_at)
    result.series[0].points = [point(result.start_at, null), point(new Date(start + 300000).toISOString(), 0), point(new Date(start + 600000).toISOString(), null)]
    const line = (memoryChart(result, 'memory_usage').series as { showSymbol: boolean; symbolSize: number; connectNulls: boolean; data: [number, number | null][] }[])[0]
    expect(line.showSymbol).toBe(true)
    expect(line.symbolSize).toBeGreaterThan(0)
    expect(line.connectNulls).toBe(false)
    expect(line.data.map(item => item[1])).toEqual([null, 0, null])
    result.series[0].points = [point(result.start_at, null)]
    const missing = (memoryChart(result, 'memory_usage').series as { showSymbol: boolean; data: [number, number | null][] }[])[0]
    expect(missing.showSymbol).toBe(false)
    expect(missing.data[0][1]).toBeNull()
  })

  it('多个孤立有效点和正常线段同时存在时，孤立点也可见且不跨空白连线', () => {
    const result = history(historyQuery([1], '1d', [null, null], 5, now))
    const start = Date.parse(result.start_at)
    result.series[0].points = [30, null, 40, null, 50, 60].map((value, index) => point(new Date(start + index * 300000).toISOString(), value))
    const line = (memoryChart(result, 'memory_usage').series as { showSymbol: boolean; connectNulls: boolean; symbolSize: (value: unknown, params: { dataIndex: number }) => number }[])[0]
    expect(line.showSymbol).toBe(true)
    expect(line.connectNulls).toBe(false)
    expect(line.symbolSize(null, { dataIndex: 0 })).toBeGreaterThan(0)
    expect(line.symbolSize(null, { dataIndex: 2 })).toBeGreaterThan(0)
    expect(line.symbolSize(null, { dataIndex: 4 })).toBe(0)
  })

  it('表格跨实例分页不改变数据，导出不携带本地页码并固定成功查询时间', async () => {
    const { model, histories, exports, ready, finishHistory } = setup()
    await ready()
    const selected = model.setSelectedIds([1, 2])
    const result = history(histories[1].query)
    result.series.forEach(item => { item.points = Array.from({ length: 25 }, (_, index) => point(new Date(Date.parse(result.start_at) + index * 300000).toISOString(), index === 0 ? null : index)) })
    await finishHistory(1, result); await selected
    expect(model.totalRows.value).toBe(50)
    model.setTablePage(2)
    expect(model.tableRows.value).toHaveLength(20)
    expect(model.tableRows.value[5].instance.id).toBe(2)
    expect(model.tableRows.value[5].memory_usage).toBeNull()
    const applied = { ...model.appliedQuery.value!, resource_ids: [...model.appliedQuery.value!.resource_ids] }
    vi.setSystemTime(now + 3600000)
    const exporting = model.exportExcel()
    expect(exports[0].query).toEqual(applied)
    expect(exports[0].query).not.toHaveProperty('page')
    expect(exports[0].query).not.toHaveProperty('page_size')
    expect(exports[0].context.tenantId).toBe(7)
    exports[0].result.resolve('DCS指标.xlsx')
    expect(await exporting).toBe('DCS指标.xlsx')
    expect(histories).toHaveLength(2)
    model.setTableSize(50)
    expect(model.tablePage.value).toBe(1)
    expect(model.tableRows.value).toHaveLength(50)
    expect(metricPage(result.series, 99, 20)).toEqual([])
  })

  it.each(['selection', 'range', 'interval', 'refresh', 'tenant', 'cancel', 'dispose'] as const)('切换%s取消导出，迟到成功不产生成功提示', async change => {
    const { model, exports, tenant, scope, ready } = setup()
    await ready()
    const exporting = model.exportExcel()
    if (change === 'selection') void model.setSelectedIds([2])
    if (change === 'range') void model.setRangePreset('2d')
    if (change === 'interval') void model.setIntervalMinutes(60)
    if (change === 'refresh') void model.refresh()
    if (change === 'tenant') tenant.value = 8
    if (change === 'cancel') model.cancelExport()
    if (change === 'dispose') scope.stop()
    expect(exports[0].context.signal.aborted).toBe(true)
    exports[0].result.resolve('过期.xlsx')
    expect(await exporting).toBeNull()
    expect(model.exportError.value).toBe('')
    expect(model.exporting.value).toBe(false)
  })

  it('导出失败可重试，不并发重复导出；取消旧请求不影响新导出', async () => {
    const { model, exports, ready } = setup()
    await ready()
    const first = model.exportExcel()
    expect(await model.exportExcel()).toBeNull()
    exports[0].result.reject(new Error('导出失败'))
    expect(await first).toBeNull()
    expect(model.exportError.value).toBe('导出失败')
    const old = model.exportExcel()
    model.cancelExport()
    const current = model.exportExcel()
    exports[1].result.reject(new Error('旧错误'))
    await old
    expect(model.exporting.value).toBe(true)
    expect(model.exportError.value).toBe('')
    exports[2].result.resolve('新的.xlsx')
    expect(await current).toBe('新的.xlsx')
  })

  it('页面可编译，复用PageHeader/EChart并保留来源限制和表头导出', () => {
    const filename = resolve(import.meta.dirname, '../src/views/dcs/DcsMemoryView.vue')
    const { descriptor, errors } = parse(readFileSync(filename, 'utf8'), { filename })
    expect(errors).toEqual([])
    expect(() => compileScript(descriptor, { id: 'dcs-memory' })).not.toThrow()
    expect(compileTemplate({ source: descriptor.template!.content, filename, id: 'dcs-memory' }).errors).toEqual([])
    expect(descriptor.template!.content).toContain(':refresh="refresh"')
    expect(descriptor.template!.content).toContain('row.warning')
    expect(descriptor.template!.content).toContain('history?.demo')
    expect(descriptor.template!.content).toContain('table-actions')
    expect(descriptor.template!.content).toContain('导出 Excel')
    expect(descriptor.template!.content).toContain('<EChart')
    expect(descriptor.styles[0].content).toContain('@media')
  })
})
