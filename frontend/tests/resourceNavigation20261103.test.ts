import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import dayjs from 'dayjs'
import { resourceMonitoringLocation } from '../src/config/resource-navigation'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import type { AlarmDetail, Resource, ResourceDetail } from '../src/types'
import * as columns from '../src/views/ledger/columns'
import { allNodes, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
const resource = (overrides: Partial<Resource> = {}): Resource => ({
  id: 58, tenant_id: 7, service: 'gaussdb', resource_type: 'gaussdb_instance', resource_id: 'cloud-instance-not-local-id', name: 'QA Gauss',
  status_raw: 'ACTIVE', health: 'OK', region: null, az: null, ip: null, spec: null, vcpus: null, memory_gb: null,
  storage_gb: null, created_time: null, labels: {}, attrs: {}, metrics: {}, expected_state: {}, owner: null, owner_user_id: null,
  business: null, env_type: null, remark: null, monitored: true, critical: false, lifecycle: 'ACTIVE',
  first_seen: '', last_seen: '', deleted_at: null, status_since: null, active_alarms: 0, ...overrides,
})
const alarm = (overrides: Partial<AlarmDetail> = {}): AlarmDetail => ({
  id: 9, rule_id: 'R-GAUSS-QA', service: 'gaussdb', resource_type: 'gaussdb_instance', resource_name: 'QA Gauss',
  resource_ref: 58, tenant_id: 7, sub_object: '', severity: 'P2', status: 'FIRING', suppress_reason: null,
  title: 'QA storage', check_item: 'storage', current_value: null, threshold_text: null, owner: null,
  first_seen: '2026-11-03T00:00:00Z', last_seen: '2026-11-03T00:00:00Z', resolved_at: null,
  count: 1, ack_by: null, ack_at: null, fingerprint: 'qa-navigation', detail: { resource_id: 'cloud-instance-not-local-id' },
  runbook: null, logs: [], notifications: [], ...overrides,
})

async function drawer(kind: 'resource' | 'alarm', options: { tenantIds?: number[] | null; permissions?: string[]; tenantId?: number | null; id?: number | null; resourceType?: string } = {}) {
  const state = reactive({ user: { tenant_ids: options.tenantIds === undefined ? [7] : options.tenantIds }, permissions: options.permissions ?? ['ledger:view'] })
  const can = (permission: string) => state.permissions.includes(permission)
  const value = resource({ tenant_id: options.tenantId === undefined ? 7 : options.tenantId, resource_type: options.resourceType ?? 'gaussdb_instance' })
  const details: ResourceDetail = { resource: value, tenant: null, alarms: [], changes: [], status_changes: [] }
  const view = mountPersonnel(kind === 'resource' ? 'ledger/ResourceDrawer.vue' : 'alarms/AlarmDrawer.vue', {
    api: { getResource: vi.fn(async () => details), getAlarm: vi.fn(async () => alarm({
      tenant_id: value.tenant_id, resource_ref: options.id === undefined ? 58 : options.id, resource_type: value.resource_type,
    })) },
    props: kind === 'resource' ? { modelValue: true, resourceId: 58, inspectedTypes: [], options: { business: [] } } : { modelValue: 9 },
    dependencies: {
      './columns': columns, dayjs: { default: dayjs }, '@/composables/useLatestRequest': { useLatestRequest },
      '@/utils/clipboard': { copyText: vi.fn() }, '@/stores/user': { useUserStore: () => ({ get user() { return state.user }, can }) },
    },
  })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, state }
}

describe('资源及告警详情中的 GaussDB 入口', () => {
  it.each([
    ['ecs_server', '/ecs', 'ledger:view'], ['dcs_instance', '/dcs', 'report:view'], ['gaussdb_instance', '/gaussdb', 'ledger:view'],
  ])('%s 在台账和告警中共用同一入口，保持 %s 的原权限 %s', async (resourceType, path, permission) => {
    for (const kind of ['resource', 'alarm'] as const) {
      const view = await drawer(kind, { resourceType, permissions: [permission] })
      expect(findNode(view.root, 'router-link', item => textOf(item) === '查看监控').props.to).toEqual({
        path, query: { instance_id: '58', tenant_id: '7' },
      })
      view.state.permissions = permission === 'report:view' ? ['ledger:view'] : ['report:view']
      await settlePersonnel()
      expect(allNodes(view.root).some(item => item.tag === 'router-link' && textOf(item) === '查看监控')).toBe(false)
    }
  })
  it.each(['resource', 'alarm'] as const)('%s 使用本地资源主键与资源自己的租户，实际路由解析正确且撤权立即隐藏', async kind => {
    const view = await drawer(kind)
    const link = findNode(view.root, 'router-link', item => textOf(item) === '查看监控')
    if (kind === 'alarm') expect(textOf(view.root)).toContain('当前查询，非告警时刻快照')
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/gaussdb', component: {} }] })
    await router.push(link.props.to as Parameters<typeof router.push>[0])
    expect(router.currentRoute.value.fullPath).toBe('/gaussdb?instance_id=58&tenant_id=7')
    view.state.permissions = ['alarm:view']
    await settlePersonnel()
    expect(allNodes(view.root).some(item => item.tag === 'router-link' && textOf(item) === '查看监控')).toBe(false)
  })

  it.each([
    { tenantIds: [8] }, { tenantIds: [] }, { tenantId: null }, { permissions: ['alarm:view'] }, { resourceType: 'evs_volume' },
  ])('两个 Drawer 均不显示越权、无租户或其他类型入口：%j', async options => {
    for (const kind of ['resource', 'alarm'] as const) {
      const view = await drawer(kind, options)
      expect(allNodes(view.root).some(item => item.tag === 'router-link' && textOf(item) === '查看监控')).toBe(false)
    }
  })

  it('告警没有本地资源关联时不从云端 resource_id 猜测，原 ECS 入口保留', async () => {
    const missing = await drawer('alarm', { id: null })
    expect(textOf(missing.root)).not.toContain('查看监控')
    const ecs = await drawer('resource', { resourceType: 'ecs_server' })
    expect(findNode(ecs.root, 'router-link', item => textOf(item) === '查看监控').props.to).toEqual({
      path: '/ecs', query: { instance_id: '58', tenant_id: '7' },
    })
  })

  it('全局范围用户可进入明确租户，空值、无登录、无效本地主键不产生链接', () => {
    const viewer = { user: { tenant_ids: null }, can: () => true }
    expect(resourceMonitoringLocation(resource(), viewer)).toEqual({ path: '/gaussdb', query: { instance_id: '58', tenant_id: '7' } })
    for (const id of [null, 0, -1, NaN, 1.5]) {
      expect(resourceMonitoringLocation({ ...resource(), id }, viewer)).toBeNull()
    }
    expect(resourceMonitoringLocation(undefined, viewer)).toBeNull()
    expect(resourceMonitoringLocation(resource(), { ...viewer, user: null })).toBeNull()
  })
})
