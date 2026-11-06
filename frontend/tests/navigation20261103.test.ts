import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import * as VueRouter from 'vue-router'
import { JOB_MENU_PATH, JOB_NAVIGATION, JOB_PERMISSION } from '../src/config/job-navigation'
import { SYSTEM_PERMS } from '../src/router/perms'
import * as workspaceNavigation from '../src/config/workspace-navigation'
import { PANEL_CONFIG } from '../src/views/job-reports/report-config'

const jobNavigation = { JOB_MENU_PATH, JOB_NAVIGATION, JOB_PERMISSION }
const histories: VueRouter.RouterHistory[] = []
beforeEach(() => {
  vi.stubGlobal('document', { title: '' })
  vi.stubGlobal('localStorage', { getItem: () => 'navigation-test-token' })
})
afterEach(() => { histories.splice(0).forEach(history => history.destroy()); vi.unstubAllGlobals() })

function load(file: string, dependencies: Record<string, unknown>) {
  const source = readFileSync(resolve(import.meta.dirname, '../src', file), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name in dependencies) return dependencies[name]
    if (name.endsWith('.vue')) return { default: { render: () => null } }
    throw new Error(`Unmocked navigation dependency: ${name}`)
  }, exports)
  return exports
}

function navigation(initialPermissions = ['report:view']) {
  const permissions = Vue.reactive(new Set(initialPermissions))
  const user = { user: { tenant_ids: null }, canAny: (...values: string[]) => values.some(value => permissions.has(value)) }
  const stores = { '@/stores/user': { useUserStore: () => user } }
  const menu = load('config/menu.ts', {
    ...stores, vue: Vue, '@/router/perms': { SYSTEM_PERMS }, './job-navigation': jobNavigation, './workspace-navigation': workspaceNavigation,
    '@element-plus/icons-vue': new Proxy({}, { get: () => ({ render: () => null }) }),
  }) as {
    MENU: { title: string; items: { path: string; perm: string | string[] }[] }[]
    GROUP_OF: Record<string, string>; visibleMenu: () => { title: string; items: { path: string }[] }[]
    firstAllowed: () => string | undefined
  }
  const history = VueRouter.createMemoryHistory()
  histories.push(history)
  const router = load('router/index.ts', {
    ...stores, 'vue-router': { ...VueRouter, createWebHistory: () => history },
    '@/api/request': { TOKEN_KEY: 'test-token' }, '@/stores/app': { useAppStore: () => ({ ensureTenant: vi.fn() }) },
    './perms': { SYSTEM_PERMS }, '@/config/job-navigation': jobNavigation, '@/config/menu': menu, '@/config/workspace-navigation': workspaceNavigation,
  }).default as VueRouter.Router
  const travel = (delta: number) => new Promise<void>(resolve => {
    const stop = router.afterEach(() => { stop(); resolve() })
    router.go(delta)
  })
  return { menu, router, permissions, travel }
}

describe('统一导航与真实路由守卫', () => {
  it('四组归属明确且作业只占一个一级入口，保留原有权限点', () => {
    const { menu } = navigation()
    expect(menu.MENU.map(group => [group.title, group.items.map(item => item.path)])).toEqual([
      ['运行监控', ['/dashboard', '/monitoring', '/pod-events', JOB_MENU_PATH]],
      ['资源与报表', ['/ledger', '/reports']],
      ['告警管理', ['/alarms']],
      ['系统', ['/system', '/guides']],
    ])
    expect(menu.MENU.flatMap(group => group.items).filter(item => item.path.startsWith('/job-reports'))).toEqual([
      expect.objectContaining({ path: JOB_MENU_PATH, perm: 'report:view' }),
    ])
    expect(menu.visibleMenu().flatMap(group => group.items).map(item => item.path)).toEqual(['/monitoring', JOB_MENU_PATH, '/reports', '/guides'])
  })

  it('逐一实际进入五个旧 URL，路由 props、权限、面包屑和选中项使用同一配置', async () => {
    const { router, menu } = navigation()
    for (const item of JOB_NAVIGATION) {
      await router.push(item.path)
      const route = router.currentRoute.value
      expect(route.path).toBe(item.path)
      expect(route.meta.perm).toBe('report:view')
      expect(route.meta.title).toBe(PANEL_CONFIG[item.panel].title)
      expect(route.meta.activeMenu).toBe(JOB_MENU_PATH)
      expect(menu.GROUP_OF[route.meta.activeMenu!]).toBe('运行监控')
      expect(route.matched[route.matched.length - 1]?.props.default).toEqual({ panel: item.panel })
    }
    await router.push('/job-reports?keyword=qa#source')
    expect(router.currentRoute.value.fullPath).toBe('/job-reports/dgc?keyword=qa#source')
  })

  it('作业 URL 前进后退更新标题和共同选中项，未授权用户实际跳转 403', async () => {
    const { router, permissions, travel } = navigation()
    await router.push('/job-reports/dgc')
    await router.push('/job-reports/mrs')
    await travel(-1)
    expect(router.currentRoute.value.meta.title).toBe('DGC 作业报表')
    await travel(1)
    expect(router.currentRoute.value.meta.title).toBe('MRS 作业报表')
    expect(router.currentRoute.value.meta.activeMenu).toBe(JOB_MENU_PATH)
    permissions.clear()
    permissions.add('ledger:view')
    for (const item of JOB_NAVIGATION) {
      await router.push(item.path)
      expect(router.currentRoute.value.path).toBe('/403')
    }
    await router.push('/gaussdb')
    expect(router.currentRoute.value.path).toBe('/gaussdb')
  })

  it('菜单撤权与默认首页回退使用现有守卫，资源旧入口继续保留查询条件', async () => {
    const { router, menu, permissions } = navigation(['role:manage'])
    await router.push('/dashboard')
    expect(router.currentRoute.value.path).toBe('/system')
    expect(menu.visibleMenu().map(group => group.title)).toEqual(['系统'])
    permissions.clear()
    expect(menu.visibleMenu()).toEqual([])
    await router.push('/system?tab=roles')
    expect(router.currentRoute.value.path).toBe('/403')
    permissions.add('ledger:view')
    await router.push('/resources?id=58&type=gaussdb_instance')
    expect(router.currentRoute.value.fullPath).toBe('/ledger?id=58&type=gaussdb_instance')
  })

  it('工作区入口不扩大子页面权限，旧资源 URL 共享正确选中项', async () => {
    const { router, permissions } = navigation(['report:view'])
    await router.push('/monitoring')
    expect(router.currentRoute.value.path).toBe('/monitoring')
    await router.push('/dcs')
    expect(router.currentRoute.value.meta.activeMenu).toBe('/monitoring')
    for (const path of ['/ecs', '/gaussdb']) {
      await router.push(path)
      expect(router.currentRoute.value.path).toBe('/403')
    }
    permissions.add('ledger:view')
    for (const path of ['/ecs', '/gaussdb']) {
      await router.push(`${path}?instance_id=58&tenant_id=7`)
      expect(router.currentRoute.value.path).toBe(path)
      expect(router.currentRoute.value.meta.activeMenu).toBe('/monitoring')
      expect(router.currentRoute.value.query).toEqual({ instance_id: '58', tenant_id: '7' })
    }
  })

  it('仅规则权限的用户直接进入可见子页，侧栏仍选择同一告警工作区', async () => {
    const { menu, router } = navigation(['rule:view'])
    expect(menu.visibleMenu().find(group => group.title === '告警管理')?.items).toEqual([
      expect.objectContaining({ path: '/rules', activePath: '/alarms', title: '告警管理' }),
    ])
    await router.push('/rules')
    expect(router.currentRoute.value.meta.activeMenu).toBe('/alarms')
    expect(router.currentRoute.value.path).toBe('/rules')
    await router.push('/alarms')
    expect(router.currentRoute.value.path).toBe('/403')
  })
})
