import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { renderToString } from 'vue/server-renderer'
import type { JobPanel, JobReport, JobReportQuery, JobReportRow } from '../src/types/job-reports'
import * as config from '../src/views/job-reports/report-config'
import * as utils from '../src/views/job-reports/report-utils'
import * as format from '../src/utils/format'
import { useJobReport } from '../src/views/job-reports/useJobReport'
import { mountBusinessScript, settleView } from './businessViewHarness'

const disposers: (() => void)[] = []
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()); vi.useRealTimers() })

const native = (extra: Partial<JobReportRow> = {}): JobReportRow => ({
  id: 1, resource_id: 'native-execution', name: 'native', service: 'mrs', resource_type: 'mrs_job',
  source: 'mrs_job_execution', object_level: 'EXECUTION', job_type: 'Flink', engine: 'Flink', engines: ['Flink'],
  status_raw: 'FINISHED', execution_state: 'FINISHED', execution_result: 'UNDEFINED',
  display_status: 'FINISHED', status_label: '已结束（结果未知）', status_known: true,
  health: 'UNKNOWN', lifecycle: 'ACTIVE', tenant_id: 7, tenant_name: 'tenant', project_ref: 11,
  project_id: 'p11', project_name: 'project', region: null, updated_at: '2026-11-09T00:00:00Z', status_since: null,
  status_details: { cluster: 'cluster-a', cluster_id: 'cid', queue: 'queue-a', job_id: '000007', elapsed_time: 9, elapsed_time_unit: 'TICKS' },
  metrics: { run_minutes: null }, active_alarms: 0, ...extra,
})
const report = (panel: JobPanel, items: JobReportRow[] = [], message = '当前范围尚无作业采集记录'): JobReport => ({
  panel, items, page: 1, page_size: 20, total: items.length, data_notes: [],
  summary: { total: items.length, status_counts: items.length ? { FINISHED: items.length } : {},
    by_source: {}, by_engine: {}, by_type: {}, unknown_total: items.length, no_data_total: 0,
    active_alarms: 0, latest_update_at: null, data_status: items.length ? 'AVAILABLE' : 'NO_DATA' },
  collection_status: { state: items.length ? 'AVAILABLE' : 'NOT_COLLECTED', message },
})

const passthrough = Vue.defineComponent({ setup(_props, { slots }) { return () => Vue.h('div', slots.default?.()) } })
const tableRowsKey = Symbol('table-rows') as Vue.InjectionKey<Vue.ComputedRef<JobReportRow[]>>
const Table = Vue.defineComponent({
  props: ['data'],
  setup(props, { slots }) {
    Vue.provide(tableRowsKey, Vue.computed(() => props.data as JobReportRow[]))
    return () => Vue.h('table', slots.default?.())
  },
})
const Column = Vue.defineComponent({
  props: ['label', 'prop'],
  setup(props, { slots }) {
    const rows = Vue.inject(tableRowsKey)!
    return () => Vue.h('td', [String(props.label), ...rows.value.map(row => slots.default?.({ row }) ?? String(row[props.prop as keyof JobReportRow] ?? ''))])
  },
})

async function renderPanel(panel: JobPanel, data: JobReport | null, options: { loading?: boolean; error?: string } = {}) {
  const filename = resolve(import.meta.dirname, '../src/views/job-reports/JobReportPanel.vue')
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: 'job-layers', inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const current = Vue.shallowRef(data)
  const dependencies: Record<string, unknown> = {
    vue: Vue, '@element-plus/icons-vue': { Download: passthrough, Search: passthrough },
    'element-plus': { ElMessage: { success: vi.fn() } }, '@/api/job-reports': {},
    '@/components/PageHeader.vue': { default: passthrough }, '@/components/EChart.vue': { default: passthrough },
    '@/components/TenantTag.vue': { default: passthrough }, './JobReportDrawer.vue': { default: passthrough },
    '@/stores/app': { useAppStore: () => ({ tenantLabel: 'tenant', tenantId: 7 }) }, '@/utils/format': format,
    './report-config': config, './report-utils': utils,
    './useJobReport': { useJobReport: () => ({
      query: Vue.shallowRef({ page: 1, page_size: 20 }), report: current, rows: Vue.computed(() => current.value?.items ?? []),
      loading: Vue.shallowRef(options.loading ?? false), error: Vue.shallowRef(options.error ?? ''),
      exporting: Vue.shallowRef(false), exportError: Vue.shallowRef(''),
      refresh: vi.fn(), setFilters: vi.fn(), reset: vi.fn(), setPage: vi.fn(), setPageSize: vi.fn(), exportExcel: vi.fn(), cancelExport: vi.fn(),
    }) },
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`)
    return dependencies[name]
  }, exports)
  const app = Vue.createSSRApp(exports.default as Vue.Component, { panel })
  for (const name of ['ElInput', 'ElSelect', 'ElOption', 'ElButton', 'ElTag', 'ElPagination', 'ElCollapse', 'ElCollapseItem']) app.component(name, passthrough)
  app.component('ElAlert', Vue.defineComponent({ props: ['title'], setup: props => () => Vue.h('p', String(props.title)) }))
  app.component('ElEmpty', Vue.defineComponent({ props: ['description'], setup: props => () => Vue.h('p', String(props.description)) }))
  app.component('ElSkeleton', Vue.defineComponent({ setup: () => () => Vue.h('p', '加载中') }))
  app.component('ElTable', Table)
  app.component('ElTableColumn', Column)
  app.directive('perm', {})
  app.directive('loading', {})
  return renderToString(app)
}

describe('job report layer presentation', () => {
  it('keeps native fields and result distinct, including unknown elapsed units', async () => {
    const html = await renderPanel('mrs', report('mrs', [native()]))
    expect(html).toContain('MRS 原生执行数')
    for (const value of ['cluster-a', 'queue-a', '000007', 'UNDEFINED', '时长未知（单位未确认）']) expect(html).toContain(value)
    expect(html).not.toContain('MRS 作业节点总数')
    expect(utils.durationText(native())).not.toBe('0 分钟')
    expect(utils.durationText(native({ metrics: { run_minutes: 0 } }))).toBe('时长未知（单位未确认）')
    expect(utils.durationText(native({ status_details: { elapsed_time: 0, duration_basis: 'start_end_milliseconds' }, metrics: { run_minutes: 0 } }))).toBe('0 分钟')
    expect(config.statusMeta('FINISHED').type).toBe('warning')
  })

  it('explains definitions without recent instances and preserves schedule state', async () => {
    const row = native({ resource_type: 'dgc_batch_job', object_level: 'DEFINITION', execution_state: null,
      display_status: 'NO_DATA', status_details: { instance_query_status: 'EMPTY', schedule_status: 'NORMAL' } })
    const html = await renderPanel('batch', report('batch', [row]))
    expect(html).toContain('DGC 批定义数')
    expect(html).toContain('近24h无实例')
    expect(html).toContain('NORMAL')
    expect(utils.executionHint({ ...row, status_details: { instance_query_status: 'FAILED' } })).toBe('实例查询失败')
    expect(utils.executionHint({ ...row, object_level: 'INSTANCE' })).toContain('未匹配父定义')
    expect(config.jobTypeLabel({ ...row, object_level: 'INSTANCE' })).toBe('DataArts 独立实例')
  })

  it.each(['当前范围尚无采集记录', '本次云端完整查询返回空结果', '当前查询范围无匹配对象'])('shows the empty reason without all-zero KPI cards: %s', async message => {
    const html = await renderPanel('dgc', report('dgc', [], message))
    expect(html).toContain(message)
    expect(html).not.toContain('kpi-grid')
    expect(html).not.toContain('exhibit-grid')
  })

  it('keeps loading and failure distinct from empty data', async () => {
    const loading = await renderPanel('dgc', null, { loading: true })
    expect(loading).toContain('加载中')
    expect(loading).not.toContain('无任务')
    const failure = await renderPanel('dgc', null, { error: 'offline' })
    expect(failure).toContain('加载失败：offline')
    expect(failure).not.toContain('kpi-grid')
  })

  it('supports legacy payloads with an explicitly unconfirmed collection state', () => {
    const legacy = report('dgc')
    delete legacy.collection_status
    expect(utils.emptyReportText(legacy)).toContain('未确认')
  })

  it('keeps five URLs and explicit DGC/native scope labels', () => {
    expect(config.PANEL_CONFIG.realtime.title).toContain('DGC')
    expect(config.PANEL_CONFIG.batch.title).toContain('DGC')
    expect(config.objectLevelOptions('mrs')).toEqual(['EXECUTION', 'NODE'])
    expect(config.objectLevelOptions('flink')).toEqual(['DEFINITION', 'EXECUTION', 'NODE', 'INSTANCE'])
    expect(config.SOURCE_FACTS.mrs_job.fields).toContain('job_id')
    expect(config.SOURCE_FACTS.dgc_mrs_node.fields).toContain('node_status')
  })
})

describe('job layer requests', () => {
  it('passes source/layer filters through list and export, resets paging and clears them on tenant change', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async (panel: JobPanel) => report(panel))
    const exporter = vi.fn(async () => 'layers.xlsx')
    const view = mountBusinessScript<{
      setFilters: (query: Partial<JobReportQuery>) => void; setPage: (page: number) => void
      exportExcel: () => Promise<string | null>; query: JobReportQuery; objectLevel: string
    }>('job-reports/JobReportPanel.vue', { props: { panel: 'flink' }, dependencies: {
      '@/api/job-reports': { fetchJobReport: fetcher, exportJobReport: exporter },
      './useJobReport': { useJobReport }, './report-config': config, './report-utils': utils,
    } })
    disposers.push(view.unmount)
    await settleView()
    expect(view.state.objectLevel).toBe('DEFINITION')
    view.state.setFilters({ object_level: 'NODE', source: 'dataarts_instance_detail' })
    await settleView()
    expect(view.state.objectLevel).toBe('NODE')
    expect(view.state.query.page).toBe(1)
    expect(fetcher).toHaveBeenLastCalledWith('flink', expect.objectContaining({ object_level: 'NODE', source: 'dataarts_instance_detail' }), expect.objectContaining({ tenantId: 7 }))
    await view.state.exportExcel()
    expect(exporter).toHaveBeenCalledWith('flink', expect.objectContaining({ object_level: 'NODE', source: 'dataarts_instance_detail' }), expect.objectContaining({ tenantId: 7 }))
    view.appState.tenantId = 8
    await settleView()
    expect(view.state.query.object_level).toBeUndefined()
    expect(view.state.query.source).toBeUndefined()
    expect(view.state.objectLevel).toBe('DEFINITION')
  })
})
