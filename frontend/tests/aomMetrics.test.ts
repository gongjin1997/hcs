import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import dayjs from 'dayjs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MetricsSource, Tenant, TenantIn } from '../src/types'
import type { InspectionTask } from '../src/api'
import * as dict from '../src/utils/dict'
import * as tenantForm from '../src/views/system/tenant-form'

interface TreeNode { tag: string; text: string; props: Record<string, unknown>; parent: TreeNode | null; children: TreeNode[] }
const node = (tag: string, text = ''): TreeNode => ({ tag, text, props: {}, parent: null, children: [] })
function remove(child: TreeNode) {
  if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
  child.parent = null
}
const renderer = Vue.createRenderer<TreeNode, TreeNode>({
  insert(child, parent, anchor) { remove(child); const i = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(i < 0 ? parent.children.length : i, 0, child); child.parent = parent },
  remove, createElement: tag => node(tag), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
const walk = (root: TreeNode): TreeNode[] => [root, ...root.children.flatMap(walk)]
const text = (root: TreeNode): string => root.tag === '#comment' ? '' : root.text + root.children.map(text).join('')
const stub = (name: string) => Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) { return () => Vue.h(name, attrs, name === 'Dialog' && attrs.modelValue === false ? [] : Object.values(slots).flatMap(slot => slot?.() ?? [])) },
})
const tableRows = Symbol('test-table-rows')
const Table = Vue.defineComponent({
  props: ['data'], inheritAttrs: false,
  setup(props, { attrs, slots }) {
    Vue.provide(tableRows, () => props.data as unknown[])
    return () => Vue.h('Table', attrs, slots.default?.())
  },
})
const Column = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    const rows = Vue.inject<() => unknown[]>(tableRows, () => [])
    return () => Vue.h('Column', attrs, slots.default ? rows().flatMap((row, index) => slots.default?.({ row, $index: index }) ?? []) : [])
  },
})
const Form = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots, expose }) {
    expose({ validate: async () => true, clearValidate: vi.fn() })
    return () => Vue.h('Form', attrs, slots.default?.())
  },
})
const seed = (source: MetricsSource = 'AUTO'): Tenant => ({
  id: 7, code: 'qa-aom', name: 'QA AOM', short_name: 'QA', color: '#1d63f0', iam_endpoint: 'https://iam.example.test',
  domain_name: '', domain_id: null, auth_type: 'PASSWORD', username: 'qa-only', access_key_mask: null, secret_set: true,
  endpoints: {}, enabled: true, report_mail_to: [], remark: null, metrics_source: source, metrics_effective: source,
  prometheus_url: null, capabilities: {}, last_check: {}, last_check_at: null,
  projects: [{ region_id: 'qa-region', project_id: 'qa-project', project_name: 'QA', external_domain: 'example.test', env: 'TEST', workspaces: [], enabled: true }],
  resource_count: 0, user_count: 0,
})
let tenants: Tenant[]
let taskRows: InspectionTask[]
let tenantScope: number[] | null = null
const api = {
  createTenant: vi.fn(async (_body: TenantIn) => undefined),
  updateTenant: vi.fn(async (_id: number, _body: TenantIn) => undefined),
  listTenants: vi.fn(async () => tenants), testTenant: vi.fn(), deleteTenant: vi.fn(),
  listTasks: vi.fn(async () => taskRows), reportSchedule: vi.fn(async () => null), runTask: vi.fn(), saveReportSchedule: vi.fn(),
  updateTask: vi.fn(async (_code: string, _body: Record<string, unknown>) => ({ changed: true })),
}
const components = new Set<string>()
const disposers: (() => void)[] = []
function load(file: 'TenantDialog' | 'TenantPanel' | 'TaskPanel'): Vue.Component {
  const filename = resolve(import.meta.dirname, `../src/views/system/${file}.vue`)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  for (const match of descriptor.template!.content.matchAll(/<el-([a-z][a-z0-9-]*)/g)) {
    components.add('El' + match[1].split('-').map(part => part[0].toUpperCase() + part.slice(1)).join(''))
  }
  const compiled = compileScript(descriptor, { id: `aom-${file}`, inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } })
  const icons = ['Delete', 'InfoFilled', 'Plus', 'QuestionFilled', 'CircleCheckFilled', 'CircleCloseFilled', 'Edit', 'Key', 'Lightning', 'MoreFilled', 'RemoveFilled', 'Select', 'WarningFilled', 'Calendar', 'VideoPlay']
  const dependencies: Record<string, unknown> = {
    vue: Vue, '@/api': api, '@/utils/dict': dict, './tenant-form': tenantForm, './TenantDialog.vue': { default: stub('TenantDialog') },
    '@/stores/user': { useUserStore: () => ({ user: { tenant_ids: tenantScope }, can: () => true, canAny: () => true, fetchMe: vi.fn(async () => undefined) }) },
    '@/stores/app': { useAppStore: () => ({ currentTenant: null, tenantId: null }) },
    '@/components/TenantTag.vue': { default: stub('TenantTag') }, dayjs: { default: dayjs },
    '@/utils/format': { fmtTime: () => '2026-10-27', humanize: String, fromNow: () => '刚刚' },
    '@element-plus/icons-vue': Object.fromEntries(icons.map(name => [name, stub('Icon')])),
    'element-plus': { ElMessage: { success: vi.fn(), warning: vi.fn() }, ElMessageBox: { confirm: vi.fn() }, ElNotification: vi.fn() },
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name === '@/components/RecipientSelect.vue') return { default: stub('RecipientSelect') }
    if (name === '@/views/ecs/EcsPolicyDialog.vue') return { default: stub('EcsPolicyDialog') }
    if (name === './SmsTenantDialog.vue') return { default: stub('SmsTenantDialog') }
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`)
    return dependencies[name]
  }, exports)
  return exports.default as Vue.Component
}
const Dialog = load('TenantDialog'), Panel = load('TenantPanel'), Tasks = load('TaskPanel')
const settle = async () => { await Vue.nextTick(); await Promise.resolve(); await Vue.nextTick() }
async function mount(component: Vue.Component, tenant: Tenant | null = null) {
  const root = node('root')
  const app = renderer.createApp(component, component === Panel ? { info: null } : component === Dialog ? { modelValue: true, tenant, info: null } : {})
  for (const name of components) app.component(name, name === 'ElTable' ? Table : name === 'ElTableColumn' ? Column : name === 'ElForm' ? Form : stub(name.slice(2)))
  app.directive('loading', {})
  app.directive('perm', {})
  app.mount(root)
  disposers.push(() => app.unmount())
  const dialog = walk(root).find(el => el.tag === 'Dialog')
  if (typeof dialog?.props.onOpen === 'function') (dialog.props.onOpen as () => void)()
  await settle()
  return root
}
function find(root: TreeNode, predicate: (el: TreeNode) => boolean): TreeNode {
  const found = walk(root).find(predicate)
  if (!found) throw new Error('Test control not found')
  return found
}
async function click(el: TreeNode) {
  await (el.props.onClick as (event: object) => unknown)({ stopPropagation() {}, preventDefault() {} })
  await settle()
}
async function change(el: TreeNode, value: unknown) {
  ;(el.props['onUpdate:modelValue'] as (value: unknown) => void)(value)
  await settle()
}
const metricsValues = (root: TreeNode) => walk(root).filter(el => el.tag === 'Radio' && Object.prototype.hasOwnProperty.call(dict.METRICS_SOURCE, String(el.props.value))).map(el => el.props.value)
beforeEach(() => { vi.clearAllMocks(); tenants = [seed()]; taskRows = []; tenantScope = null })
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()) })

describe('AOM / 服务原生元数据和租户配置', () => {
  it('连通性失败显示实际服务端点和底座配置提示，兼容旧测试结果', async () => {
    tenants[0].last_check = {
      ok: false,
      configuration_warnings: ['请核对底座导出表 region0_id'],
      checks: [{ name: 'ECS 云服务器列表', kind: 'service', ok: false, message: '域名解析失败',
        endpoint: 'https://ecs.region-qa.example.invalid', endpoint_source: 'global', network_reason: 'dns', retryable: false }],
    }
    const root = await mount(Panel)
    expect(text(root)).toContain('https://ecs.region-qa.example.invalid')
    expect(text(root)).toContain('请核对底座导出表 region0_id')
    expect(text(root)).toContain('域名解析失败')
    const check = find(root, el => String(el.props.class).split(' ').includes('check'))
    expect(check.props.title).toContain('https://ecs.region-qa.example.invalid\n域名解析失败')
  })

  it('AOM 仅用于旧配置停用说明，不列入可用能力或自动来源', () => {
    expect(dict.SERVICES.aom).toBe('AOM（已停用）')
    expect(dict.CAPABILITY).toEqual({ prometheus: 'Prometheus', cce_api: 'CCE K8s API' })
    expect(dict.METRICS_SOURCE.AOM.label).toBe('AOM（已停用）')
    expect(dict.METRICS_SOURCE.AUTO.desc).not.toContain('AOM')
    expect(dict.METRICS_SOURCE.AUTO.desc).not.toContain('CES')
    expect(dict.METRICS_SOURCE.NONE.desc).toContain('服务接口')
    expect(dict.METRICS_SOURCE.CES.label).toContain('旧配置')
    expect(tenantForm.TENANT_METRICS_SOURCES).not.toHaveProperty('AOM')
    expect(tenantForm.ENDPOINT_SERVICES).not.toContain('aom')
    expect(tenantForm.DEFAULT_ENDPOINTS).not.toHaveProperty('aom')
  })
  it('旧 AOM 转为 NONE，保留已有服务地址但不生成地址或更换成 Prometheus', () => {
    const old = seed('AOM')
    old.prometheus_url = 'https://prometheus.example.test'
    old.endpoints = { aom: 'https://aom.example.test', ecs: 'https://ecs.example.test' }
    const form = tenantForm.toTenantIn(old)
    expect(form.metrics_source).toBe('NONE')
    form.metrics_source = 'AOM'
    expect(tenantForm.cleanTenantIn(form)).toMatchObject({ metrics_source: 'NONE', prometheus_url: old.prometheus_url, endpoints: old.endpoints })
    expect(old.metrics_source).toBe('AOM')
  })
  it('新租户仅显示 AUTO、Prometheus、服务原生，不能选 AOM 或新建 AOM 地址', async () => {
    const root = await mount(Dialog)
    expect(metricsValues(root)).toEqual(['AUTO', 'PROMETHEUS', 'NONE'])
    expect(text(root)).toContain('可选 ECS 使用率来源')
    expect(text(root)).not.toContain('供 ECS 按专用映射接入')
    expect(text(root)).toContain('DCS 本地历史')
    expect(text(root)).toContain('GaussDB 使用原生接口')
    expect(text(root)).not.toContain('CES')
    await click(find(root, el => el.tag === 'Button' && text(el) === '添加覆盖'))
    const selector = find(root, el => el.tag === 'Select' && el.props.class === 'ep-k')
    expect(selector.props.allowCreate).not.toBe(true)
    expect(walk(root).filter(el => el.tag === 'Option').some(el => el.props.value === 'aom')).toBe(false)
  })
  it('旧 AOM 租户打开时选择服务原生，原配置提示停用，保存提交 NONE', async () => {
    const tenant = seed('AOM')
    tenant.prometheus_url = 'https://prometheus.example.test'
    const root = await mount(Dialog, tenant)
    expect(metricsValues(root)).not.toContain('AOM')
    const selected = find(root, el => el.tag === 'Radio' && el.props.value === 'NONE')
    expect(selected.props.modelValue ?? selected.props['model-value']).toBe('NONE')
    expect(walk(root).some(el => el.tag === 'Alert' && String(el.props.title).includes('旧 AOM 来源已停用'))).toBe(true)
    await click(find(root, el => el.tag === 'Button' && text(el) === '保存修改'))
    expect(api.updateTenant).toHaveBeenCalledExactlyOnceWith(7, expect.objectContaining({ metrics_source: 'NONE', prometheus_url: tenant.prometheus_url }))
    expect(tenant.metrics_source).toBe('AOM')
  })
  it('旧 AOM 地址只读展示停用，可删除，不提供编辑入口', async () => {
    const tenant = seed('AOM')
    tenant.endpoints = { aom: 'https://aom.example.test' }
    const root = await mount(Dialog, tenant)
    const selector = find(root, el => el.tag === 'Select' && el.props.class === 'ep-k')
    const input = find(root, el => el.tag === 'Input' && el.props.placeholder === 'AOM 已停用，旧地址不再使用')
    expect(selector.props.disabled).toBe(true)
    expect(input.props.disabled).toBe(true)
    await click(find(root, el => el.tag === 'Button' && text(el) === '保存修改'))
    expect(api.updateTenant.mock.calls[0][1]).toMatchObject({ metrics_source: 'NONE', endpoints: tenant.endpoints })
  })
  it('用户明确选择 Prometheus 才切换通道并保留实际地址', async () => {
    const tenant = seed('AOM')
    const root = await mount(Dialog, tenant)
    await click(find(root, el => el.tag === 'Radio' && el.props.value === 'PROMETHEUS'))
    await change(find(root, el => el.tag === 'Input' && el.props.placeholder === '填写内网实际 Prometheus 地址'), ' https://prometheus.example.test ')
    await click(find(root, el => el.tag === 'Button' && text(el) === '保存修改'))
    expect(api.updateTenant).toHaveBeenCalledExactlyOnceWith(7, expect.objectContaining({ metrics_source: 'PROMETHEUS', prometheus_url: 'https://prometheus.example.test' }))
  })
  it('旧 CES 配置可读可保存，已有 endpoint 不变', async () => {
    const tenant = seed('CES')
    tenant.endpoints = { ces: 'https://legacy.example.test/metrics' }
    const root = await mount(Dialog, tenant)
    expect(metricsValues(root)).toContain('CES')
    expect(text(root)).toContain('CES（旧配置）')
    await click(find(root, el => el.tag === 'Button' && text(el) === '保存修改'))
    expect(api.updateTenant).toHaveBeenCalledExactlyOnceWith(7, expect.objectContaining({ metrics_source: 'CES', endpoints: tenant.endpoints }))
    expect(tenant.metrics_source).toBe('CES')
  })
  it.each([true, false, null])('旧 AOM 能力为 %s 也不显示为当前可用通道', async available => {
    const tenant = seed('AOM')
    tenant.metrics_effective = 'PROMETHEUS'
    tenant.prometheus_url = 'https://prometheus.example.test'
    tenant.capabilities = { aom: { available, reason: '旧探测记录', at: '' } }
    tenants = [tenant]
    const root = await mount(Panel)
    const capRows = walk(root).filter(el => String(el.props.class).split(' ').includes('cap'))
    expect(capRows.some(el => text(el).includes('AOM'))).toBe(false)
    expect(text(root)).toContain('配置AOM（已停用）')
    expect(text(root)).toContain('实际生效仅服务原生')
    expect(text(root)).toContain('指标：仅服务原生')
    expect(text(root)).not.toContain('需配置内网实际 Endpoint 和任务指标映射')
    expect(api.testTenant).not.toHaveBeenCalled()
  })
  it('以前通过的 AOM 连通性记录显示停用历史，不能再显示为当前成功能力', async () => {
    tenants[0].last_check = { ok: true, checks: [{ name: 'AOM 指标接口', kind: 'capability', ok: true }] }
    const root = await mount(Panel)
    const check = find(root, el => String(el.props.class).split(' ').includes('check'))
    expect(text(check)).toContain('AOM 指标接口（已停用，历史）')
    expect(String(check.props.class).split(' ')).toContain('off')
    expect(String(check.props.class).split(' ')).not.toContain('ok')
    expect(tenants[0].last_check.checks?.[0].ok).toBe(true)
  })
  it.each([['AUTO', 'NONE'], ['AUTO', 'PROMETHEUS'], ['PROMETHEUS', 'PROMETHEUS'], ['CES', 'CES'], ['NONE', 'NONE']] as const)('来源 %s 继续按服务端 %s 显示', async (configured, effective) => {
    tenants = [{ ...seed(configured), metrics_effective: effective }]
    const root = await mount(Panel)
    expect(text(root)).toContain(`实际生效${dict.METRICS_SOURCE[effective].label}`)
  })
})

const task = (code = 'ecs_status', aom?: object): InspectionTask => ({
  code, name: 'QA 采集任务', service: 'ecs', interval_sec: 300, cron: null, enabled: true,
  params: { window_hours: 6, ...(aom ? { aom } : {}) }, runs: [], next_run: null, next_run_source: 'estimate',
  types: ['ecs'], note: null, min_interval: 60, skipped_tenants: [],
})
const mapping = () => ({ namespace: 'QA.NAMESPACE', dimension: 'resource_id', metrics: { cpu_util: 'qa_cpu_percent' } })
async function openTask(row = task()) {
  taskRows = [row]
  const root = await mount(Tasks)
  await click(find(root, el => el.tag === 'Button' && text(el) === '修改'))
  return root
}
const saveTask = (root: TreeNode) => click(find(root, el => el.tag === 'Button' && text(el) === '保存'))
const taskCodes = ['ecs_status', 'ecs_metrics', 'gaussdb_status', 'dcs_status', 'data_cleanup', 'rds_status', 'mrs_job_status', 'constructor', '__proto__']

describe('TaskPanel AOM 全面停用', () => {
  it.each(taskCodes)('%s 不显示映射编辑器，保存仅更新调度字段', async code => {
    const root = await openTask(task(code))
    expect(walk(root).some(el => el.props['aria-label'] === 'AOM 指标映射 JSON')).toBe(false)
    expect(text(root)).not.toContain('AOM 指标映射')
    await saveTask(root)
    expect(api.updateTask).toHaveBeenCalledExactlyOnceWith(code, expect.objectContaining({ enabled: true }))
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('aom')
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('params')
  })
  it.each(taskCodes)('%s 旧映射仅提示停用，未勾选清理不回传', async code => {
    const row = task(code, { ...mapping(), credential: 'FAKE_SECRET' })
    const original = structuredClone(row.params)
    const root = await openTask(row)
    expect(text(root)).toContain('AOM 已停用，旧映射不再生效')
    expect(text(root)).not.toContain('FAKE_SECRET')
    expect(find(root, el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射').props.modelValue).toBe(false)
    await saveTask(root)
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('aom')
    expect(row.params).toEqual(original)
  })
  it.each(taskCodes)('%s 清理旧映射仅提交 aom:null，不上传 params', async code => {
    const row = task(code, mapping())
    const original = structuredClone(row.params)
    const root = await openTask(row)
    await change(find(root, el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射'), true)
    await saveTask(root)
    expect(api.updateTask).toHaveBeenCalledExactlyOnceWith(code, expect.objectContaining({ aom: null }))
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('params')
    expect(row.params).toEqual(original)
  })
  it.each([null, undefined])('空旧映射 %s 不显示清理操作', async value => {
    const row = task()
    row.params = { aom: value }
    const root = await openTask(row)
    expect(walk(root).some(el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射')).toBe(false)
    await saveTask(root)
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('aom')
  })
  it('取消清理后重开恢复未勾选，取消期间没有写请求或修改源对象', async () => {
    const row = task('ecs_metrics', mapping())
    const original = structuredClone(row.params)
    const root = await openTask(row)
    await change(find(root, el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射'), true)
    await click(find(root, el => el.tag === 'Button' && text(el) === '取消'))
    expect(api.updateTask).not.toHaveBeenCalled()
    expect(row.params).toEqual(original)
    await click(find(root, el => el.tag === 'Button' && text(el) === '修改'))
    expect(find(root, el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射').props.modelValue).toBe(false)
    await saveTask(root)
    expect(api.updateTask.mock.calls[0][1]).not.toHaveProperty('aom')
  })
  it('保存清理中阻止重复提交，使用已捕获的 null 配置', async () => {
    let finish!: (value: { changed: boolean }) => void
    api.updateTask.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const root = await openTask(task('ecs_metrics', mapping()))
    const checkbox = find(root, el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射')
    await change(checkbox, true)
    const button = find(root, el => el.tag === 'Button' && text(el) === '保存')
    const first = (button.props.onClick as () => Promise<void>)()
    await settle()
    expect(checkbox.props.disabled).toBe(true)
    await (button.props.onClick as () => Promise<void>)()
    expect(api.updateTask).toHaveBeenCalledOnce()
    expect(api.updateTask.mock.calls[0][1]).toHaveProperty('aom', null)
    finish({ changed: true })
    await first
  })
  it('租户范围管理员不能编辑共享任务或清除共享映射', async () => {
    tenantScope = [7]
    taskRows = [task('ecs_metrics', mapping())]
    const root = await mount(Tasks)
    expect(walk(root).some(el => el.tag === 'Button' && text(el) === '修改')).toBe(false)
    expect(walk(root).some(el => el.tag === 'Checkbox' && text(el) === '清除旧 AOM 映射')).toBe(false)
    expect(api.updateTask).not.toHaveBeenCalled()
  })
})
