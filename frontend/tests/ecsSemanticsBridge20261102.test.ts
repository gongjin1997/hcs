import { afterEach, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { ecsPolicyPayload, blankEcsPolicy, parseEcsPrometheus } from '../src/views/ecs/ecs-policy'
import * as policyUtils from '../src/views/ecs/ecs-policy'
import * as utils from '../src/views/ecs/ecs-utils'
import * as chartUtils from '../src/views/ecs/ecs-chart'
import { useEcsPolicy } from '../src/views/ecs/useEcsPolicy'
import { useEcsTrend } from '../src/views/ecs/useEcsTrend'
import { ecsHistory, ecsPolicy } from './ecsFixtures'
import { allNodes, clickButton, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const mapping = { queries: { cpu_util: 'measured_cpu_percent' }, instance_label: 'vm_id', units: { cpu_util: '%' },
  timestamp_queries: { cpu_util: 'measured_cpu_source_seconds' }, label_filters: { job: 'verified-exporter' } }
const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(fn => fn()))
const dependencies = { vue: Vue, './ecs-utils': utils, './ecs-policy': policyUtils, './ecs-chart': chartUtils,
  './useEcsPolicy': { useEcsPolicy }, './useEcsTrend': { useEcsTrend },
  '@/stores/app': { useAppStore: () => ({ tenants: [{ id: 7, name: '测试租户' }] }) },
  '@/stores/user': { useUserStore: () => ({ user: { id: 1, tenant_ids: [7] }, can: () => true, canAny: () => true }) } }

it('策略保存完整保留单位、源时间和精确标签，不改原配置', () => {
  const json = JSON.stringify(mapping)
  expect(ecsPolicyPayload(blankEcsPolicy(), json).prometheus).toEqual(mapping)
  expect(JSON.stringify(mapping)).toBe(json)
})
it.each([
  { queries: { cpu_util: 'custom' } },
  { ...mapping, units: { cpu_util: 'bytes' } },
  { ...mapping, timestamp_queries: { cpu_util: 'bad\nquery' } },
  { ...mapping, label_filters: { 'not-label': 'x' } },
])('无证据单位或非法配置不能提交 %j', raw => {
  expect(() => parseEcsPrometheus(JSON.stringify(raw))).toThrow()
})

it('实际策略表单能改单位和源时间，保存时保留标签过滤', async () => {
  const response = ecsPolicy()
  response.policy.prometheus = mapping
  const saver = vi.fn(async (_id, body) => ({ ...response, policy: body }))
  const view = mountPersonnel('ecs/EcsPolicyDialog.vue', { props: { modelValue: true, tenantId: 7 },
    dependencies: { ...dependencies, '@/api/ecs': { getEcsPolicy: async () => response, saveEcsPolicy: saver } } })
  dispose.push(view.unmount)
  await settlePersonnel()
  const unit = findNode(view.root, 'Select', node => node.props['aria-label'] === 'CPU 使用率单位')
  ;(unit.props['onUpdate:modelValue'] as (value: string) => void)('%')
  await settlePersonnel()
  const timestamp = findNode(view.root, 'Input', node => node.props['aria-label'] === 'CPU 使用率源时间查询')
  ;(timestamp.props['onUpdate:modelValue'] as (value: string) => void)('another_source_seconds')
  await settlePersonnel()
  await clickButton(view.root, '保存策略')
  expect(saver).toHaveBeenCalledOnce()
  expect(saver.mock.calls[0][1].prometheus).toMatchObject({ units: { cpu_util: '%' },
    timestamp_queries: { cpu_util: 'another_source_seconds' }, label_filters: mapping.label_filters })
})

it('趋势保留真实求值零并显示时间语义，未知旧历史不标作物理采样', async () => {
  const view = mountPersonnel('ecs/EcsTrendDrawer.vue', { props: { modelValue: true, instanceId: 11, tenantId: 7 },
    dependencies: { ...dependencies, '@/api/ecs': { getEcsHistory: async (_id: number, query: Parameters<typeof ecsHistory>[0]) => {
      const history = ecsHistory(query)
      Object.assign(history.instance.latest.cpu_util!, { value: 0, timestamp_semantics: 'evaluation', unit: '%' })
      Object.assign(history.series[0], { timestamp_semantics: 'evaluation' })
      return history
    } } } })
  dispose.push(view.unmount)
  await settlePersonnel()
  expect(textOf(view.root)).toContain('0.0%')
  expect(textOf(view.root)).toContain('求值时间')
  expect(textOf(view.root)).toContain('时间语义未知')
  const columns = allNodes(view.root).filter(node => node.tag === 'TableColumn').map(node => node.props.label)
  expect(columns).toContain('时间语义')
  expect(columns).not.toContain('采样时间（本地）')
})
