import { readFileSync } from 'node:fs'
import { workspaceById, workspaceMeta } from '../src/config/workspace-navigation'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { allNodes, findNode, settlePersonnel, textOf } from './personnelHarness'
import * as utils from '../src/views/gaussdb/gauss-utils'
import type { GaussNativeStatus } from '../src/types/gaussdb'
import { createGaussModel, gaussButtons as buttons, gaussInspection, gaussOverview, gaussRows, inspectionRecord, mountGauss } from './gaussFixtures'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).reverse().forEach(fn => fn()))
const mount: typeof mountGauss = (file, options) => {
  const view = mountGauss(file, options)
  dispose.push(view.unmount)
  return view
}

describe('GaussDB 原生结果的实际模板', () => {
  it('磁盘保留 0 和缺失值，不用查询时间冒充采样时间', async () => {
    const view = mount('GaussDiskPanel.vue', { props: { overview: gaussOverview() } }); await settlePersonnel()
    expect(textOf(view.root)).toContain('0 GB'); expect(textOf(view.root)).toContain('0 %')
    expect(textOf(view.root)).toContain('源端未提供')
    expect(textOf(view.root)).not.toContain('dn-2')
    const updated = gaussOverview(); updated.disk.used_gb = null; updated.disk.usage_percent = null
    view.updateProps({ overview: updated }); await settlePersonnel()
    expect(allNodes(view.root).filter(node => node.tag === 'strong').map(textOf)).toEqual(['—', '100 GB', '—'])
  })
  it('本地接收时间明确标注，缺失采样时间不以 queried_at 伪造', async () => {
    const overview = gaussOverview()
    overview.disk.time_basis = 'RESPONSE_RECEIVED'; overview.disk.collected_at = '2026-11-01T00:00:00Z'
    const view = mount('GaussDiskPanel.vue', { props: { overview } }); await settlePersonnel()
    expect(textOf(view.root)).toContain('获取时间：'); expect(textOf(view.root)).toContain('（本地接收）')
    expect(textOf(view.root)).not.toContain('源端采样时间：')
    const missing = gaussOverview(); view.updateProps({ overview: missing }); await settlePersonnel()
    expect(textOf(view.root)).toContain('源端采样时间：源端未提供')
    expect(allNodes(view.root).some(node => node.tag === 'Table')).toBe(false)
  })
  it('慢 SQL 耗时以微秒原值显示，保留 SQL 文本且支持只读展开', async () => {
    const view = mount('GaussNativeTable.vue', { props: { kind: 'slow-sql', canRun: true, result: gaussRows({ items: [{ sql_id: 'sql-1', sql_text: '<img src=x onerror=alert(1)>', avg_exec_time: '5000000', avg_cpu_time: '1000000', avg_io_time: null }] }) } }); await settlePersonnel()
    const labels = allNodes(view.root).filter(node => node.tag === 'TableColumn').map(node => node.props.label)
    expect(labels).toContain('平均执行耗时（μs）'); expect(labels).toContain('平均 CPU 耗时（μs）')
    expect(textOf(view.root)).toContain('5000000'); expect(textOf(view.root)).toContain('<img src=x onerror=alert(1)>')
    expect(allNodes(view.root).some(node => node.tag === 'img')).toBe(false)
    expect(buttons(view.root)).toEqual(['展开'])
  })
  it('会话不声称全量，保留秒数和原始 query_runtime，不提供写操作', async () => {
    const view = mount('GaussNativeTable.vue', { props: { kind: 'sessions', result: gaussRows({ items: [{ session_id: '1', exec_time: '97', query_runtime: '00:00:22.641774' }], truncated: true }) } }); await settlePersonnel()
    expect(textOf(view.root)).toContain('源端未提供总数'); expect(textOf(view.root)).toContain('结果已截断')
    expect(textOf(view.root)).toContain('00:00:22.641774')
    const labels = allNodes(view.root).filter(node => node.tag === 'TableColumn').map(node => node.props.label)
    expect(labels).toContain('会话建立时长（秒）'); expect(labels).toContain('语句执行时长（原始字符串）')
    expect(buttons(view.root)).toEqual([])
  })
  it('慢 SQL UTC 日期字符串保留原值，时间表头明确标注原生时间', async () => {
    const start = '2024-03-26 11:23:47 +0000', finish = '2024-03-26 11:23:51 +0000'
    const view = mount('GaussNativeTable.vue', { props: { kind: 'slow-sql', result: gaussRows({ items: [{ start_time: start, finish_time: finish }] }) } }); await settlePersonnel()
    const labels = allNodes(view.root).filter(node => node.tag === 'TableColumn').map(node => node.props.label)
    expect(labels).toContain('开始时间（原生时间）'); expect(labels).toContain('结束时间（原生时间）')
    expect(textOf(view.root)).toContain(start); expect(textOf(view.root)).toContain(finish)
    expect(allNodes(view.root).some(node => node.props.title === `原生时间：${start}`)).toBe(true)
    expect(labels.some(label => String(label).includes('本地'))).toBe(false)
  })
  it('慢 SQL 详情显示新增字段中文标签及微秒，UTC 日期字符串保持原文', async () => {
    const state = createGaussModel(); dispose.push(state.stop)
    const { model, api } = state
    await settlePersonnel(); model.selectInstance(model.items.value[0]); model.setPanel('slow-sql')
    await model.loadSlowNodes(); model.slowFilters.node_ids = ['node-1']; await model.queryPanel()
    const view = mount('GaussWorkspace.vue', { props: { model, canRun: true }, dependencies: state.dependencies })
    const start = '2024-03-26 11:23:47 +0000', finish = '2024-03-26 11:23:51 +0000'
    api.slowDetail.mockResolvedValueOnce(gaussRows({ items: [{ start_time: start, finish_time: finish,
      fetched_pages: 50, hit_pages: 25, total_time: '1000000', cpu_time: '500000', plan_time: '100000', io_time: '400000', lock_time: '1000', lock_count: 1 }],
      units: { total_time: '微秒', cpu_time: '微秒', plan_time: '微秒', io_time: '微秒', lock_time: '微秒' } }))
    await model.showDetail(model.rows.value!.items[0]); await settlePersonnel()
    const text = textOf(view.root)
    for (const label of ['扫描页数', '命中页数', '总耗时（微秒）', 'CPU 耗时（微秒）', '计划耗时（微秒）', 'I/O 耗时（微秒）', '加锁耗时（微秒）', '加锁次数', '开始时间（原生时间）', '结束时间（原生时间）']) expect(text).toContain(label)
    expect(text).toContain(start); expect(text).toContain(finish); expect(text).toContain('1000000')
  })
  it('巡检列表时间按毫秒、真实检查计数渲染，运行中禁用详情', async () => {
    const view = mount('GaussNativeTable.vue', { props: { kind: 'inspections', canRun: true, result: gaussRows({ items: [{ ...inspectionRecord, status: 'running' }], units: { start_time: 'UTC 毫秒', end_time: 'UTC 毫秒' } }) } }); await settlePersonnel()
    expect(textOf(view.root)).toContain(utils.inspectionTime(inspectionRecord.start_time))
    expect(allNodes(view.root).some(node => node.props.label === '异常项数')).toBe(true)
    expect(allNodes(view.root).some(node => node.props.label === '开始时间（本地）')).toBe(true)
    expect(allNodes(view.root).some(node => node.props.title === `原始时间：${inspectionRecord.start_time}（UTC 毫秒）`)).toBe(true)
    expect(findNode(view.root, 'Button', node => textOf(node) === '展开').props.disabled).toBe(true)
  })
  it('巡检详情兼容对象和数组，单位各自保留，报告时间与采样时间分开', async () => {
    const view = mount('GaussInspectionDetail.vue', { props: { result: gaussInspection(), record: inspectionRecord } }); await settlePersonnel()
    const text = textOf(view.root)
    expect(text).toContain('报告时间'); expect(text).toContain('检查结果')
    expect(text).toContain('指标单位：%'); expect(text).toContain('采样单位：%')
    expect(text).toContain('指标单位：MB'); expect(text).toContain('采样单位：GB')
    expect(text).toContain(utils.inspectionTime(1709163360000))
    expect(text).toContain('0'); expect(text).toContain('—')
    const tables = allNodes(view.root).filter(node => node.tag === 'Table')
    expect(tables.map(node => (node.props.data as unknown[]).length)).toEqual([2, 1])
    expect((tables[0].props.data as { value: unknown }[]).map(point => point.value)).toEqual([0, null])
    const result = findNode(view.root, 'p', node => textOf(node).includes('发现高负载'))
    let ancestor = result.parent
    while (ancestor) { expect(ancestor.tag).not.toBe('details'); ancestor = ancestor.parent }
    const raw = findNode(view.root, 'details')
    expect(raw.props.open).toBeUndefined(); expect(textOf(raw)).toContain('检查项与原始采样')
    expect(allNodes(raw)).toContain(tables[0]); expect(allNodes(raw)).toContain(tables[1])
  })
  it.each(['AVAILABLE', 'PARTIAL', 'ERROR'] as GaussNativeStatus[])('详情 %s 仅在非 AVAILABLE 时传递 warning，避免重复语义说明', async status => {
    const result = { ...gaussInspection(), status, warning: '源端限制或错误' }
    const view = mount('GaussInspectionDetail.vue', { props: { result, record: inspectionRecord } }); await settlePersonnel()
    const state = findNode(view.root, 'Component', node => node.props.status === status)
    expect(state.props.warning).toBe(status === 'AVAILABLE' ? null : '源端限制或错误')
    expect(textOf(view.root)).toContain('发现高负载')
  })
  it.each(['NO_DATA', 'PARTIAL', 'UNAVAILABLE', 'ERROR', 'DISABLED', 'UNSUPPORTED'] as GaussNativeStatus[])('%s 空态和能力限制明确标注', async status => {
    const view = mount('GaussNativeStatus.vue', { props: { status, warning: '接口返回的限制', queriedAt: null } }); await settlePersonnel()
    expect(textOf(view.root)).toContain(utils.NATIVE_STATUS[status]); expect(textOf(view.root)).toContain('接口返回的限制')
  })
  it('路由和监控菜单均要求 ledger:view', () => {
    const router = readFileSync(resolve(import.meta.dirname, '../src/router/index.ts'), 'utf8')
    expect(router).toMatch(/path: 'gaussdb'.*GaussView\.vue.*workspaceMeta\('\/gaussdb'\)/)
    expect(workspaceMeta('/gaussdb')).toEqual({ title: 'GaussDB 监控', perm: 'ledger:view', activeMenu: '/monitoring' })
    expect(workspaceById('monitoring').items).toContainEqual(expect.objectContaining({ path: '/gaussdb', perm: 'ledger:view' }))
  })
})
