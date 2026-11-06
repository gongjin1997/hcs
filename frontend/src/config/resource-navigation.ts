import type { LocationQuery, RouteLocationRaw } from 'vue-router'
import { monitoringEntryFor } from './workspace-navigation'

export interface ResourceTarget { id: number | null; tenant_id: number | null; resource_type: string }
export interface ResourceViewer { user: { id?: number; tenant_ids: number[] | null } | null; can: (permission: string) => boolean }
export interface MonitoringContext { instanceId: number; tenantId: number }

const positiveId = (value: number | null) => value !== null && Number.isSafeInteger(value) && value > 0
export const positiveIdParam = (value: unknown): number | null => typeof value === 'string' && /^[1-9]\d*$/.test(value)
  && Number.isSafeInteger(Number(value)) ? Number(value) : null

function allowed(resource: ResourceTarget | null | undefined, viewer: ResourceViewer, permission: string | string[]): resource is ResourceTarget & { id: number; tenant_id: number } {
  return !!resource && positiveId(resource.id) && positiveId(resource.tenant_id) && !!viewer.user
    && (Array.isArray(permission) ? permission.some(item => viewer.can(item)) : viewer.can(permission))
    && (viewer.user.tenant_ids === null || viewer.user.tenant_ids.includes(resource.tenant_id!))
}

/** 使用台账本地主键和资源所属租户，权限与各服务现有路由一致。 */
export function resourceMonitoringLocation(resource: ResourceTarget | null | undefined, viewer: ResourceViewer): RouteLocationRaw | null {
  const target = resource && monitoringEntryFor(resource.resource_type)
  if (!target || !allowed(resource, viewer, target.perm)) return null
  return { path: target.path, query: { instance_id: String(resource.id), tenant_id: String(resource.tenant_id) } }
}

export function resourceLedgerLocation(resource: ResourceTarget | null | undefined, viewer: ResourceViewer): RouteLocationRaw | null {
  if (!allowed(resource, viewer, 'ledger:view')) return null
  return { path: '/ledger', query: { type: resource.resource_type, id: String(resource.id), tenant_id: String(resource.tenant_id) } }
}

export function resourceAlarmsLocation(resource: ResourceTarget | null | undefined, viewer: ResourceViewer): RouteLocationRaw | null {
  if (!allowed(resource, viewer, 'alarm:view')) return null
  return { path: '/alarms', query: { resource_ref: String(resource.id), tenant_id: String(resource.tenant_id) } }
}

/** URL 参数只接受单个十进制正整数；缺失租户时不猜测当前全局筛选。 */
export function readMonitoringContext(query: LocationQuery, tenantId: number | null,
  viewer: ResourceViewer, permission: string): { context: MonitoringContext | null; error: string } {
  if (query.instance_id == null && query.tenant_id == null) return { context: null, error: '' }
  const instanceId = positiveIdParam(query.instance_id), linkedTenant = positiveIdParam(query.tenant_id)
  if (instanceId === null || linkedTenant === null) return { context: null, error: '实例链接无效，请重新选择实例' }
  if (!viewer.user || !viewer.can(permission) || (viewer.user.tenant_ids !== null && !viewer.user.tenant_ids.includes(linkedTenant))) {
    return { context: null, error: '当前账号无权查看此实例' }
  }
  if (tenantId !== null && tenantId !== linkedTenant) return { context: null, error: '实例不属于当前租户，请重新选择实例' }
  return { context: { instanceId, tenantId: linkedTenant }, error: '' }
}

export function monitoringListQuery(query: LocationQuery): LocationQuery {
  const { instance_id: _id, tenant_id: _tenant, tab: _tab, ...filters } = query
  return filters
}

/** 租户切换时清除旧租户资源主键；保留不会扩大数据范围的业务筛选条件。 */
export function tenantChangeQuery(path: string, query: LocationQuery): LocationQuery {
  if (['/monitoring', '/ecs', '/dcs', '/gaussdb'].includes(path)) return monitoringListQuery(query)
  if (path === '/ledger') {
    const { id: _id, tenant_id: _tenant, ...filters } = query
    return filters
  }
  if (path === '/alarms') {
    const { id: _id, resource_ref: _resource, tenant_id: _tenant, ...filters } = query
    return filters
  }
  return query
}

/** These two list views clear scope data and close their detail drawer without remounting. */
export const tenantViewKey = (path: string, tenantSeq: number): string =>
  ['/ledger', '/alarms'].includes(path) ? path : `${path}#${tenantSeq}`

/** 用户身份、授权范围或监控相关权限变化时，清理已加载的资源上下文。 */
export function resourceViewerScopeKey(viewer: ResourceViewer): string {
  return JSON.stringify([viewer.user?.id, viewer.user?.tenant_ids,
    ...['ledger:view', 'report:view', 'alarm:view', 'task:run', 'ledger:export'].map(permission => viewer.can(permission))])
}
