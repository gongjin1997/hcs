import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { TOKEN_KEY } from '@/api/request'
import BasicLayout from '@/layouts/BasicLayout.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'

declare module 'vue-router' {
  /** perm：访问页面需要的权限点（数组 = 满足任一）；public：无需登录 */
  interface RouteMeta { title?: string; perm?: string | string[]; public?: boolean }
}

/** 系统管理页：任一子模块有权限即可进入（页内 Tab 再按权限显示） */
export const SYSTEM_PERMS = ['tenant:view', 'task:view', 'user:manage', 'role:manage', 'audit:view']

const routes: RouteRecordRaw[] = [
  { path: '/login', component: () => import('@/views/login/LoginView.vue'), meta: { title: '登录', public: true } },
  { path: '/m/action/:token', component: () => import('@/views/mobile/MailActionView.vue'), meta: { title: '告警处理', public: true } },
  { path: '/bigscreen', component: () => import('@/views/bigscreen/BigScreenView.vue'), meta: { title: '态势大屏', perm: 'dashboard:view' } },
  {
    path: '/',
    component: BasicLayout,
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', component: () => import('@/views/dashboard/DashboardView.vue'), meta: { title: '运行总览', perm: 'dashboard:view' } },
      { path: 'alarms', component: () => import('@/views/alarms/AlarmListView.vue'), meta: { title: '告警中心', perm: 'alarm:view' } },
      { path: 'pod-events', component: () => import('@/views/pods/PodEventsView.vue'), meta: { title: '容器事件', perm: 'podevent:view' } },
      { path: 'ledger', component: () => import('@/views/ledger/LedgerView.vue'), meta: { title: '资源台账', perm: 'ledger:view' } },
      { path: 'resources', redirect: (to) => ({ path: '/ledger', query: to.query }) },   // 兼容旧链接（邮件 / 收藏夹）
      { path: 'rules', component: () => import('@/views/rules/RuleListView.vue'), meta: { title: '规则管理', perm: 'rule:view' } },
      { path: 'notify', component: () => import('@/views/notify/NotifyView.vue'), meta: { title: '通知管理', perm: 'notify:view' } },
      { path: 'silences', component: () => import('@/views/misc/PlaceholderView.vue'), meta: { title: '维护与静默', perm: 'alarm:view' } },
      { path: 'reports', component: () => import('@/views/reports/ReportView.vue'), meta: { title: '报表中心', perm: 'report:view' } },
      { path: 'system', component: () => import('@/views/system/SystemView.vue'), meta: { title: '系统管理', perm: SYSTEM_PERMS } },
      { path: '403', component: () => import('@/views/misc/ErrorView.vue'), props: { code: 403 }, meta: { title: '无权限' } },
    ],
  },
  { path: '/:pathMatch(.*)*', component: () => import('@/views/misc/ErrorView.vue'), props: { code: 404 }, meta: { title: '页面不存在', public: true } },
]

const router = createRouter({ history: createWebHistory(), routes, scrollBehavior: () => ({ top: 0 }) })

// 路由守卫：登录校验 → 拉取当前用户（角色、权限点、租户范围）→ 页面权限校验
router.beforeEach(async (to) => {
  document.title = `${to.meta.title ?? ''} - HCS 巡检平台`
  if (to.meta.public) return true
  if (!localStorage.getItem(TOKEN_KEY)) return { path: '/login', query: { redirect: to.fullPath } }
  const user = useUserStore()
  if (!user.user) {
    try {
      await user.fetchMe()
      useAppStore().ensureTenant()
    } catch {
      return { path: '/login', query: { redirect: to.fullPath } }
    }
  }
  const perm = to.meta.perm
  if (perm && !user.canAny(...(Array.isArray(perm) ? perm : [perm]))) {
    // 默认首页无权限时，跳到第一个有权限的菜单，避免登录后直接 403
    if (to.path === '/dashboard') {
      const { firstAllowed } = await import('@/config/menu')
      const path = firstAllowed()
      if (path && path !== '/dashboard') return path
    }
    return '/403'
  }
  return true
})

export default router
