import { Bell, Box, Coin, Document, Message, MuteNotification, Odometer, Setting, SetUp } from '@element-plus/icons-vue'
import { type Component, markRaw } from 'vue'
import { useUserStore } from '@/stores/user'

export interface MenuItem { path: string; title: string; icon: Component; perm: string | string[]; badge?: 'firing' }
export interface MenuGroup { title: string; items: MenuItem[] }

const SYSTEM_PERMS = ['tenant:view', 'task:view', 'user:manage', 'role:manage', 'audit:view']

/** 侧边栏菜单：按角色权限点过滤（与路由 meta.perm 一致） */
export const MENU: MenuGroup[] = [
  {
    title: '监控',
    items: [
      { path: '/dashboard', title: '运行总览', icon: markRaw(Odometer), perm: 'dashboard:view' },
      { path: '/alarms', title: '告警中心', icon: markRaw(Bell), perm: 'alarm:view', badge: 'firing' },
      { path: '/pod-events', title: '容器事件', icon: markRaw(Box), perm: 'podevent:view' },
      { path: '/ledger', title: '资源台账', icon: markRaw(Coin), perm: 'ledger:view' },
    ],
  },
  {
    title: '配置',
    items: [
      { path: '/rules', title: '规则管理', icon: markRaw(SetUp), perm: 'rule:view' },
      { path: '/notify', title: '通知管理', icon: markRaw(Message), perm: 'notify:view' },
      { path: '/silences', title: '维护与静默', icon: markRaw(MuteNotification), perm: 'alarm:view' },
    ],
  },
  {
    title: '报表与系统',
    items: [
      { path: '/reports', title: '报表中心', icon: markRaw(Document), perm: 'report:view' },
      { path: '/system', title: '系统管理', icon: markRaw(Setting), perm: SYSTEM_PERMS },
    ],
  },
]

const allowed = (m: MenuItem) => {
  const u = useUserStore()
  return u.canAny(...(Array.isArray(m.perm) ? m.perm : [m.perm]))
}
/** 当前用户可见的菜单（空分组不显示） */
export const visibleMenu = (): MenuGroup[] =>
  MENU.map((g) => ({ ...g, items: g.items.filter(allowed) })).filter((g) => g.items.length)
export const firstAllowed = () => visibleMenu()[0]?.items[0]?.path

/** 面包屑：页面 → 所属分组 */
export const GROUP_OF: Record<string, string> = Object.fromEntries(MENU.flatMap((g) => g.items.map((m) => [m.path, g.title])))
