import { vi } from 'vitest'
import { effectScope, markRaw, reactive } from 'vue'
import * as Vue from 'vue'
import type { GaussApi, GaussDirectoryApi, GaussInstance, GaussMetricSeries, GaussNativeRows, GaussOverviewOut, GaussSummary } from '../src/types/gaussdb'
import { useGaussPanel } from '../src/views/gaussdb/useGaussPanel'
import { useGaussDiskHistory } from '../src/views/gaussdb/useGaussDiskHistory'
import * as gaussUtils from '../src/views/gaussdb/gauss-utils'
import * as gaussChart from '../src/views/gaussdb/gauss-chart'
import { allNodes, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

export const gaussInstance = (id = 11, tenantId = 7): GaussInstance => ({
  id, resource_id: `db-${id}`, name: `数据库 ${id}`, tenant_id: tenantId, tenant_name: `租户 ${tenantId}`,
  project_ref: 107, project_name: '项目甲', region: 'region-test', status_raw: 'ACTIVE', health: 'OK',
  vcpus: 8, memory_size: 32, memory_unit: null, storage_size: 100, storage_unit: 'GB', snapshot_at: '2026-11-02T01:00:00Z',
})
export const gaussSummary = (patch: Partial<GaussSummary> = {}): GaussSummary => ({
  total: 2, abnormal: 0, transitional: 0, stopped: 0, normal: 2, scope_excluded: 0, disk_high: 0, components_down: 0,
  active_alarms: 0, last_collected_at: '2026-11-02T01:59:00Z', ...patch,
})
export const gaussRows = (patch: Partial<GaussNativeRows> = {}): GaussNativeRows => ({
  source: 'GAUSSDB', status: 'AVAILABLE', queried_at: '2026-11-02T02:00:00Z', items: [], total: null,
  truncated: false, warning: null, units: {}, start_at: null, end_at: null, record_id: null, ...patch,
})
export const gaussOverview = (id = 11, tenantId = 7): GaussOverviewOut => ({
  instance: gaussInstance(id, tenantId), source: 'GAUSSDB', queried_at: '2026-11-02T02:00:00Z',
  disk: { status: 'AVAILABLE', used_gb: 0, total_gb: 100, usage_percent: 0, observed_at: null, warning: null },
  components: [
    { node_id: 'node-1', node_name: '节点甲', component_id: 'cn-1', component_type: 'CN', role: 'master', status: 'ACTIVE' },
    { node_id: 'node-2', node_name: '节点乙', component_id: 'dn-2', component_type: 'DN', role: 'slave', status: 'ACTIVE' },
    { node_id: 'node-3', node_name: '节点丙', component_id: 'cm-3', component_type: 'CM', role: null, status: null },
  ],
  components_status: 'AVAILABLE', components_warning: null, cpu_usage: null, memory_usage: null,
  performance_note: 'CPU、内存按原生巡检报告时间展示。',
})
export const inspectionRecord = { record_id: 'report-1', instance_id: 'db-11', start_time: 1793577600000,
  end_time: 1793577660000, status: 'completed', total_count: 3, normal_count: 2, abnormal_count: 1, failed_count: 0 }
export const gaussInspection = (): GaussNativeRows => gaussRows({ record_id: 'report-1', start_at: '2026-11-02T01:00:00Z', end_at: '2026-11-02T01:01:00Z', items: [{
  name: 'CPU 检查', item: 'os_cpu_usage', description: '按报告时间检查', status: 'abnormal',
  inspection_result: '发现高负载', possible_causes: '业务负载', suggestions: '检查业务', indicators: [
    { name: 'CPU 用户使用率', unit: '%', node: 'node-1', metrics: [
      { name: 'cpu', ip: '192.0.2.1', min: 0, max: 90.01, average: 45, data: { time: [1709163360000, 1709163420000], metrics: [0, null] } },
    ] },
    { name: '内存用量', unit: 'MB', node: 'node-2', metrics: [
      { name: 'memory', ip: '192.0.2.2', unit: 'GB', min: null, max: 2, average: 2, data: [{ time: [1709163360000], metrics: [2] }] },
    ] },
  ],
}] })

export const gaussMetric = (metric: GaussMetricSeries['metric'] = 'cpu'): GaussMetricSeries => ({
  metric, check_name: '负载检查', check_item: `os_${metric}`, check_status: 'normal',
  name: metric === 'cpu' ? 'CPU 使用率' : '内存用量', unit: metric === 'cpu' ? '%' : 'GB',
  node_name: '节点甲', node_ip: '192.0.2.1', min: 0, max: 90, average: 45,
  points: [{ observed_at: '2026-11-02T01:00:00Z', value: 0 }, { observed_at: '2026-11-02T01:01:00Z', value: null }],
})

export function createGaussApi() {
  return {
    instances: vi.fn<GaussApi['instances']>(async (query, context) => ({ items: [gaussInstance(11, context.tenantId ?? 7), gaussInstance(12, context.tenantId ?? 7)], total: 2, page: query.page, page_size: query.page_size })),
    summary: vi.fn<GaussApi['summary']>(async () => gaussSummary()),
    overview: vi.fn<GaussApi['overview']>(async (id, context) => gaussOverview(id, context.tenantId ?? 7)),
    slowNodes: vi.fn<GaussApi['slowNodes']>(async () => gaussRows({ items: [{ node_id: 'node-1', node_name: '节点甲', role: 'master', component_type: 'CN' }] })),
    slowSql: vi.fn<GaussApi['slowSql']>(async () => gaussRows({ items: [{ sql_id: 'sql-1', sql_text: 'select 1', avg_exec_time: '5000000' }] })),
    slowDetail: vi.fn<GaussApi['slowDetail']>(async () => gaussRows({ items: [{ sql_id: 'sql-1', sql_text: 'select 1', query_plan: 'Seq Scan' }] })),
    sessions: vi.fn<GaussApi['sessions']>(async () => gaussRows({ items: [{ session_id: '1', exec_time: '97', query_runtime: '00:00:22.641774' }] })),
    inspections: vi.fn<GaussApi['inspections']>(async () => gaussRows({ items: [{ ...inspectionRecord }], total: 41 })),
    inspectionDetail: vi.fn<GaussApi['inspectionDetail']>(async (_id, recordId) => ({ ...gaussInspection(), record_id: recordId })),
    export: vi.fn<GaussApi['export']>(async () => '原生数据.xlsx'),
  }
}

export function createGaussDirectoryApi() {
  return {
    instance: vi.fn<GaussDirectoryApi['instance']>(async (id, context) => gaussInstance(id, context.tenantId ?? 7)),
    projects: vi.fn<GaussDirectoryApi['projects']>(async context => [{ project_ref: 107, project_name: '项目甲', tenant_id: context.tenantId ?? 7, tenant_name: '租户甲', region: 'region-test' }]),
  }
}

export function createGaussModel(permissions = ['ledger:view', 'task:run', 'ledger:export']) {
  const app = reactive({ tenantId: 7 as number | null, tenantLabel: '租户甲', multiTenant: false })
  const allowed = reactive({ permissions })
  const can = (...keys: string[]) => keys.every(key => allowed.permissions.includes(key))
  const api = createGaussApi(), scope = effectScope()
  // personnelHarness 用 reactive 保存 props；真实组件 props 是 shallow，model 内的 ref 不应被深层解包。
  const model = markRaw(scope.run(() => useGaussPanel({ tenantId: () => app.tenantId, canView: () => can('ledger:view'),
    canRun: () => can('ledger:view', 'task:run'), canExport: () => can('ledger:export'), api,
    now: () => Date.parse('2026-11-02T02:00:00Z') }))!)
  const dependencies = { '@/stores/app': { useAppStore: () => app }, '@/stores/user': { useUserStore: () => ({ user: { id: 1, tenant_ids: [7] }, can }) } }
  return { api, model, app, allowed, dependencies, stop: () => scope.stop() }
}

/** 仅 Gauss 用的依赖适配；仍由共享 personnelHarness 编译实际模板并在内存中渲染。 */
export function mountGauss(file: string, options: Parameters<typeof mountPersonnel>[1] = {}) {
  const adapters = new Map<string, Vue.ConcreteComponent>()
  const resolveComponent: typeof Vue.resolveComponent = (name, self) => {
    const original = Vue.resolveComponent(name, self)
    if (name !== 'el-table' && name !== 'el-drawer') return original
    if (!adapters.has(name)) adapters.set(name, Vue.defineComponent({
      inheritAttrs: false,
      setup(_props, { attrs, slots }) {
        return () => name === 'el-drawer' && !attrs.modelValue ? null : Vue.h(original, attrs, {
          ...slots, default: () => name === 'el-table' && !(attrs.data as unknown[] | undefined)?.length ? slots.empty?.() : slots.default?.(),
        })
      },
    }))
    return adapters.get(name)!
  }
  return mountPersonnel(`gaussdb/${file}`, { ...options, dependencies: {
    // 只补 Table 空插槽和 Drawer 显隐语义，不修改共享 renderer 或复制其表格行实现。
    vue: { ...Vue, resolveComponent },
    './gauss-utils': gaussUtils, './gauss-chart': gaussChart, './useGaussPanel': { useGaussPanel },
    './useGaussDiskHistory': { useGaussDiskHistory }, ...options.dependencies,
  } })
}

type TestNode = Parameters<typeof textOf>[0]
export const gaussButtons = (root: TestNode) => allNodes(root).filter(node => node.tag === 'Button').map(node => textOf(node).trim())
export async function updateGauss(root: TestNode, tag: string, label: string | undefined, value: unknown) {
  const control = findNode(root, tag, node => node.props['aria-label'] === label)
  if (control.props.disabled) throw new Error(`Disabled ${label}`)
  await (control.props['onUpdate:modelValue'] as (value: unknown) => unknown)(value)
  await settlePersonnel()
}
export async function submitGauss(root: TestNode, label = '查询') {
  const button = findNode(root, 'Button', node => textOf(node).trim() === label)
  if (button.props.disabled) throw new Error(`Disabled ${label}`)
  let form = button.parent
  while (form && form.tag !== 'form') form = form.parent
  if (!form) throw new Error(`Missing form for ${label}`)
  await (form.props.onSubmit as (event: { preventDefault: () => void }) => unknown)({ preventDefault() {} })
  await settlePersonnel()
}
