import { afterEach, describe, expect, it, vi } from 'vitest'
import type { GaussNativeRows } from '../src/types/gaussdb'
import { deferred } from './businessViewHarness'
import { allNodes, clickButton, findNode, settlePersonnel, textOf } from './personnelHarness'
import { createGaussModel, gaussButtons, gaussInspection, gaussInstance, gaussMetric, gaussOverview, gaussRows, inspectionRecord, mountGauss, submitGauss, updateGauss } from './gaussFixtures'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).reverse().forEach(fn => fn()))
const mount: typeof mountGauss = (file, options) => {
  const view = mountGauss(file, options); dispose.push(view.unmount); return view
}
async function setup(file = 'GaussWorkspace.vue', permissions?: string[]) {
  const state = createGaussModel(permissions); dispose.push(state.stop)
  await settlePersonnel(); state.model.selectInstance(state.model.items.value[0]); state.model.setPanel('overview')
  const canRun = state.allowed.permissions.includes('task:run')
  const onBack = vi.fn(), onPanel = vi.fn()
  const view = mount(file, { props: { model: state.model, canRun, onBack, onPanel }, dependencies: state.dependencies })
  return { ...state, ...view, onBack, onPanel }
}
const emptyDescriptions = (root: Parameters<typeof textOf>[0]) => allNodes(root).filter(node => node.tag === 'Empty').map(node => node.props.description)

// root 负责自动准备只读数据；这些测试独立操作真实子组件的表单、按钮和公开事件。
describe('GaussDB 工作区与查询子组件', () => {
  it('概要展示配置原值和节点，返回与查看会话通过公开事件通知 root', async () => {
    const view = await setup(); await view.model.queryOverview(); await settlePersonnel()
    expect(textOf(view.root)).toContain('数据库 11'); expect(textOf(view.root)).toContain('8 vCPU')
    expect(textOf(view.root)).toContain('32 （单位未确认）')
    expect(textOf(view.root)).toContain('cn-1'); expect(textOf(view.root)).toContain('dn-2')
    expect(allNodes(view.root).filter(node => node.tag === 'TabPane').map(node => node.props.name)).toEqual(['overview', 'performance', 'slow-sql', 'sessions'])
    await clickButton(view.root, '实例列表'); expect(view.onBack).toHaveBeenCalledOnce()
    await clickButton(view.root, '查看会话'); expect(view.onPanel).toHaveBeenCalledWith('sessions')
    expect(view.api.sessions).not.toHaveBeenCalled()
    await clickButton(view.root, '导出 Excel')
    expect(view.api.export).toHaveBeenCalledExactlyOnceWith(11, { panel: 'overview' }, expect.objectContaining({ tenantId: 7 }))
  })
  it('概览先展示最近一次巡检采集的磁盘与组件，宕机组件高亮，不发在线请求', async () => {
    const state = createGaussModel(); dispose.push(state.stop); await settlePersonnel()
    const local = { ...gaussInstance(), disk_state: 'AVAILABLE' as const, disk_usage: 91.5, disk_used_gb: 91.5, disk_total_gb: 100,
      disk_observed_at: '2026-11-02T01:58:00Z', disk_freshness: 'FRESH' as const, active_alarms: 2,
      components: { state: 'AVAILABLE' as const, observed_at: '2026-11-02T01:58:00Z', freshness: 'FRESH' as const, total: 2, down: 1, unknown: 0, by_type: { DN: 2 }, warning: null },
      component_items: [
        { node_id: 'node-1', node_name: 'dn-1', component_id: 'dn_6001', component_type: 'DN', role: 'master', status: 'Primary' },
        { node_id: 'node-2', node_name: 'dn-2', component_id: 'dn_6002', component_type: 'DN', role: 'slave', status: 'Down' },
      ] }
    expect(await state.model.openInstance(11, async () => local)).toBe(true)
    const view = mount('GaussWorkspace.vue', { props: { model: state.model, canRun: true }, dependencies: state.dependencies })
    await settlePersonnel()
    const text = textOf(view.root)
    expect(text).toContain('91.5%'); expect(text).toContain('1 个组件宕机'); expect(text).toContain('dn_6002'); expect(text).toContain('宕机')
    expect(text).toContain('最近一次巡检采集的结果')
    expect(findNode(view.root, 'Table', node => Array.isArray(node.props.data) && node.props.data.length === 2)).toBeTruthy()
    expect(gaussButtons(view.root)).toContain('查询实例数据')
    for (const key of ['overview', 'slowNodes', 'slowSql', 'sessions', 'inspections'] as const) expect(state.api[key]).not.toHaveBeenCalled()
  })
  it('viewer 概要说明在线查询权限，所有标签只发 panel 事件', async () => {
    const view = await setup('GaussWorkspace.vue', ['ledger:view'])
    expect(textOf(view.root)).toContain('在线查询需管理员授予执行巡检权限')
    expect(gaussButtons(view.root)).not.toContain('查询实例数据'); expect(gaussButtons(view.root)).not.toContain('导出 Excel')
    await updateGauss(view.root, 'Tabs', undefined, 'sessions')
    expect(view.onPanel).toHaveBeenCalledWith('sessions')
    expect(view.api.overview).not.toHaveBeenCalled(); expect(view.api.sessions).not.toHaveBeenCalled()
  })
  it('取消在途概要保留当前标签，并拒绝迟到概要', async () => {
    const view = await setup(), pending = deferred<ReturnType<typeof gaussOverview>>()
    view.api.overview.mockReturnValueOnce(pending.promise)
    const querying = (findNode(view.root, 'Button', node => textOf(node) === '查询实例数据').props.onClick as () => Promise<unknown>)()
    await settlePersonnel(); await clickButton(view.root, '取消查询')
    expect(view.api.overview.mock.calls[0][1].signal.aborted).toBe(true)
    pending.resolve(gaussOverview()); await querying; await settlePersonnel()
    expect(view.model.panel.value).toBe('overview'); expect(view.model.overview.value).toBeNull()
    expect(emptyDescriptions(view.root)).toContain('暂无在线节点数据')
  })
  it('慢 SQL 节点选择和查询缺一不可，阈值为秒，详情事件使用查询记录', async () => {
    const view = await setup('GaussQueryPanel.vue'); view.model.setPanel('slow-sql')
    await view.model.loadSlowNodes(); await settlePersonnel()
    expect(findNode(view.root, 'Button', node => textOf(node) === '查询').props.disabled).toBe(true)
    await updateGauss(view.root, 'Select', '慢 SQL 节点', ['node-1'])
    await updateGauss(view.root, 'InputNumber', '慢 SQL 阈值秒', 5)
    expect(view.api.slowSql).not.toHaveBeenCalled()
    await submitGauss(view.root)
    expect(view.api.slowSql.mock.calls[0][1]).toMatchObject({ node_ids: ['node-1'], threshold: 5, start_at: '2026-11-02T01:00:00.000Z', end_at: '2026-11-02T02:00:00.000Z' })
    expect(textOf(view.root)).toContain('慢 SQL 阈值（秒）')
    const result = findNode(view.root, 'Component', node => node.props.kind === 'slow-sql')
    await (result.props.onDetail as (row: object) => unknown)(view.model.rows.value!.items[0]); await settlePersonnel()
    expect(view.api.slowDetail.mock.calls[0][1]).toEqual({ start_at: '2026-11-02T01:00:00.000Z', end_at: '2026-11-02T02:00:00.000Z', node_ids: ['node-1'], sql_id: 'sql-1' })
    await clickButton(view.root, '导出 Excel')
    expect(view.api.export.mock.calls[0][1]).toEqual({ panel: 'slow-sql', ...view.api.slowSql.mock.calls[0][1] })
  })
  it('会话仅允许真实 CN/DN，选中组件后仍需查询；更改条件禁用旧导出', async () => {
    const view = await setup('GaussQueryPanel.vue'); view.model.setPanel('sessions'); await view.model.queryOverview(); await settlePersonnel()
    const options = allNodes(view.root).filter(node => node.tag === 'Option').map(node => node.props.value)
    expect(options).toContain(JSON.stringify(['node-1', 'cn-1'])); expect(options).toContain(JSON.stringify(['node-2', 'dn-2']))
    expect(options).not.toContain(JSON.stringify(['node-3', 'cm-3']))
    expect(findNode(view.root, 'Button', node => textOf(node) === '查询').props.disabled).toBe(true)
    await updateGauss(view.root, 'Select', '会话组件', JSON.stringify(['node-2', 'dn-2']))
    await updateGauss(view.root, 'Input', '会话用户', ' user ')
    expect(view.api.sessions).not.toHaveBeenCalled()
    await submitGauss(view.root)
    expect(view.api.sessions.mock.calls[0][1]).toMatchObject({ node_id: 'node-2', component_id: 'dn-2', user_name: 'user', limit: 50 })
    await clickButton(view.root, '导出 Excel')
    expect(view.api.export.mock.calls[0][1]).toEqual({ panel: 'sessions', ...view.api.sessions.mock.calls[0][1] })
    await updateGauss(view.root, 'Input', '会话用户', 'other')
    expect(findNode(view.root, 'Button', node => textOf(node) === '导出 Excel').props.disabled).toBe(true)
    expect(gaussButtons(view.root).some(label => /终止|扩容|执行 SQL/.test(label))).toBe(false)
  })
  it('慢 SQL / 会话往返保留各自表单、结果和可导出的 applied，页面不串 tab', async () => {
    const view = await setup('GaussQueryPanel.vue'); view.model.setPanel('slow-sql'); await view.model.loadSlowNodes(); await settlePersonnel()
    await updateGauss(view.root, 'Select', '慢 SQL 节点', ['node-1']); await updateGauss(view.root, 'InputNumber', '慢 SQL 阈值秒', 5)
    await submitGauss(view.root)
    const slowResult = view.model.rows.value
    view.model.setPanel('sessions'); await view.model.queryOverview(); await settlePersonnel()
    expect(emptyDescriptions(view.root)).toContain('选择节点组件查询当前会话')
    await updateGauss(view.root, 'Select', '会话组件', JSON.stringify(['node-2', 'dn-2']))
    await updateGauss(view.root, 'Input', '会话用户', '保留用户'); await submitGauss(view.root)
    const sessionResult = view.model.rows.value
    view.model.setPanel('slow-sql'); await settlePersonnel()
    expect(findNode(view.root, 'Component', node => node.props.kind === 'slow-sql').props.result).toBe(slowResult)
    expect(findNode(view.root, 'InputNumber', node => node.props['aria-label'] === '慢 SQL 阈值秒').props.modelValue).toBe(5)
    await clickButton(view.root, '导出 Excel'); expect(view.api.export.mock.calls.at(-1)![1]).toMatchObject({ panel: 'slow-sql', threshold: 5 })
    view.model.setPanel('sessions'); await settlePersonnel()
    expect(findNode(view.root, 'Component', node => node.props.kind === 'sessions').props.result).toBe(sessionResult)
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === '会话用户').props.modelValue).toBe('保留用户')
    await clickButton(view.root, '导出 Excel'); expect(view.api.export.mock.calls.at(-1)![1]).toMatchObject({ panel: 'sessions', user_name: '保留用户' })
    expect(view.api.slowSql).toHaveBeenCalledOnce(); expect(view.api.sessions).toHaveBeenCalledOnce()
  })
  it.each(['slow-sql', 'sessions'] as const)('viewer 的 %s 查询和选择禁用，即使提交表单也不请求', async panel => {
    const view = await setup('GaussQueryPanel.vue', ['ledger:view']); view.model.setPanel(panel); await settlePersonnel()
    expect(findNode(view.root, 'Button', node => textOf(node) === '查询').props.disabled).toBe(true)
    const label = panel === 'slow-sql' ? '慢 SQL 节点' : '会话组件'
    expect(findNode(view.root, 'Select', node => node.props['aria-label'] === label).props.disabled).toBe(true)
    await (findNode(view.root, 'form').props.onSubmit as (event: { preventDefault: () => void }) => unknown)({ preventDefault() {} })
    expect(view.api.slowSql).not.toHaveBeenCalled(); expect(view.api.sessions).not.toHaveBeenCalled()
    expect(gaussButtons(view.root)).not.toContain('导出 Excel')
  })
})

describe('GaussDB 性能报告嵌入与分页', () => {
  it('跳过运行中记录，自动打开首条 completed 报告，右侧直接嵌入详情', async () => {
    const view = await setup('GaussPerformancePanel.vue'); view.model.setPanel('performance')
    view.api.inspections.mockResolvedValueOnce(gaussRows({ items: [
      { ...inspectionRecord, record_id: 'running', status: 'running' },
      { ...inspectionRecord, record_id: 'done-2', task_name: '已完成报告' },
      { ...inspectionRecord, record_id: 'done-3' },
    ], total: 3 }))
    await view.model.queryPanel(); await settlePersonnel()
    expect(view.api.inspectionDetail).toHaveBeenCalledExactlyOnceWith(11, 'done-2', expect.objectContaining({ tenantId: 7 }))
    const records = allNodes(view.root).filter(node => node.tag === 'button')
    expect(records.map(node => node.props.disabled)).toEqual([true, false, false])
    expect(records.map(node => node.props['aria-pressed'])).toEqual([false, true, false])
    const article = findNode(view.root, 'article', node => node.props['aria-label'] === '巡检报告详情')
    expect(allNodes(article).some(node => node.tag === 'Component' && node.props.result === view.model.detail.value)).toBe(true)
    expect(allNodes(view.root).some(node => node.tag === 'Dialog' || node.tag === 'Drawer')).toBe(false)
    await (records[2].props.onClick as () => unknown)(); await settlePersonnel()
    expect(view.api.inspectionDetail.mock.calls.at(-1)![1]).toBe('done-3')
    await clickButton(view.root, '导出报告 Excel')
    expect(view.api.export.mock.calls.at(-1)![1]).toEqual({ panel: 'inspection-detail', record_id: 'done-3' })
  })
  it('报告下一页/上一页/每页条数立即查询，同步 offset 并自动读取新报告', async () => {
    const view = await setup('GaussPerformancePanel.vue'); view.model.setPanel('performance')
    await view.model.queryPanel(); await settlePersonnel()
    await clickButton(view.root, '下一页')
    expect(view.api.inspections.mock.calls.at(-1)![1]).toEqual({ limit: 20, offset: 20 })
    expect(view.api.inspectionDetail).toHaveBeenCalledTimes(2)
    await clickButton(view.root, '上一页')
    expect(view.api.inspections.mock.calls.at(-1)![1]).toEqual({ limit: 20, offset: 0 })
    await updateGauss(view.root, 'Select', '巡检每页条数', 10)
    expect(view.api.inspections.mock.calls.at(-1)![1]).toEqual({ limit: 10, offset: 0 })
    expect(view.api.inspections).toHaveBeenCalledTimes(4)
    expect(findNode(view.root, 'Button', node => textOf(node) === '上一页').props.disabled).toBe(true)
    await clickButton(view.root, '导出记录 Excel')
    expect(view.api.export.mock.calls.at(-1)![1]).toEqual({ panel: 'inspections', limit: 10, offset: 0 })
  })
  it('报告时间筛选应用后分页和导出保留同一个范围', async () => {
    const view = await setup('GaussPerformancePanel.vue'); view.model.setPanel('performance')
    await updateGauss(view.root, 'DatePicker', '巡检时间范围', [new Date('2026-11-01T00:00:00Z'), new Date('2026-11-02T00:00:00Z')])
    expect(view.api.inspections).not.toHaveBeenCalled()
    await submitGauss(view.root, '查询报告'); await clickButton(view.root, '下一页')
    const expected = { start_at: '2026-11-01T00:00:00.000Z', end_at: '2026-11-02T00:00:00.000Z', limit: 20, offset: 20 }
    expect(view.api.inspections.mock.calls.at(-1)![1]).toEqual(expected)
    await clickButton(view.root, '导出记录 Excel'); expect(view.api.export.mock.calls.at(-1)![1]).toEqual({ panel: 'inspections', ...expected })
  })
  it('空报告和只有运行中报告均不请求详情，显示选择空态', async () => {
    const view = await setup('GaussPerformancePanel.vue'); view.model.setPanel('performance')
    view.api.inspections.mockResolvedValueOnce(gaussRows({ items: [], total: 0, status: 'NO_DATA' }))
    await view.model.queryPanel(); await settlePersonnel()
    expect(emptyDescriptions(view.root)).toContain('未查到巡检记录')
    expect(view.api.inspectionDetail).not.toHaveBeenCalled()
    expect(findNode(view.root, 'Button', node => textOf(node) === '下一页').props.disabled).toBe(true)
    view.api.inspections.mockResolvedValueOnce(gaussRows({ items: [{ ...inspectionRecord, status: 'running' }], total: 1 }))
    await submitGauss(view.root, '查询报告')
    expect(emptyDescriptions(view.root)).toContain('选择一份已完成报告查看指标和检查结果')
    expect(findNode(view.root, 'button').props.disabled).toBe(true); expect(view.api.inspectionDetail).not.toHaveBeenCalled()
  })
  it('viewer 报告筛选/分页禁用，已有记录也不自动读取在线详情', async () => {
    const view = await setup('GaussPerformancePanel.vue', ['ledger:view']); view.model.setPanel('performance')
    view.model.rows.value = gaussRows({ items: [{ ...inspectionRecord }], total: 41 }); await settlePersonnel()
    expect(view.api.inspectionDetail).not.toHaveBeenCalled()
    for (const label of ['查询报告', '下一页', '上一页']) expect(findNode(view.root, 'Button', node => textOf(node) === label).props.disabled).toBe(true)
    expect(findNode(view.root, 'button').props.disabled).toBe(true)
    expect(gaussButtons(view.root)).not.toContain('导出记录 Excel')
  })
  it('切租户取消正在读取的内嵌报告，迟到报告不展示', async () => {
    const view = await setup('GaussPerformancePanel.vue'), pending = deferred<GaussNativeRows>()
    view.model.setPanel('performance'); view.api.inspectionDetail.mockReturnValueOnce(pending.promise)
    await view.model.queryPanel(); await settlePersonnel()
    const signal = view.api.inspectionDetail.mock.calls[0][2].signal
    view.app.tenantId = 8; await settlePersonnel(); expect(signal.aborted).toBe(true)
    pending.resolve(gaussInspection()); await settlePersonnel()
    expect(view.model.detail.value).toBeNull(); expect(view.model.rows.value).toBeNull()
    expect(gaussButtons(view.root)).not.toContain('导出报告 Excel')
  })
})

describe('GaussDB 指标分类与本地目录子组件', () => {
  it('报告未提供任何 CPU/内存序列时显示空态，仍保留检查结果', async () => {
    const result = { ...gaussInspection(), metric_series: [] }
    const view = mount('GaussInspectionDetail.vue', { props: { result, record: inspectionRecord } }); await settlePersonnel()
    expect(emptyDescriptions(view.root)).toContain('此报告未提供可绘制的 CPU 或内存指标')
    expect(textOf(view.root)).toContain('发现高负载')
    expect(allNodes(view.root).some(node => node.props.series || node.props.option)).toBe(false)
  })
  it.each([['cpu', 'memory', '此报告没有内存指标'], ['memory', 'cpu', '此报告没有 CPU 指标']] as const)('仅有 %s 时切到 %s 显示缺失空态，不补零', async (available, missing, message) => {
    const view = mount('GaussReportChart.vue', { props: { series: [gaussMetric(available)] } }); await settlePersonnel()
    const chart = findNode(view.root, 'Component', node => !!node.props.option)
    const option = chart.props.option as { series: { data: unknown[][] }[] }
    expect(option.series[0].data.map(point => point[1])).toEqual([0, null])
    await updateGauss(view.root, 'RadioGroup', '巡检指标分类', missing)
    expect(emptyDescriptions(view.root)).toContain(message)
    expect(allNodes(view.root).some(node => node.props.option)).toBe(false)
    expect(textOf(view.root)).not.toContain('0 %'); expect(textOf(view.root)).not.toContain('0 GB')
    await updateGauss(view.root, 'RadioGroup', '巡检指标分类', 'all')
    expect(findNode(view.root, 'Component', node => !!node.props.option)).toBeTruthy()
  })
  it('无有效采样保留报告统计值与未知单位，切新报告重置指标选择', async () => {
    const metric = { ...gaussMetric('memory'), unit: null, points: [{ observed_at: '2026-11-02T01:00:00Z', value: null }] }
    const view = mount('GaussReportChart.vue', { props: { series: [metric] } }); await settlePersonnel()
    expect(emptyDescriptions(view.root)).toContain('该指标没有有效时间序列')
    expect(textOf(view.root)).toContain('45 （单位未确认）')
    expect(allNodes(view.root).some(node => node.props.option)).toBe(false)
    await updateGauss(view.root, 'RadioGroup', '巡检指标分类', 'cpu')
    view.updateProps({ series: [gaussMetric('cpu')] }); await settlePersonnel()
    expect(findNode(view.root, 'RadioGroup').props.modelValue).toBe('all')
    expect(allNodes(view.root).some(node => node.props.option)).toBe(true)
  })
  it('实例列表项目选择来自独立目录，筛选和分页查询真实模型，点击发送实例事件', async () => {
    const state = createGaussModel(); dispose.push(state.stop); await settlePersonnel()
    const onOpen = vi.fn(), projects = [{ project_ref: 202, project_name: '第二页项目', tenant_id: 7, tenant_name: '租户甲', region: null }]
    const view = mount('GaussInstanceList.vue', { props: { model: state.model, projects, projectsError: '', onOpen }, dependencies: state.dependencies })
    expect(allNodes(view.root).filter(node => node.tag === 'Option').map(node => node.props.value)).toEqual([202])
    await clickButton(view.root, '数据库 11'); expect(onOpen).toHaveBeenCalledWith(gaussInstance())
    await updateGauss(view.root, 'Input', '实例关键字', ' db ')
    await submitGauss(view.root)
    expect(state.api.instances.mock.calls.at(-1)![0]).toMatchObject({ keyword: 'db', page: 1 })
    await (findNode(view.root, 'Pagination').props['onUpdate:currentPage'] as (value: number) => unknown)(2); await settlePersonnel()
    expect(state.api.instances.mock.calls.at(-1)![0].page).toBe(2)
    await updateGauss(view.root, 'Select', 'GaussDB 项目', 202)
    state.api.instances.mockResolvedValueOnce({ items: [], total: 0, page: 1, page_size: 20 })
    await submitGauss(view.root)
    expect(state.api.instances.mock.calls.at(-1)![0]).toMatchObject({ project_ref: 202, page: 1 })
    expect(emptyDescriptions(view.root)).toContain('当前范围没有 GaussDB 实例')
    expect(gaussButtons(view.root)).not.toContain('查看')
    expect(onOpen).toHaveBeenCalledOnce()
  })
})
