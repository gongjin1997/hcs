import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { nodeStatusType, sortNodes, topologyHealth, topologyText } from '../src/views/dcs/dcs-utils'
import type { DcsNode, DcsTopology } from '../src/types/dcs'

const topology = (changes: Partial<DcsTopology> = {}): DcsTopology => ({
  state: 'AVAILABLE', observed_at: '2026-11-06T08:00:00Z', freshness: 'FRESH', node_count: 6, shards: 3, masters: 3,
  replicas: 3, proxies: 0, inactive: 0, inactive_masters: 0, transitional: 0, unknown: 0, warning: null, ...changes,
})
const node = (name: string, changes: Partial<DcsNode> = {}): DcsNode => ({
  name, node_name: null, role: 'slave', kind: 'redis-server', status: 'Active', shard: 'group-0', shard_id: null,
  ip: null, port: null, az: null, ...changes,
})

describe('DCS 分片与节点（逻辑节点接口 §4.3.3）', () => {
  it('拓扑摘要按分片、主从和 Proxy 汇总，没有节点数据时说明原因', () => {
    expect(topologyText(topology())).toBe('3 分片 · 3 主 3 从')
    expect(topologyText(topology({ shards: 2, masters: 2, replicas: 2, proxies: 2 }))).toBe('2 分片 · 2 主 2 从 · 2 Proxy')
    expect(topologyText(topology({ shards: null, masters: 1, replicas: 0, node_count: 1 }))).toBe('1 主')
    expect(topologyText(topology({ state: 'NOT_SUPPORTED' }))).toBe('不支持节点查询')
    expect(topologyText(topology({ state: 'NOT_RETURNED' }))).toBe('源端未返回节点')
    expect(topologyText(null)).toBe('—')
  })

  it('主节点故障最严重；状态未识别不显示为正常；过期结果只作参考', () => {
    expect(topologyHealth(topology())).toEqual({ type: 'success', text: '全部运行中' })
    expect(topologyHealth(topology({ inactive: 2, inactive_masters: 1 }))).toEqual({ type: 'danger', text: '1 个主节点故障' })
    expect(topologyHealth(topology({ inactive: 1 }))).toEqual({ type: 'warning', text: '1 个节点故障' })
    expect(topologyHealth(topology({ unknown: 1 }))).toEqual({ type: 'warning', text: '1 个状态未识别' })
    expect(topologyHealth(topology({ state: 'STALE', freshness: 'STALE', inactive: 1 })))
      .toEqual({ type: 'info', text: '1 个节点故障（已过期）' })
    expect(topologyHealth(topology({ state: 'UNAVAILABLE' }))).toEqual({ type: 'info', text: '获取失败' })
    expect(topologyHealth(undefined)).toEqual({ type: 'info', text: '无节点数据' })
  })

  it('节点按分片排列，同分片主节点在前，Proxy 在最后', () => {
    const sorted = sortNodes([
      node('p1', { role: 'proxy', kind: 'redis-proxy', shard: null }), node('s10', { shard: 'group-10' }),
      node('m2', { role: 'master', shard: 'group-2' }), node('s2', { shard: 'group-2' }), node('m10', { role: 'master', shard: 'group-10' }),
    ])
    expect(sorted.map(item => item.name)).toEqual(['m2', 's2', 'm10', 's10', 'p1'])
    expect(nodeStatusType('Inactive')).toBe('danger')
    expect(nodeStatusType('AddSharding')).toBe('warning')
    expect(nodeStatusType('Unexpected')).toBe('info')
  })

  it('当前统计表显示分片 / 节点列，选择实例后显示节点面板', () => {
    const current = readFileSync(new URL('../src/views/dcs/DcsCurrentStatistics.vue', import.meta.url), 'utf8')
    expect(current).toContain('分片 / 节点')
    expect(current).toContain('topologyHealth(row.topology)')
    const view = readFileSync(new URL('../src/views/dcs/DcsMemoryView.vue', import.meta.url), 'utf8')
    expect(view).toContain('<DcsNodesPanel :instance="selectedTarget ?? null" />')
    const panel = readFileSync(new URL('../src/views/dcs/DcsNodesPanel.vue', import.meta.url), 'utf8')
    expect(panel).toContain('getDcsInstance(target.id')
    expect(panel).toContain('节点告警只在实例运行中时判定')
  })
})
