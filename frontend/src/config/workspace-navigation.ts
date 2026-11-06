import { JOB_MENU_PATH, JOB_NAVIGATION, JOB_PERMISSION } from './job-navigation'
import type { RouteLocationRaw } from 'vue-router'

export type WorkspaceId = 'monitoring' | 'alarms' | 'jobs'
export interface WorkspaceItem { path: string; label: string; title: string; perm: string | string[]; resourceType?: string }
export interface Workspace { id: WorkspaceId; title: string; menuPath: string; items: WorkspaceItem[] }
export type PermissionCheck = (...permissions: string[]) => boolean

/** 菜单、页内导航和路由元数据共用一个入口定义；权限仍由各业务 API 校验。 */
export const WORKSPACES: Workspace[] = [
  {
    id: 'monitoring', title: '资源监控', menuPath: '/monitoring', items: [
      { path: '/monitoring', label: '监控覆盖', title: '监控覆盖', perm: ['ledger:view', 'report:view'] },
      { path: '/ecs', label: 'ECS', title: 'ECS 监控', perm: 'ledger:view', resourceType: 'ecs_server' },
      { path: '/dcs', label: 'DCS', title: 'DCS 监控', perm: 'report:view', resourceType: 'dcs_instance' },
      { path: '/gaussdb', label: 'GaussDB', title: 'GaussDB 监控', perm: 'ledger:view', resourceType: 'gaussdb_instance' },
    ],
  },
  {
    id: 'alarms', title: '告警管理', menuPath: '/alarms', items: [
      { path: '/alarms', label: '告警', title: '告警中心', perm: 'alarm:view' },
      { path: '/rules', label: '规则', title: '规则管理', perm: 'rule:view' },
      { path: '/notify', label: '通知', title: '通知管理', perm: 'notify:view' },
      { path: '/silences', label: '维护与静默', title: '维护与静默', perm: 'alarm:view' },
    ],
  },
  {
    id: 'jobs', title: '作业运行', menuPath: JOB_MENU_PATH,
    items: JOB_NAVIGATION.map(item => ({ path: item.path, label: item.label, title: item.meta.title, perm: JOB_PERMISSION })),
  },
]

export const workspaceById = (id: WorkspaceId): Workspace => WORKSPACES.find(item => item.id === id)!
export const monitoringEntryFor = (resourceType: string) => workspaceById('monitoring').items.find(item => item.resourceType === resourceType)
export const workspaceFor = (path: string) => WORKSPACES.find(workspace => workspace.items.some(item => item.path === path))
export const permittedItems = (workspace: Workspace, canAny: PermissionCheck) =>
  workspace.items.filter(item => canAny(...(Array.isArray(item.perm) ? item.perm : [item.perm])))
export const workspacePermissions = (id: WorkspaceId) =>
  [...new Set(workspaceById(id).items.flatMap(item => Array.isArray(item.perm) ? item.perm : [item.perm]))]

export function workspaceMeta(path: string) {
  const workspace = workspaceFor(path)
  const item = workspace?.items.find(candidate => candidate.path === path)
  if (!workspace || !item) throw new Error(`Unknown workspace route: ${path}`)
  return { title: item.title, perm: item.perm, activeMenu: workspace.menuPath }
}

/** 健康分组进入实际业务页；作业不属于资源台账，不能套用资源类型筛选。 */
export function healthGroupLocation(code: string, canAny: PermissionCheck): RouteLocationRaw | null {
  const destinations: Record<string, string> = { ecs: '/ecs', dcs: '/dcs', gaussdb: '/gaussdb',
    dgc_realtime: '/job-reports/realtime', dgc_batch: '/job-reports/batch', mrs: '/job-reports/mrs' }
  if (code === 'cce') return canAny('podevent:view') ? { path: '/pod-events' } : null
  const path = destinations[code]
  if (!path) return null
  const { perm } = workspaceMeta(path)
  return canAny(...(Array.isArray(perm) ? perm : [perm])) ? { path } : null
}
