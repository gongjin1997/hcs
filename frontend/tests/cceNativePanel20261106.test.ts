import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as Vue from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import * as display from '../src/views/pods/cce-native-display'
import { nativePanelData } from '../src/views/pods/cce-native-display'
import { mountBusinessScript, settleView } from './businessViewHarness'

type JsonRecord = Record<string, unknown>
const stops: (() => void)[] = []
afterEach(() => stops.splice(0).forEach(stop => stop()))

function metric(fields: JsonRecord = {}) {
  return { status: 'ok', reason: 'usage_valid', identity: { name: 'worker', namespace: 'ns', uid: 'pod-uid' },
    cpu_cores: 0.125, memory_bytes: 1610612736, timestamp: '2026-11-06T03:00:00Z', window_seconds: 30,
    age_seconds: 15, fresh: true, containers: [], ...fields }
}
function source(items: unknown[] = [metric()], fields: JsonRecord = {}) {
  return { status: 'ok', reason: 'metrics_samples_available', complete: true, items, ...fields }
}
function metrics(pods: unknown = source(), nodes: unknown = source([metric({ identity: { name: 'node', namespace: null } })]),
  fields: JsonRecord = {}) {
  return { status: 'ok', reason: 'metrics_collected', discovery: { status: 'ok', version: 'v1beta1' }, pods, nodes, ...fields }
}
function identity(kind: string, name: string, uid: unknown = `${kind}-uid`, namespace = 'ns') {
  return { kind, name, namespace, uid, api_version: kind === 'Pod' ? 'v1' : `${kind === 'Job' || kind === 'CronJob' ? 'batch' : 'apps'}/v1` }
}
function ownership(fields: JsonRecord = {}) {
  return { status: 'ok', reason: 'owner_chain_verified', pod: identity('Pod', 'app-random'),
    direct_owner: identity('ReplicaSet', 'opaque-middle'), top_owner: identity('Deployment', 'business-app'), ...fields }
}
function owners(items: unknown[] = [ownership()], fields: JsonRecord = {}) {
  return { status: 'ok', reason: 'owners_resolved', items,
    sources: { ReplicaSet: { status: 'ok', reason: 'list_complete', complete: true } }, ...fields }
}
const podRows = (payload: JsonRecord) => nativePanelData(payload).metrics[0].rows

// Render the actual SFC template; only Element Plus tab/tag shells are substituted.
const filename = resolve(import.meta.dirname, '../src/views/pods/CceNativePanel.vue')
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const compiled = compileScript(descriptor, { id: 'cce-native-panel', inlineTemplate: true })
const { outputText } = ts.transpileModule(compiled.content, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
})
const exports: { default?: Vue.Component } = {}
new Function('require', 'exports', outputText)((name: string) => {
  if (name === 'vue') return Vue
  if (name === './cce-native-display') return display
  throw new Error(`Unexpected native panel dependency: ${name}`)
}, exports)
async function render(native: JsonRecord) {
  const app = Vue.createSSRApp(exports.default!, { native })
  const passthrough = Vue.defineComponent({ setup: (_props, context) => () => Vue.h('div', context.slots.default?.()) })
  app.component('el-tabs', passthrough)
  app.component('el-tab-pane', Vue.defineComponent({
    props: ['label', 'name'], setup: (props, context) => () => Vue.h('section', { 'aria-label': props.label }, context.slots.default?.()),
  }))
  app.component('el-tag', passthrough)
  return renderToString(app)
}

describe('CCE 原生指标单位与证据状态', () => {
  it('展示 CPU 核数、MiB、上游时间和窗口，不推算百分比或容量', async () => {
    const html = await render({ metrics: metrics() })
    expect(html).toContain('CPU（核）')
    expect(html).toContain('内存（MiB）')
    expect(html).toContain('>0.125<')
    expect(html).toContain('>1,536<')
    expect(html).toContain('datetime="2026-11-06T03:00:00Z"')
    expect(html).toContain('30 秒')
    expect(html).toContain('有效')
    expect(html).not.toContain('%')
    expect(html).not.toContain('容量')
  })
  it('零值与未知分开，纳核与微小 MiB 不被格式化成零', () => {
    const rows = podRows({ metrics: metrics(source([metric({ cpu_cores: 0, memory_bytes: 0 }),
      metric({ cpu_cores: null, memory_bytes: null }), metric({ cpu_cores: 1e-9, memory_bytes: 0.000001 })])) })
    expect(rows[0]).toMatchObject({ cpu: '0', memory: '0', state: '有效' })
    expect(rows[1]).toMatchObject({ cpu: '未知', memory: '未知' })
    expect(rows[2].cpu).toBe('0.000000001')
    expect(rows[2].memory).not.toBe('0')
  })
  it.each([null, undefined, true, false, '0', '0.125', NaN, Infinity, -1, {}, []])('非法数值 %s 保留未知', value => {
    const row = podRows({ metrics: metrics(source([metric({ cpu_cores: value, memory_bytes: value })])) })[0]
    expect(row.cpu).toBe('未知')
    expect(row.memory).toBe('未知')
  })
  it('部分样本仅显示已知字段，不将未知内存补零', () => {
    const row = podRows({ metrics: metrics(source([metric({ status: 'partial', reason: 'quantity_invalid', memory_bytes: null })])) })[0]
    expect(row).toMatchObject({ cpu: '0.125', memory: '未知', state: '部分数值未知' })
  })
  it('查询时标记过期后，保留的旧值明确标注上次样本', async () => {
    const native = { freshness: 'stale', metrics: metrics(source([metric({ fresh: false, status: 'partial', reason: 'sample_stale' })])) }
    const row = podRows(native)[0]
    expect(row).toMatchObject({ cpu: '上次 0.125', memory: '上次 1,536', state: '已过期（上次样本）', historical: true })
    const html = await render(native)
    expect(html).toContain('上次 0.125')
    expect(html).toContain('已过期（上次样本）')
    expect(html).toContain('datetime="2026-11-06T03:00:00Z"')
  })
  it.each(['stale', 'unknown', 'refresh_failed'])('快照时效 %s 不把旧样本显示为当前有效', freshness => {
    const row = podRows({ freshness, metrics: metrics() })[0]
    expect(row.state).not.toBe('有效')
    expect(row.cpu).not.toBe('0.125')
    expect(row.memory).not.toBe('1,536')
  })
  it.each([false, null, undefined, 'true', 1])('fresh 必须为布尔真，收到 %s 时数值未知', fresh => {
    const row = podRows({ metrics: metrics(source([metric({ fresh })])) })[0]
    expect(row.cpu).toBe('未知')
    expect(row.memory).toBe('未知')
  })
  it.each(['timestamp_invalid', 'window_invalid', 'window_excessive', 'timestamp_future',
    'metric_identity_invalid', 'metric_identity_duplicate', 'metric_type_invalid'])('无效证据 %s 即使有旧数值也不展示为有效', reason => {
    const row = podRows({ metrics: metrics(source([metric({ reason })])) })[0]
    expect(row.cpu).toBe('未知')
    expect(row.memory).toBe('未知')
  })
  it.each([{ timestamp: null }, { timestamp: 'invalid' }, { timestamp: '2026-11-06T03:00:00' },
    { window_seconds: null }, { window_seconds: 0 }, { window_seconds: -10 }, { window_seconds: '30' },
    { status: 'malformed' }, { identity: {} }])('缺失或畸形样本 %s 保留未知', fields => {
    const row = podRows({ metrics: metrics(source([metric(fields)])) })[0]
    expect(row.cpu).toBe('未知')
    expect(row.memory).toBe('未知')
  })
  it('完全采用后端时效判断，不按浏览器当前时间重新套阈值', () => {
    const row = podRows({ metrics: metrics(source([metric({ timestamp: '2000-01-01T00:00:00Z' })])) })[0]
    expect(row.state).toBe('有效')
  })
  it('不完整列表不展示残留行，Node 权限失败不影响 Pod', () => {
    const data = nativePanelData({ metrics: metrics(source([metric()], { status: 'partial', complete: false }),
      source([], { status: 'forbidden', complete: false })) })
    expect(data.metrics[0].rows).toEqual([])
    expect(data.metrics[0].hint).toContain('列表不完整')
    expect(data.metrics[1].status).toBe('无权限')
    const independent = nativePanelData({ metrics: metrics(source(), source([], { status: 'forbidden', complete: false }), { status: 'partial' }) })
    expect(independent.metrics[0].rows[0].cpu).toBe('0.125')
  })
})

describe('CCE 原生可选能力与空状态', () => {
  it.each([
    ['disabled', '未启用', '当前租户未启用'], ['forbidden', '无权限', '缺少集群只读权限'],
    ['unsupported', '不支持', '集群未提供此能力'], ['error', '查询失败', '本轮查询未成功'],
    ['malformed', '数据无效', '响应格式无效'],
  ])('状态 %s 使用简短中文，不展示为告警', async (status, label, hint) => {
    const html = await render({ metrics: { status, reason: 'PRIVATE-RAW-PAYLOAD' }, owners: { status, items: [], sources: {} } })
    expect(html).toContain(label)
    expect(html).toContain(hint)
    expect(html).not.toContain('告警')
    expect(html).not.toContain('PRIVATE-RAW-PAYLOAD')
    expect(html).not.toContain('<table')
  })
  it('空原生快照、空指标清单与未启用有不同文案', async () => {
    expect(await render({})).toContain('尚无原生采集数据')
    expect(await render({ metrics: metrics(source([]), source([])) })).toContain('暂无指标样本')
    expect(await render({ owners: owners([]) })).toContain('暂无 Pod 归属记录')
  })
  it('仅有归属时不额外展示指标标签页', async () => {
    const html = await render({ owners: owners() })
    expect(html).toContain('工作负载归属')
    expect(html).not.toContain('CPU（核）')
  })
  it('父级指标关闭或失败时不信任遗留子级数值', () => {
    const data = nativePanelData({ metrics: metrics(source(), source(), { status: 'disabled' }) })
    expect(data.metrics.every(section => !section.rows.length && section.status === '未启用')).toBe(true)
  })
})

describe('CCE Pod 直接与顶层归属', () => {
  it('显示命名空间、直接 ReplicaSet 和 UID 链已核对的 Deployment', async () => {
    const html = await render({ owners: owners() })
    expect(html).toContain('ReplicaSet · ns/opaque-middle')
    expect(html).toContain('Deployment · ns/business-app')
    expect(html).toContain('UID 链已核对')
    expect(html).toContain('ReplicaSet：可用')
  })
  it('支持经过核对的 Job 到 CronJob，不根据 Pod 名字猜测', () => {
    const row = nativePanelData({ owners: owners([ownership({ pod: identity('Pod', 'totally-unrelated'),
      direct_owner: identity('Job', 'opaque-job'), top_owner: identity('CronJob', 'schedule') })],
    { sources: { Job: { status: 'ok', complete: true } } }) }).owners.rows[0]
    expect(row.top).toBe('CronJob · ns/schedule')
  })
  it.each(['pod', 'direct_owner', 'top_owner'])('缺失 %s UID 时明确未知', field => {
    const item = ownership()
    const missing = { ...item, [field]: { ...(item[field as 'pod' | 'direct_owner' | 'top_owner'] as object), uid: undefined } }
    const row = nativePanelData({ owners: owners([missing]) }).owners.rows[0]
    expect(row.top).toBe('未知')
    expect(row.state).toContain('UID 链缺失')
  })
  it('同名但 UID 不匹配保留直接引用，顶层为未知', () => {
    const row = nativePanelData({ owners: owners([ownership({ status: 'partial', reason: 'owner_uid_mismatch', top_owner: null })], { status: 'partial' }) }).owners.rows[0]
    expect(row.top).toBe('未知')
    expect(row.direct).toContain('ReplicaSet · ns/opaque-middle')
    expect(row.state).toContain('UID 不匹配')
  })
  it('跨命名空间或来源未完成不得标记归属已核对', () => {
    const cross = nativePanelData({ owners: owners([ownership({ top_owner: identity('Deployment', 'business-app', 'top-uid', 'foreign') })]) }).owners.rows[0]
    expect(cross.top).toBe('未知')
    expect(cross.state).toContain('命名空间不匹配')
    const missing = nativePanelData({ owners: owners([ownership()], { sources: { ReplicaSet: { status: 'forbidden', complete: false } } }) }).owners.rows[0]
    expect(missing.top).toBe('未知')
  })
  it.each([undefined, 'private/v1', 'apps/../../v1'])('API 组或版本 %s 未核实时不展示已验证的顶层归属', api_version => {
    const row = nativePanelData({ owners: owners([ownership({ top_owner: { ...identity('Deployment', 'business-app'), api_version } })]) }).owners.rows[0]
    expect(row.top).toBe('未知')
    expect(row.state).toContain('未核实')
  })
  it.each(['bad uid', 1, '', null])('畸形 UID %s 不能作为归属链依据', uid => {
    const row = nativePanelData({ owners: owners([ownership({ top_owner: identity('Deployment', 'business-app', uid) })]) }).owners.rows[0]
    expect(row.top).toBe('未知')
    expect(row.state).toContain('UID 链缺失')
  })
  it('同 UID 循环和同 UID 不同名的伪直接归属不能显示已核对', () => {
    const pod = identity('Pod', 'worker')
    const cycle = nativePanelData({ owners: owners([ownership({ reason: 'direct_owner', pod,
      direct_owner: identity('StatefulSet', 'loop', pod.uid), top_owner: identity('StatefulSet', 'loop', pod.uid) })]) }).owners.rows[0]
    expect(cycle.top).toBe('未知')
    const changedName = nativePanelData({ owners: owners([ownership({ reason: 'owner_standalone',
      top_owner: identity('ReplicaSet', 'different-name') })]) }).owners.rows[0]
    expect(changedName.top).toBe('未知')
  })
  it('无顶层证据时不从 Pod 名称截取 Deployment', async () => {
    const html = await render({ owners: owners([ownership({ reason: 'owner_not_found', status: 'partial',
      pod: identity('Pod', 'obvious-app-123456789-abcde'), top_owner: null })], { status: 'partial' }) })
    expect(html).toContain('obvious-app-123456789-abcde')
    expect(html).not.toContain('Deployment · ns/obvious-app')
    expect(html).toContain('顶层归属未知')
  })
  it('裸 Pod、独立中间控制器和直接 StatefulSet 不臆造更高归属', () => {
    const pod = identity('Pod', 'bare'), rs = identity('ReplicaSet', 'standalone'), sts = identity('StatefulSet', 'stable')
    const rows = nativePanelData({ owners: owners([
      ownership({ reason: 'pod_unowned', pod, direct_owner: null, top_owner: pod }),
      ownership({ reason: 'owner_standalone', direct_owner: rs, top_owner: rs }),
      ownership({ reason: 'direct_owner', direct_owner: sts, top_owner: sts }),
    ]) }).owners.rows
    expect(rows.map(row => row.top)).toEqual(['Pod · ns/bare', 'ReplicaSet · ns/standalone', 'StatefulSet · ns/stable'])
    expect(rows[0].state).toBe('无控制器')
  })
  it('过期归属快照保留上次核对标识', () => {
    const row = nativePanelData({ freshness: 'stale', owners: owners() }).owners.rows[0]
    expect(row.state).toBe('上次核对（快照已过期）')
    expect(row.top).toBe('上次 Deployment · ns/business-app')
  })
  it('刷新失败时旧归属只作为上次快照展示', async () => {
    const html = await render({ freshness: 'refresh_failed', collected_at: '2026-11-06T03:00:00Z', owners: owners() })
    expect(html).toContain('上次 Deployment · ns/business-app')
    expect(html).toContain('上次核对（刷新失败）')
    expect(html).toContain('当前关系未知')
    expect(html).toContain('采集：')
  })
  it('对象名称通过 Vue 文本转义，原始 reason 不反射到页面', async () => {
    const html = await render({ owners: owners([ownership({ pod: identity('Pod', '<img src=x onerror=alert(1)>'),
      status: 'partial', reason: 'PRIVATE-RAW-PAYLOAD', top_owner: null })]) })
    expect(html).toContain('&lt;img')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('PRIVATE-RAW-PAYLOAD')
  })
})

describe('CCE 面板输入更新', () => {
  it('替换租户快照后立即移除旧行，不修改原始输入', async () => {
    const input = { metrics: metrics(), owners: owners() }
    const before = JSON.stringify(input)
    const view = mountBusinessScript<{ panel: ReturnType<typeof nativePanelData> }>('pods/CceNativePanel.vue', {
      props: { native: input }, dependencies: { './cce-native-display': display },
    })
    stops.push(view.unmount)
    expect(view.state.panel.metrics[0].rows[0].cpu).toBe('0.125')
    expect(JSON.stringify(input)).toBe(before)
    view.props.native = { metrics: { status: 'disabled' }, owners: { status: 'disabled', items: [] } }
    await settleView()
    expect(view.state.panel.metrics[0].rows).toEqual([])
    expect(view.state.panel.owners.rows).toEqual([])
    expect(JSON.stringify(input)).toBe(before)
    expect(view.messages.error).not.toHaveBeenCalled()
    expect(view.messages.warning).not.toHaveBeenCalled()
  })
  it.each([null, [], 'bad', 1])('畸形嵌套 JSON %s 不崩溃', value => {
    expect(() => nativePanelData({ metrics: value, owners: value })).not.toThrow()
  })
})
