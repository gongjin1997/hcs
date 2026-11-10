import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { parse } from '@vue/compiler-sfc'
import type { InspectionTask, TaskRun } from '../src/api'
import * as scopePresentation from '../src/views/system/collect-scope'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
const task: InspectionTask = {
  code: 'mrs_job_status', name: 'MRS 作业巡检', service: 'mrs', interval_sec: 120, cron: null,
  enabled: true, params: {}, runs: [], next_run: null, next_run_source: 'scheduler', types: ['mrs_job'],
  note: '仅查询云作业', min_interval: 30, skipped_tenants: [], checks: ['job_status'], steps: ['查询清单', '查询详情'],
}
const cardProps = () => ({ task, serviceName: 'MRS', cadence: '每 2 分钟', scopeLabel: '租户甲 · 已启用项目', resourceLabel: 'MRS 原生执行',
  nextRunLabel: '后端未上报', checks: ['作业状态'], steps: task.steps, checksProvided: true, canEdit: true, canRun: true, submitting: false, toggling: false,
  active: false, hasEcsPolicy: false, hasJobSelection: false })
function mountCard(props: Record<string, unknown> = {}) {
  const view = mountPersonnel('system/TaskDefinitionCard.vue', { props: { ...cardProps(), ...props } })
  dispose.push(view.unmount)
  return view
}

describe('任务配置卡片与详情', () => {
  it('MRS 显示服务、租户/项目、资源种类和执行参数 / 资源范围入口', async () => {
    const configure = vi.fn(), scope = vi.fn(), run = vi.fn()
    const view = mountCard({ onConfigure: configure, onScope: scope, onRun: run })
    for (const label of ['MRS', '每 2 分钟', '租户甲 · 已启用项目', 'MRS 原生执行', '作业状态']) expect(textOf(view.root)).toContain(label)
    await clickButton(view.root, '执行参数')
    await clickButton(view.root, '资源范围')
    await clickButton(view.root, '立即执行')
    expect(configure).toHaveBeenCalledOnce()
    expect(scope).toHaveBeenCalledOnce()
    expect(run).toHaveBeenCalledOnce()
    expect(allNodes(view.root).some(node => node.tag === 'Table')).toBe(false)
  })

  it('无告警规则的采集任务显示实际步骤，不误报后端缺数据', () => {
    const view = mountCard({ checks: [], steps: ['筛选已启用集群', '读取节点与节点池'] })
    expect(textOf(view.root)).toContain('主要步骤：筛选已启用集群、读取节点与节点池')
    expect(textOf(view.root)).not.toContain('后端未提供检查项')
  })

  it('只读用户保留详情与范围查看，不能触发执行或修改', async () => {
    const onToggle = vi.fn(), onDetail = vi.fn()
    const view = mountCard({ canEdit: false, canRun: false, onToggle, onDetail })
    const buttons = allNodes(view.root).filter(node => node.tag === 'Button').map(textOf)
    expect(buttons).toEqual(['任务详情', '资源范围'])
    const toggle = findNode(view.root, 'Switch')
    expect(toggle.props.disabled).toBe(true)
    ;(toggle.props.onChange as (value: boolean) => void)(false)
    expect(onToggle).not.toHaveBeenCalled()
    await clickButton(view.root, '任务详情')
    expect(onDetail).toHaveBeenCalledOnce()
  })

  it('已停用和已有活跃执行均不能重复执行，状态不取决于本地 loading', () => {
    const view = mountCard({ active: true })
    expect(findNode(view.root, 'Button', node => textOf(node) === '已有活跃执行').props.disabled).toBe(true)
    const stopped = mountCard({ task: { ...task, enabled: false } })
    expect(findNode(stopped.root, 'Button', node => textOf(node) === '立即执行').props.disabled).toBe(true)
    expect(textOf(stopped.root)).toContain('已停用')
  })

  it('ECS 与 JobSelection 的复用配置入口通过事件交还工作区', async () => {
    const onEcs = vi.fn(), onJobs = vi.fn()
    const view = mountCard({ hasEcsPolicy: true, hasJobSelection: true, onEcs, onJobs })
    await clickButton(view.root, '监控配置')
    await clickButton(view.root, '监控作业')
    expect(onEcs).toHaveBeenCalledOnce()
    expect(onJobs).toHaveBeenCalledOnce()
  })

  it('详情中的说明步骤默认折叠，周期与资源范围操作独立', async () => {
    const onConfigure = vi.fn(), onScope = vi.fn()
    const view = mountPersonnel('system/TaskDefinitionDrawer.vue', { props: { modelValue: true, task, serviceName: 'MRS', cadence: '每 2 分钟',
      scopeLabel: '租户甲 · 项目乙', checks: ['作业状态'], steps: task.steps, checksProvided: true, canEdit: true, timeoutLabel: '120 秒', onConfigure, onScope } })
    dispose.push(view.unmount)
    expect(findNode(view.root, 'details').props.open).toBeUndefined()
    expect(textOf(view.root)).toContain('查询清单查询详情')
    expect(textOf(view.root)).toContain('规则检查与执行步骤')
    expect(textOf(view.root)).toContain('120 秒')
    await clickButton(view.root, '执行参数 / 周期')
    await clickButton(view.root, '查看 / 配置资源范围')
    expect(onConfigure).toHaveBeenCalledOnce()
    expect(onScope).toHaveBeenCalledOnce()
  })

  it('详情区分没有告警规则与任务定义缺字段', () => {
    const noRules = mountPersonnel('system/TaskDefinitionDrawer.vue', { props: { modelValue: true, task, serviceName: 'CCE', cadence: '每 2 分钟',
      scopeLabel: '租户甲 · 项目乙', checks: [], steps: ['读取节点'], checksProvided: true, canEdit: false } })
    dispose.push(noRules.unmount)
    expect(textOf(noRules.root)).toContain('未配置告警规则检查项')
    expect(textOf(noRules.root)).toContain('读取节点')
    const missing = mountPersonnel('system/TaskDefinitionDrawer.vue', { props: { modelValue: true, task, serviceName: 'CCE', cadence: '每 2 分钟',
      scopeLabel: '租户甲 · 项目乙', checks: [], steps: [], checksProvided: false, canEdit: false } })
    dispose.push(missing.unmount)
    expect(textOf(missing.root)).toContain('旧任务定义未提供规则检查项')
  })

  it('组件模板可编译，样式限定在任务工作区', () => {
    for (const file of ['TaskDefinitionCard.vue', 'TaskDefinitionDrawer.vue', 'TaskRunHistory.vue', 'TaskScopeDialog.vue']) {
      const { descriptor, errors } = parse(readFileSync(new URL(`../src/views/system/${file}`, import.meta.url), 'utf8'))
      expect(errors).toEqual([])
      expect(descriptor.styles.every(style => style.scoped)).toBe(true)
    }
  })
})

describe('历史执行与资源范围保持真实返回', () => {
  it('未结束的历史运行不猜超时，缺失错误不生成失败原因', () => {
    const running: TaskRun = { id: 19, tenant_id: 7, tenant_name: '租户甲', project_ref: 11, trigger_type: 'MANUAL', started_at: '2020-01-01T00:00:00Z',
      finished_at: null, status: 'RUNNING', resource_total: 0, abnormal_total: 0, api_calls: 0, error_summary: null, stats: {} }
    const view = mountPersonnel('system/TaskRunHistory.vue', { props: { task: { ...task, runs: [running] }, describeRun: () => '',
      status: (value: string) => ({ label: value }), projectLabel: () => '项目乙' } })
    dispose.push(view.unmount)
    expect(textOf(view.root)).toContain('RUNNING')
    expect(textOf(view.root)).toContain('尚未结束')
    expect(textOf(view.root)).not.toContain('超时')
    expect(allNodes(view.root).some(node => node.props.class === 'run-error')).toBe(false)
  })

  it('只读范围沿用现有查看器，向下传递 can-manage=false', async () => {
    const getCollectScopes = vi.fn(async () => ({ global_editable: false, tenants: [{ id: 7, name: '租户甲' }], limits: { rules: 20, patterns: 10, pattern_length: 120 },
      types: [{ code: 'mrs_job', name: 'MRS 原生执行', dimensions: [], global: { rules: [] }, tenants: { '7': { source: 'none', rules: [], excluded: 0 } } }] }))
    const view = mountPersonnel('system/TaskScopeDialog.vue', { props: { modelValue: true, task, tenantId: null, canManage: false },
      api: { getCollectScopes }, dependencies: { './collect-scope': scopePresentation } })
    dispose.push(view.unmount)
    await settlePersonnel()
    expect(textOf(view.root)).toContain('全部采集')
    await clickButton(view.root, '查看资源范围')
    const editor = findNode(view.root, 'Component', node => 'can-manage' in node.props)
    expect(editor.props['can-manage']).toBe(false)
    expect(editor.props['tenant-id']).toBe(7)
    expect(editor.props.modelValue).toBe(true)
    expect(getCollectScopes).toHaveBeenCalledOnce()
  })

  it('切换租户后，迟到的资源范围响应不能替换当前租户配置', async () => {
    const resolvers: ((value: object) => void)[] = []
    const getCollectScopes = vi.fn(() => new Promise<object>(resolve => { resolvers.push(resolve) }))
    const view = mountPersonnel('system/TaskScopeDialog.vue', { props: { modelValue: true, task, tenantId: 7, canManage: true },
      api: { getCollectScopes }, dependencies: { './collect-scope': scopePresentation } })
    dispose.push(view.unmount)
    view.updateProps({ tenantId: 8 }); await settlePersonnel()
    const response = (id: number) => ({ global_editable: false, tenants: [{ id, name: `租户${id}` }], limits: { rules: 20, patterns: 10, pattern_length: 120 },
      types: [{ code: 'mrs_job', name: 'MRS 原生执行', dimensions: [], global: { rules: [] }, tenants: { [id]: { source: 'none', rules: [], excluded: 0 } } }] })
    resolvers[1]!(response(8)); await settlePersonnel()
    resolvers[0]!(response(7)); await settlePersonnel()
    await clickButton(view.root, '编辑资源范围')
    expect(findNode(view.root, 'Component', node => 'can-manage' in node.props).props['tenant-id']).toBe(8)
  })
})
