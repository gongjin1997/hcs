import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { allNodes, findNode, mountPersonnel, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })

async function system(initial = '/system?tab=tenants', permissions: string[] | null = null, tenantIds: number[] | null = null) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/system', component: {} }] })
  await router.push(initial)
  const route = reactive({ get query() { return router.currentRoute.value.query } })
  const identity = reactive({ tenant_ids: tenantIds })
  const can = (permission: string) => permissions === null || permissions.includes(permission)
  const view = mountPersonnel('system/SystemView.vue', {
    api: { systemInfo: vi.fn(async () => ({ mock: false })) },
    dependencies: {
      'vue-router': { useRoute: () => route, useRouter: () => router },
      '@/stores/user': { useUserStore: () => ({ user: identity, can, canAny: (...values: string[]) => values.some(can) }) },
    },
  })
  dispose.push(view.unmount)
  await settlePersonnel()
  const tabNames = () => allNodes(view.root).filter(item => item.tag === 'TabPane').map(item => item.props.name)
  const selectTab = async (name: string) => {
    const tabs = findNode(view.root, 'Tabs')
    ;(tabs.props['onUpdate:modelValue'] as (value: string) => void)(name)
    await settlePersonnel()
  }
  const section = async (label: string) => {
    const button = findNode(view.root, 'button', item => textOf(item) === label)
    await (button.props.onClick as () => unknown)()
    await settlePersonnel()
  }
  const travel = async (delta: number) => {
    await new Promise<void>(resolve => {
      const stop = router.afterEach(() => { stop(); resolve() })
      router.go(delta)
    })
    await settlePersonnel()
  }
  return { ...view, router, identity, tabNames, selectTab, section, travel }
}

describe('系统分区与实际路由历史', () => {
  it('每次仅展示当前分区子项，切换分区与子项写入历史并保留其他 query', async () => {
    const view = await system('/system?tab=tenants&from=qa')
    expect(view.tabNames()).toEqual(['tenants', 'switches', 'tasks', 'platform'])
    await view.section('人员与权限')
    expect(view.router.currentRoute.value.query).toEqual({ tab: 'users', from: 'qa' })
    expect(view.tabNames()).toEqual(['users', 'roles'])
    await view.selectTab('roles')
    expect(view.router.currentRoute.value.query.tab).toBe('roles')
    await view.section('系统设置')
    expect(view.tabNames()).toEqual(['settings', 'audit'])
    await view.travel(-1)
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('roles')
    expect(view.tabNames()).toEqual(['users', 'roles'])
    await view.travel(-1)
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('users')
    await view.travel(1)
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('roles')
  })

  it('同组件外部 query 导航、accounts 旧链接及非法 tab 均同步到可见项', async () => {
    const view = await system('/system?tab=accounts')
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('tenants')
    await view.router.push('/system?tab=audit')
    await settlePersonnel()
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('audit')
    expect(view.tabNames()).toEqual(['settings', 'audit'])
    await view.router.push('/system?tab=unavailable')
    await settlePersonnel()
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('tenants')
  })

  it('按权限过滤分区，租户范围用户不能通过旧 URL 或页内导航进入用户管理', async () => {
    const view = await system('/system?tab=users', ['user:manage', 'role:manage', 'audit:view'], [7])
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('roles')
    expect(view.tabNames()).toEqual(['roles'])
    expect(textOf(view.root)).not.toContain('接入与采集')
    await view.selectTab('users')
    expect(view.router.currentRoute.value.query.tab).toBe('users')
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('roles')
    await view.section('系统设置')
    expect(view.tabNames()).toEqual(['audit'])
  })
})
