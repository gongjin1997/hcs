import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, type EffectScope } from 'vue'
import type { JobPanel, JobReport, JobReportExporter, JobReportFetcher, JobReportQuery, JobReportRow, JobReportSummary } from '../src/types/job-reports'
import { useJobReport } from '../src/views/job-reports/useJobReport'
import { detailText, durationText, factText, jobNodes } from '../src/views/job-reports/report-utils'

const observedAt = '2026-10-25T00:00:00Z'
const scopes: EffectScope[] = []

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(observedAt))
})

afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function job(id = 1, overrides: Partial<JobReportRow> = {}): JobReportRow {
  return {
    id, resource_id: `job-${id}`, name: `作业 ${id}`, service: 'dataarts', resource_type: 'dgc_realtime_job',
    job_type: null, engine: null, status_raw: 'running', execution_state: 'running', execution_result: null,
    display_status: 'RUNNING', status_label: '运行中', status_known: true, health: 'HEALTHY', lifecycle: 'ACTIVE',
    tenant_id: 7, tenant_name: '租户七', project_ref: null, project_id: null, project_name: null, region: null,
    updated_at: observedAt, status_since: null, status_details: {}, metrics: {}, active_alarms: 0,
    ...overrides,
  }
}

function report(items = [job()], overrides: Partial<JobReport> = {}): JobReport {
  const total = overrides.total ?? items.length
  // Mirror the API summary contract, including the resource-type distribution.
  const summary: JobReportSummary = {
    total, status_counts: total ? { RUNNING: total } : {}, unknown_total: 0, no_data_total: 0,
    active_alarms: 0, latest_update_at: total ? observedAt : null,
    data_status: total ? 'AVAILABLE' : 'NO_DATA',
    by_source: total ? { dataarts: total } : {}, by_engine: total ? { UNKNOWN: total } : {},
    by_type: total ? { dgc_realtime_job: total } : {},
  }
  return { panel: 'dgc', summary, items, total, page: 1, page_size: 20, data_notes: ['最新资源快照，非执行历史'], ...overrides }
}

interface PendingRequest {
  panel: JobPanel
  query: JobReportQuery
  signal: AbortSignal
  tenantId: number | null
  resolve: (value: JobReport) => void
  reject: (reason: unknown) => void
}

interface PendingExport extends Omit<PendingRequest, 'resolve'> {
  resolve: (filename: string) => void
}

function setup() {
  const panel = ref<JobPanel>('dgc')
  const tenantId = ref<number | null>(7)
  const requests: PendingRequest[] = []
  // Deliberately ignore abort events: late responses must also be rejected by the composable.
  const fetcher = vi.fn<JobReportFetcher>((panel, query, { signal, tenantId }) =>
    new Promise((resolve, reject) => requests.push({ panel, query, signal, tenantId, resolve, reject })),
  )
  const exports: PendingExport[] = []
  const exporter = vi.fn<JobReportExporter>((panel, query, { signal, tenantId }) =>
    new Promise((resolve, reject) => exports.push({ panel, query, signal, tenantId, resolve, reject })),
  )
  const scope = effectScope()
  scopes.push(scope)
  const model = scope.run(() => useJobReport({ panel: () => panel.value, tenantId: () => tenantId.value, fetcher, exporter }))!
  const finish = async (index: number, value = report()) => {
    requests[index].resolve(value)
    await nextTick()
  }
  return { model, panel, tenantId, requests, fetcher, exports, exporter, scope, finish }
}

describe('useJobReport request ordering and scope', () => {
  it('starts one scoped request and replaces rows and summary atomically', async () => {
    const { model, requests, finish } = setup()
    expect(requests).toHaveLength(1)
    expect(requests[0]).toMatchObject({ panel: 'dgc', tenantId: 7, query: { keyword: '', status: [], engine: '', page: 1, page_size: 20 } })
    expect(model.report.value).toBeNull()
    expect(model.rows.value).toEqual([])
    expect(model.loading.value).toBe(true)
    expect(model.fetchedAt.value).toBeNull()
    const result = report([job(1), job(2)], { total: 12 })
    await finish(0, result)
    expect(model.report.value).toBe(result)
    expect(model.rows.value).toBe(result.items)
    expect(model.report.value?.summary.total).toBe(12)
    expect(model.loading.value).toBe(false)
    expect(model.error.value).toBe('')
    expect(model.fetchedAt.value).toBe('2026-10-25T00:00:00.000Z')
  })

  it('discards an older response arriving after the latest response, including its summary and timestamp', async () => {
    const { model, requests, finish } = setup()
    const current = model.refresh()
    expect(requests[0].signal.aborted).toBe(true)
    const latest = report([job(2)], { total: 200 })
    await finish(1, latest)
    await current
    const fetchedAt = model.fetchedAt.value
    vi.setSystemTime(new Date('2026-10-25T00:01:00Z'))
    await finish(0, report([job(1)], { total: 1 }))
    expect(model.report.value).toBe(latest)
    expect(model.rows.value.map(row => row.id)).toEqual([2])
    expect(model.report.value?.summary.total).toBe(200)
    expect(model.fetchedAt.value).toBe(fetchedAt)
    expect(model.loading.value).toBe(false)
  })

  it.each(['resolve', 'reject'] as const)('does not let a stale %s clear the latest loading state', async outcome => {
    const { model, requests, finish } = setup()
    const current = model.refresh()
    if (outcome === 'resolve') requests[0].resolve(report([job(99)]))
    else requests[0].reject(new Error('stale failure'))
    await nextTick()
    expect(model.report.value).toBeNull()
    expect(model.loading.value).toBe(true)
    expect(model.error.value).toBe('')
    await finish(1, report([job(2)]))
    await current
    expect(model.rows.value.map(row => row.id)).toEqual([2])
    expect(model.loading.value).toBe(false)
  })

  it.each(['another tenant', 'all tenants', 'panel'] as const)('synchronously clears old data and aborts requests when switching to %s', async change => {
    const { model, panel, tenantId, requests, finish } = setup()
    await finish(0)
    model.setFilters({ keyword: 'old', status: ['RUNNING'], engine: 'Flink' })
    await finish(1)
    model.setPageSize(50)
    await finish(2, report([job()], { page_size: 50 }))
    model.setPage(3)
    await finish(3, report([job()], { page: 3, page_size: 50 }))
    const oldRefresh = model.refresh()
    if (change === 'panel') panel.value = 'mrs'
    else tenantId.value = change === 'all tenants' ? null : 8
    expect(requests[4].signal.aborted).toBe(true)
    expect(model.report.value).toBeNull()
    expect(model.rows.value).toEqual([])
    expect(model.fetchedAt.value).toBeNull()
    expect(model.error.value).toBe('')
    expect(model.loading.value).toBe(true)
    expect(model.query.value).toEqual({ keyword: '', status: [], engine: '', page: 1, page_size: 20 })
    expect(requests).toHaveLength(6)
    expect(requests[5]).toMatchObject({ panel: panel.value, tenantId: tenantId.value, query: model.query.value })
    await finish(4, report([job(99)]))
    await oldRefresh
    expect(model.report.value).toBeNull()
    expect(model.loading.value).toBe(true)
    const current = report([job(2, { tenant_id: tenantId.value })], { panel: panel.value })
    await finish(5, current)
    expect(model.report.value).toBe(current)
    expect(model.loading.value).toBe(false)
  })

  it('debounces filters, immediately clears stale data, and resets a later page to page one', async () => {
    const { model, requests, finish } = setup()
    await finish(0)
    model.setPage(4)
    await finish(1, report([job()], { page: 4 }))
    const stale = model.refresh()
    model.setFilters({ keyword: 'f' }, 250)
    expect(requests[2].signal.aborted).toBe(true)
    expect(model.report.value).toBeNull()
    expect(model.fetchedAt.value).toBeNull()
    expect(model.loading.value).toBe(true)
    expect(model.query.value.page).toBe(1)
    await finish(2, report([job(99)], { page: 4 }))
    await stale
    expect(model.rows.value).toEqual([])
    expect(model.loading.value).toBe(true)
    await vi.advanceTimersByTimeAsync(200)
    model.setFilters({ keyword: 'flink', status: ['FAILED'], engine: 'Flink' }, 250)
    await vi.advanceTimersByTimeAsync(249)
    expect(requests).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(requests).toHaveLength(4)
    expect(requests[3].query).toEqual({ keyword: 'flink', status: ['FAILED'], engine: 'Flink', page: 1, page_size: 20 })
    await finish(3)
    expect(model.loading.value).toBe(false)
  })

  it('normalizes pagination on page-size changes and reset without fetching unchanged pages', async () => {
    const { model, requests, finish } = setup()
    await finish(0)
    model.setPage(1)
    model.setPageSize(20)
    expect(requests).toHaveLength(1)
    model.setFilters({ keyword: 'job', status: ['FAILED'], engine: 'Spark' })
    await finish(1)
    model.setPage(5)
    expect(requests[2].query.page).toBe(5)
    await finish(2)
    model.setPageSize(50)
    expect(requests[3].query).toEqual({ keyword: 'job', status: ['FAILED'], engine: 'Spark', page: 1, page_size: 50 })
    await finish(3)
    model.setPage(2)
    await finish(4)
    model.reset()
    expect(requests).toHaveLength(6)
    expect(requests[5].query).toEqual({ keyword: '', status: [], engine: '', page: 1, page_size: 50 })
  })

  it('returns to a valid page if a refresh removes the last page, retaining filters', async () => {
    const { model, requests, finish } = setup()
    await finish(0, report([job()], { total: 65 }))
    model.setFilters({ keyword: 'ops', status: ['RUNNING'] })
    await finish(1, report([job()], { total: 65 }))
    model.setPage(4)
    await finish(2, report([job(65)], { page: 4, total: 65 }))
    const refreshing = model.refresh()
    await finish(3, report([], { page: 4, total: 21 }))
    await refreshing
    expect(model.query.value).toMatchObject({ page: 2, keyword: 'ops', status: ['RUNNING'] })
    expect(model.report.value).toBeNull()
    expect(model.loading.value).toBe(true)
    expect(requests).toHaveLength(5)
    expect(requests[4].query.page).toBe(2)
    await finish(4, report([job(21)], { page: 2, total: 21 }))
    expect(model.rows.value.map(row => row.id)).toEqual([21])
    expect(model.loading.value).toBe(false)
  })

  it('passes a separate status-array snapshot to each request', () => {
    const { model, requests } = setup()
    const status = ['RUNNING']
    model.setFilters({ status })
    expect(requests[1].query.status).not.toBe(model.query.value.status)
    status.push('FAILED')
    expect(requests[1].query.status).toEqual(['RUNNING'])
    expect(model.query.value.status).toEqual(['RUNNING'])
  })

  it.each([
    { cause: new Error('permission denied'), message: 'permission denied' },
    { cause: 'unstructured failure', message: '报表加载失败，请稍后重试' },
  ])('exposes a current failure and recovers on retry: $message', async ({ cause, message }) => {
    const { model, requests, finish } = setup()
    requests[0].reject(cause)
    await nextTick()
    expect(model.error.value).toBe(message)
    expect(model.loading.value).toBe(false)
    expect(model.report.value).toBeNull()
    expect(model.fetchedAt.value).toBeNull()
    const retry = model.refresh()
    expect(model.error.value).toBe('')
    expect(model.loading.value).toBe(true)
    await finish(1)
    await retry
    expect(model.error.value).toBe('')
    expect(model.loading.value).toBe(false)
    expect(model.rows.value).toHaveLength(1)
  })

  it('preserves the last successful snapshot and its time when a manual refresh fails', async () => {
    const { model, requests, finish } = setup()
    const previous = report()
    await finish(0, previous)
    const fetchedAt = model.fetchedAt.value
    const refresh = model.refresh()
    requests[1].reject(new Error('offline'))
    await refresh
    expect(model.report.value).toBe(previous)
    expect(model.fetchedAt.value).toBe(fetchedAt)
    expect(model.error.value).toBe('offline')
    expect(model.loading.value).toBe(false)
  })

  it('rejects a response for a different panel instead of displaying it', async () => {
    const { model, finish } = setup()
    await finish(0, report([job()], { panel: 'mrs' }))
    expect(model.report.value).toBeNull()
    expect(model.error.value).toContain('面板与请求不一致')
    expect(model.loading.value).toBe(false)
    expect(model.fetchedAt.value).toBeNull()
  })

  it('aborts a disposed scope, ignores its late response, and stops all future requests', async () => {
    const { model, requests, scope, panel, tenantId, finish } = setup()
    expect(vi.getTimerCount()).toBe(1)
    scope.stop()
    expect(requests[0].signal.aborted).toBe(true)
    expect(model.loading.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    await finish(0)
    await model.refresh()
    model.setFilters({ keyword: 'disposed' }, 250)
    model.setPage(2)
    model.setPageSize(50)
    model.reset()
    panel.value = 'mrs'
    tenantId.value = 8
    await vi.advanceTimersByTimeAsync(60000)
    expect(await model.exportExcel()).toBeNull()
    expect(requests).toHaveLength(1)
    expect(model.report.value).toBeNull()
    expect(model.fetchedAt.value).toBeNull()
    expect(model.error.value).toBe('')
  })

  it('removes pending filter debounce timers on disposal', async () => {
    const { model, requests, scope, finish } = setup()
    await finish(0)
    model.setFilters({ keyword: 'pending' }, 250)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(60000)
    expect(requests).toHaveLength(1)
    expect(model.loading.value).toBe(false)
  })

  it('cancels a pending debounce when the tenant changes', async () => {
    const { model, tenantId, requests, finish } = setup()
    await finish(0)
    model.setFilters({ keyword: 'old tenant' }, 250)
    tenantId.value = 8
    expect(requests).toHaveLength(2)
    expect(requests[1]).toMatchObject({ tenantId: 8, query: { keyword: '', page: 1 } })
    await vi.advanceTimersByTimeAsync(250)
    expect(requests).toHaveLength(2)
  })

  it('does not overlap periodic refreshes or refresh while disabled or exporting', async () => {
    const { model, requests, exports, finish } = setup()
    await vi.advanceTimersByTimeAsync(30000)
    expect(requests).toHaveLength(1)
    await finish(0)
    await vi.advanceTimersByTimeAsync(30000)
    expect(requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(60000)
    expect(requests).toHaveLength(2)
    await finish(1)
    model.autoRefresh.value = false
    await vi.advanceTimersByTimeAsync(30000)
    expect(requests).toHaveLength(2)
    model.autoRefresh.value = true
    const exporting = model.exportExcel()
    await vi.advanceTimersByTimeAsync(60000)
    expect(requests).toHaveLength(2)
    exports[0].resolve('作业报表.xlsx')
    expect(await exporting).toBe('作业报表.xlsx')
  })

})

describe('useJobReport Excel export', () => {
  it('exports frozen filters without changing the displayed page', async () => {
    const { model, requests, exports, finish } = setup()
    model.setFilters({ keyword: 'match', status: ['FAILED'], engine: 'Flink' })
    model.setPage(3)
    await finish(2, report([job(3)], { page: 3, total: 205 }))
    const displayed = model.report.value
    const pending = model.exportExcel()
    expect(model.exporting.value).toBe(true)
    expect(exports).toHaveLength(1)
    expect(exports[0]).toMatchObject({ panel: 'dgc', tenantId: 7, query: { keyword: 'match', status: ['FAILED'], engine: 'Flink', page: 3 } })
    expect(exports[0].query.status).not.toBe(model.query.value.status)
    exports[0].resolve('DGC.xlsx')
    expect(await pending).toBe('DGC.xlsx')
    expect(model.report.value).toBe(displayed)
    expect(model.query.value.page).toBe(3)
    expect(requests).toHaveLength(3)
    expect(model.exporting.value).toBe(false)
    expect(model.exportError.value).toBe('')
  })

  it('prevents exports during loading and concurrent exports', async () => {
    const { model, exports, finish } = setup()
    expect(await model.exportExcel()).toBeNull()
    expect(exports).toHaveLength(0)
    await finish(0)
    const pending = model.exportExcel()
    expect(await model.exportExcel()).toBeNull()
    expect(exports).toHaveLength(1)
    exports[0].resolve('作业报表.xlsx')
    expect(await pending).toBe('作业报表.xlsx')
  })

  it.each(['tenant', 'panel', 'filters', 'cancel', 'dispose'] as const)('cancels a pending export on %s', async change => {
    const { model, tenantId, panel, exports, scope, finish } = setup()
    await finish(0)
    const pending = model.exportExcel()
    if (change === 'tenant') tenantId.value = 8
    if (change === 'panel') panel.value = 'mrs'
    if (change === 'filters') model.setFilters({ keyword: 'new' })
    if (change === 'cancel') model.cancelExport()
    if (change === 'dispose') scope.stop()
    expect(exports[0].signal.aborted).toBe(true)
    expect(model.exporting.value).toBe(false)
    exports[0].resolve('old.xlsx')
    expect(await pending).toBeNull()
    expect(model.exportError.value).toBe('')
  })

  it('keeps the new export pending after a cancelled request settles', async () => {
    const { model, exports, finish } = setup()
    await finish(0)
    const old = model.exportExcel()
    model.cancelExport()
    const current = model.exportExcel()
    exports[0].reject(new Error('old failure'))
    expect(await old).toBeNull()
    expect(model.exporting.value).toBe(true)
    expect(model.exportError.value).toBe('')
    exports[1].resolve('new.xlsx')
    expect(await current).toBe('new.xlsx')
    expect(model.exporting.value).toBe(false)
  })

  it.each([new Error('导出失败'), 'invalid response'])('shows export failure and permits retry: %j', async cause => {
    const { model, exports, finish } = setup()
    await finish(0)
    const failed = model.exportExcel()
    exports[0].reject(cause)
    expect(await failed).toBeNull()
    expect(model.exportError.value).toBe(cause instanceof Error ? cause.message : 'Excel 导出失败')
    expect(model.exporting.value).toBe(false)
    const retry = model.exportExcel()
    expect(model.exportError.value).toBe('')
    exports[1].resolve('retry.xlsx')
    expect(await retry).toBe('retry.xlsx')
  })
})

function localTime(value: number | string): string {
  const date = new Date(value)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

describe('job report fact formatting', () => {
  it.each([null, undefined, ''])('shows uncollected facts as missing rather than zero: %j', value => {
    expect(factText(value)).toBe('未采集')
  })

  it('preserves explicit zero, false, unknown status text and structured source facts', () => {
    expect(factText(0)).toBe('0')
    expect(factText(false)).toBe('false')
    expect(factText('UNKNOWN_STATE')).toBe('UNKNOWN_STATE')
    expect(factText({ count: null, nodes: [0, false] })).toBe('{"count":null,"nodes":[0,false]}')
  })

  it('chooses only the duration metric belonging to the resource type', () => {
    const metrics = { run_minutes: 2.5, elapsed_min: 9 }
    expect(durationText(job(1, { metrics }))).toBe('2.5 分钟')
    expect(durationText(job(1, { resource_type: 'dgc_batch_job', metrics }))).toBe('2.5 分钟')
    expect(durationText(job(1, { resource_type: 'mrs_job', metrics }))).toBe('9 分钟')
    expect(durationText(job(1, { metrics: { elapsed_min: 9 } }))).toBe('未采集')
    expect(durationText(job(1, { resource_type: 'mrs_job', metrics: { run_minutes: 2.5 } }))).toBe('未采集')
  })

  it.each([undefined, null, '', '3', -1, NaN, Infinity, -Infinity])('does not fabricate a numeric duration from %j', value => {
    expect(durationText(job(1, { metrics: { run_minutes: value } }))).toBe('未采集')
    expect(durationText(job(1, { resource_type: 'mrs_job', metrics: { elapsed_min: value } }))).toBe('未采集')
  })

  it.each([0, 0.25, 60])('preserves a saved duration of %s minutes without unit conversion', value => {
    expect(durationText(job(1, { metrics: { run_minutes: value } }))).toBe(`${value} 分钟`)
    expect(durationText(job(1, { resource_type: 'mrs_job', metrics: { elapsed_min: value } }))).toBe(`${value} 分钟`)
  })

  it.each(['start_time', 'end_time', 'plan_time', 'submitted_time'])('formats %s as milliseconds in the local timezone', key => {
    const milliseconds = Date.UTC(2026, 9, 25, 4, 5, 6, 789)
    expect(detailText(key, milliseconds)).toBe(`${localTime(milliseconds)}（本地）`)
    expect(detailText(key, 1234)).toBe(`${localTime(1234)}（本地）`)
  })

  it.each([0, -1, NaN, Infinity, 1e16, '1792886400000', 'not-a-date', null])('preserves an unparseable or nonnumeric timestamp fact: %j', value => {
    expect(detailText('start_time', value)).toBe(factText(value))
  })

  it('does not infer a timestamp from an unrelated numeric field', () => {
    expect(detailText('run_minutes', 1792886400000)).toBe('1792886400000')
  })

  it('formats only actual node objects without inventing missing names or statuses', () => {
    const row = job(1, { status_details: { nodes: [null, 'node', 7, [], { name: 'first', status: 'FUTURE_STATE' }, { name: 0, status: false }, {}] } })
    expect(jobNodes(row)).toEqual([
      { name: 'first', status: 'FUTURE_STATE' }, { name: '0', status: 'false' }, { name: '未采集', status: '未采集' },
    ])
    expect(jobNodes(job())).toEqual([])
    expect(jobNodes(job(1, { status_details: { nodes: { name: 'not an array' } } }))).toEqual([])
  })
})
