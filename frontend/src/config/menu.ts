import { Bell, Box, Coin, Document, Odometer, Setting } from '@element-plus/icons-vue'
import { type Component, markRaw } from 'vue'
import { SYSTEM_PERMS } from '@/router/perms'
import { useUserStore } from '@/stores/user'
import { JOB_MENU_PATH, JOB_PERMISSION } from './job-navigation'
import { permittedItems, workspaceById, workspacePermissions, type WorkspaceId } from './workspace-navigation'

export interface MenuItem { path: string; title: string; icon: Component; perm: string | string[]; badge?: 'firing'; workspace?: WorkspaceId; activePath?: string }
export interface MenuGroup { title: string; items: MenuItem[] }


/** 侧边栏菜单：按角色权限点过滤（与路由 meta.perm 一致） */
export const MENU: MenuGroup[] = [
  {
    title: '运行监控',
    items: [
      { path: '/dashboard', title: '运行总览', icon: markRaw(Odometer), perm: 'dashboard:view' },
      { path: '/monitoring', title: '资源监控', icon: markRaw(Odometer), perm: workspacePermissions('monitoring'), workspace: 'monitoring' },
      { path: '/pod-events', title: '容器事件', icon: markRaw(Box), perm: 'podevent:view' },
      { path: JOB_MENU_PATH, title: '作业运行', icon: markRaw(Document), perm: JOB_PERMISSION, workspace: 'jobs' },
    ],
  },
  {
    title: '资源与报表',
    items: [
      { path: '/ledger', title: '资源台账', icon: markRaw(Coin), perm: 'ledger:view' },
      { path: '/reports', title: '报表中心', icon: markRaw(Document), perm: 'report:view' },
    ],
  },
  {
    title: '告警管理',
    items: [
      { path: '/alarms', title: '告警管理', icon: markRaw(Bell), perm: workspacePermissions('alarms'), workspace: 'alarms', badge: 'firing' },
    ],
  },
  {
    title: '系统',
    items: [
      { path: '/system', title: '系统管理', icon: markRaw(Setting), perm: SYSTEM_PERMS },
      { path: '/guides', title: '资源与指标指南', icon: markRaw(Document), perm: ['dashboard:view', 'ledger:view', 'rule:view', 'report:view'] },
    ],
  },
]

const allowed = (m: MenuItem) => {
  const u = useUserStore()
  return u.canAny(...(Array.isArray(m.perm) ? m.perm : [m.perm]))
}
/** 当前用户可见的菜单（空分组不显示） */
export const visibleMenu = (): MenuGroup[] => {
  const user = useUserStore()
  return MENU.map(group => ({ ...group, items: group.items.filter(allowed).flatMap(item => {
    if (!item.workspace) return [item]
    const first = permittedItems(workspaceById(item.workspace), user.canAny)[0]
    return first ? [{ ...item, path: first.path, activePath: item.path }] : []
  }) })).filter(group => group.items.length)
}
export const firstAllowed = () => visibleMenu()[0]?.items[0]?.path

/** 面包屑：页面 → 所属分组 */
export const GROUP_OF: Record<string, string> = Object.fromEntries(MENU.flatMap((g) => g.items.map((m) => [m.path, g.title])))
