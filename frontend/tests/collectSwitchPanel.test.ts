import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import type { CollectScopes, CollectSwitches, ScopeRule } from '../src/types'
import * as collectScope from '../src/views/system/collect-scope'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(fn => fn()))
// 复用项目 renderer，额外渲染 Alert 的标题槽以操作说明折叠按钮。
const Alert = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) { return () => Vue.h('Alert', attrs, [slots.title?.(), slots.default?.()]) },
})

const rule: ScopeRule = { effect: 'exclude', match: { namespace: ['test-*'] } }
function fixture(): CollectSwitches {
  return {
    categories: [{ code: 'compute', name: '计算与容器' }, { code: 'storage', name: '存储与数据库' }],
    tenants: [
      { id: 7, code: 'dev', name: '研发与运维联合验证租户', short_name: '', color: '#409eff', enabled: true },
      { id: 8, code: 'disabled', name: '已停用的验证租户', short_name: '', color: '#67c23a', enabled: false },
    ],
    types: [
      { code: 'cce_workload', name: '容器工作负载（Deployment / StatefulSet）', service: 'CCE', service_name: '云容器引擎', category: 'compute', ledger: false },
      { code: 'dcs_instance', name: '分布式缓存实例', service: 'DCS', service_name: '分布式缓存服务', category: 'storage', ledger: true },
    ].map(type => ({ ...type, tasks: [], global: true, tenants: {
      7: { setting: null, effective: true, resources: 123456, active_alarms: 12345 },
      8: { setting: false, effective: false, resources: 42, active_alarms: 0 },
    } })),
    task_types: {}, global_editable: true,
  }
}

async function mount(options: { manage?: boolean; globalEditable?: boolean; scopeFailure?: boolean } = {}) {
  const data = fixture()
  data.global_editable = options.globalEditable ?? true
  const scopes: CollectScopes = {
    tenants: data.tenants, global_editable: data.global_editable,
    limits: { rules: 100, patterns: 200, pattern_length: 200 },
    types: data.types.map(type => ({
      code: type.code, name: type.name, service: type.service, category: type.category,
      dimensions: [{ key: 'namespace', label: '命名空间' }],
      global: { rules: [rule], updated_by: null, updated_at: null },
      tenants: Object.fromEntries(data.tenants.map(tenant => [tenant.id, {
        setting: [rule], source: 'tenant', rules: [rule], resources: 123456, excluded: 12345,
        updated_by: null, updated_at: null,
      }])),
    })),
  }
  const api = {
    getCollectSwitches: vi.fn(async () => structuredClone(data)),
    getCollectScopes: vi.fn(async () => {
      if (options.scopeFailure) throw new Error('范围读取失败')
      return structuredClone(scopes)
    }),
    listTasks: vi.fn(async () => []),
    switchImpact: vi.fn(async () => ({ stop: ['研发与运维联合验证租户 · 容器工作负载'], start: [], active_alarms: 12345 })),
    saveCollectSwitches: vi.fn(async () => ({ changed: [], closed_alarms: 12345 })),
  }
  const view = mountPersonnel('system/CollectSwitchPanel.vue', {
    api, can: permission => permission !== 'collect:manage' || options.manage !== false,
    dependencies: {
      './collect-scope': collectScope,
      vue: { ...Vue, resolveComponent: (name: string) => name === 'el-alert' ? Alert : Vue.resolveComponent(name) },
    },
  })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, api }
}

type Node = Parameters<typeof textOf>[0]
const withClass = (root: Node, name: string) => allNodes(root).filter(node => String(node.props.class).split(/\s+/).includes(name))
async function update(node: Node, value: unknown, event = 'onUpdate:modelValue') {
  await (node.props[event] as (value: unknown) => unknown)(value)
  await settlePersonnel()
}

describe('采集开关矩阵实际模板', () => {
  it('说明折叠按钮切换展开状态并关联完整说明，不改动开关草稿', async () => {
    const view = await mount()
    const toggle = findNode(view.root, 'Button', node => textOf(node) === '展开说明')
    expect(toggle.props['aria-expanded']).toBe(false)
    expect(toggle.props['aria-controls']).toBe('collect-switch-rules')
    await clickButton(view.root, '展开说明')
    expect(findNode(view.root, 'Button', node => textOf(node) === '收起说明').props['aria-expanded']).toBe(true)
    expect(textOf(view.root)).toContain('该类资源的活动告警自动关闭并留处理记录')
    expect(findNode(view.root, 'Button', node => textOf(node) === '保存').props.disabled).toBe(true)
    await clickButton(view.root, '收起说明')
    expect(findNode(view.root, 'Button', node => textOf(node) === '展开说明').props['aria-expanded']).toBe(false)
  })

  it('长资源名称、大计数与范围摘要完整渲染，状态文字和状态点在同一组', async () => {
    const view = await mount()
    expect(textOf(view.root)).toContain('容器工作负载（Deployment / StatefulSet）')
    expect(withClass(view.root, 'eff-state').map(textOf)).toEqual(['采集中', '采集中', '已关闭', '已关闭'])
    expect(withClass(view.root, 'eff-state').every(node => withClass(node, 'dot').length === 1)).toBe(true)
    expect(withClass(view.root, 'eff').map(textOf)).toContain('采集中· 资源 123456· 告警 12345')
    expect(withClass(view.root, 'scope-line')).toHaveLength(6)
    expect(withClass(view.root, 'scope-line').map(textOf)).toContain('范围：1 条规则 · 范围外 12345设置范围')
    expect(textOf(findNode(view.root, 'span', node => String(node.props.class).includes('off-summary'))))
      .toBe('2 个“租户 × 资源类型”已关闭采集')
  })

  it('分组后的分类和搜索仍筛选实际资源行', async () => {
    const view = await mount()
    await update(findNode(view.root, 'Segmented'), 'storage')
    expect(textOf(view.root)).toContain('分布式缓存实例')
    expect(textOf(view.root)).not.toContain('容器工作负载（Deployment / StatefulSet）')
    await update(findNode(view.root, 'Input'), '没有匹配的服务')
    expect(allNodes(view.root).filter(node => node.tag === 'Select')).toHaveLength(0)
    await update(findNode(view.root, 'Input'), 'DCS')
    expect(allNodes(view.root).filter(node => node.tag === 'Select')).toHaveLength(2)
  })

  it('租户开关先暂存，保存先显示告警影响，确认后才提交', async () => {
    const view = await mount()
    const select = findNode(view.root, 'Select')
    await update(select, 'off', 'onChange')
    expect(textOf(view.root)).toContain('保存（1 项）')
    expect(view.api.saveCollectSwitches).not.toHaveBeenCalled()
    await clickButton(view.root, '保存（1 项）')
    expect(view.api.switchImpact).toHaveBeenCalledWith([{ resource_type: 'cce_workload', tenant_id: 7, enabled: false }])
    expect(textOf(findNode(view.root, 'Dialog'))).toContain('12345 条活动告警，保存后会自动关闭（写入处理记录）')
    expect(view.api.saveCollectSwitches).not.toHaveBeenCalled()
    await clickButton(view.root, '确认保存')
    expect(view.api.saveCollectSwitches).toHaveBeenCalledExactlyOnceWith([{ resource_type: 'cce_workload', tenant_id: 7, enabled: false }])
  })

  it('放弃修改恢复跟随全局并禁用保存按钮', async () => {
    const view = await mount()
    await update(findNode(view.root, 'Select'), 'off', 'onChange')
    await clickButton(view.root, '放弃修改')
    expect(findNode(view.root, 'Select').props['model-value']).toBe('follow')
    expect(findNode(view.root, 'Button', node => textOf(node) === '保存').props.disabled).toBe(true)
    expect(view.api.switchImpact).not.toHaveBeenCalled()
  })

  it('范围按钮仍定位到对应租户和资源类型', async () => {
    const view = await mount()
    const scopeLine = withClass(view.root, 'scope-line')[2]!
    await clickButton(scopeLine, '设置范围')
    const dialog = findNode(view.root, 'Component')
    expect(dialog.props['modelValue']).toBe(true)
    expect(dialog.props['tenant-id']).toBe(7)
    expect(dialog.props['tenant-name']).toBe('研发与运维联合验证租户')
    expect(dialog.props.type).toMatchObject({ code: 'cce_workload' })
    expect(dialog.props['can-manage']).toBe(true)
  })

  it('只读用户仍可查看范围，但没有保存操作且开关均禁用', async () => {
    const view = await mount({ manage: false })
    expect(allNodes(view.root).filter(node => ['Switch', 'Select'].includes(node.tag)).every(node => node.props.disabled)).toBe(true)
    expect(withClass(view.root, 'toolbar-actions').flatMap(node => allNodes(node).filter(child => child.tag === 'Button'))).toHaveLength(0)
    expect(withClass(view.root, 'scope-line').map(textOf).every(text => text.endsWith('查看范围'))).toBe(true)
    await clickButton(withClass(view.root, 'scope-line')[0]!, '查看范围')
    expect(findNode(view.root, 'Component').props['can-manage']).toBe(false)
    expect(view.api.switchImpact).not.toHaveBeenCalled()
  })

  it('受限权限只禁用全局默认，租户范围入口保持可管理', async () => {
    const view = await mount({ globalEditable: false })
    expect(allNodes(view.root).filter(node => node.tag === 'Switch').every(node => node.props.disabled)).toBe(true)
    expect(allNodes(view.root).filter(node => node.tag === 'Select').every(node => !node.props.disabled)).toBe(true)
    await clickButton(withClass(view.root, 'scope-line')[0]!, '查看范围')
    expect(findNode(view.root, 'Component').props['can-manage']).toBe(false)
    await clickButton(withClass(view.root, 'scope-line')[2]!, '设置范围')
    expect(findNode(view.root, 'Component').props['can-manage']).toBe(true)
  })

  it('范围读取失败不影响开关矩阵和暂存操作', async () => {
    const view = await mount({ scopeFailure: true })
    expect(withClass(view.root, 'scope-line')).toHaveLength(0)
    await update(findNode(view.root, 'Select'), 'off', 'onChange')
    expect(textOf(view.root)).toContain('保存（1 项）')
  })
})
