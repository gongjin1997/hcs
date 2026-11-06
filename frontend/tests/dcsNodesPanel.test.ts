import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DcsInstance, DcsInstanceFetcher, DcsNode } from '../src/types/dcs'
import * as dcsUtils from '../src/views/dcs/dcs-utils'
import { deferred } from './businessViewHarness'
import { mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const disposers: (() => void)[] = []
afterEach(() => disposers.splice(0).forEach(dispose => dispose()))
const node = (name: string): DcsNode => ({ name, node_name: name, role: 'master', kind: 'redis-server', status: 'Active',
  shard: 'group-0', shard_id: null, ip: null, port: null, az: null })
const instance = (id = 11, tenantId = 7): DcsInstance => ({ id, tenant_id: tenantId, tenant_name: `租户 ${tenantId}`,
  resource_id: `redis-${id}`, name: `缓存 ${id}`, project_ref: 107, project_name: '项目甲', region: null,
  status_raw: 'RUNNING', memory_usage: null, used_memory_mb: null, max_memory_mb: null, snapshot_at: null })
function mount(target: DcsInstance, getDcsInstance: DcsInstanceFetcher) {
  const view = mountPersonnel('dcs/DcsNodesPanel.vue', { props: { instance: target }, dependencies: {
    '@/api/dcs': { getDcsInstance }, './dcs-utils': dcsUtils,
  } })
  disposers.push(view.unmount)
  return view
}

describe('DCS 节点面板刷新和请求隔离', () => {
  it('刷新同一实例的本地记录后，重新读取节点而非保留旧拓扑', async () => {
    const target = instance()
    const getDcsInstance = vi.fn<DcsInstanceFetcher>()
      .mockResolvedValueOnce({ ...target, nodes: [node('old-master')] })
      .mockResolvedValueOnce({ ...target, nodes: [node('new-master')] })
    const view = mount(target, getDcsInstance)
    await settlePersonnel()
    expect(textOf(view.root)).toContain('old-master')
    view.updateProps({ instance: { ...target, snapshot_at: '2026-11-07T01:00:00Z' } })
    await settlePersonnel()
    expect(getDcsInstance).toHaveBeenCalledTimes(2)
    expect(textOf(view.root)).toContain('new-master')
    expect(textOf(view.root)).not.toContain('old-master')
  })

  it('切换到带节点的详情时，迟到请求不能覆盖当前实例', async () => {
    const old = deferred<DcsInstance>(), target = instance()
    const getDcsInstance = vi.fn<DcsInstanceFetcher>(() => old.promise)
    const view = mount(target, getDcsInstance)
    view.updateProps({ instance: { ...instance(12, 8), nodes: [node('new-tenant-master')] } })
    await settlePersonnel()
    expect(getDcsInstance.mock.calls[0][1].signal.aborted).toBe(true)
    old.resolve({ ...target, nodes: [node('late-old-tenant-master')] })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('new-tenant-master')
    expect(textOf(view.root)).not.toContain('late-old-tenant-master')
  })

  it('拒绝与目标租户不匹配的节点详情', async () => {
    const getDcsInstance = vi.fn<DcsInstanceFetcher>().mockResolvedValue({ ...instance(11, 8), nodes: [node('foreign-master')] })
    const view = mount(instance(), getDcsInstance)
    await settlePersonnel()
    expect(textOf(view.root)).not.toContain('foreign-master')
  })
})
