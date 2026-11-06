import { afterEach, describe, expect, it, vi } from 'vitest'
import { mountBusinessScript } from './businessViewHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
const userDependency = (tenantIds: number[] | null) => ({
  '@/stores/user': { useUserStore: () => ({ user: { tenant_ids: tenantIds }, can: () => true, canAny: () => true, fetchMe: vi.fn(async () => undefined) }) },
})

describe('全局身份管理范围与顶栏选择独立', () => {
  it.each([null, [7], []] as const)('授权scope=%j时按身份范围控制用户页签', tenantIds => {
    const view = mountBusinessScript<{ tabs: { name: string }[] }>('system/SystemView.vue', { dependencies: userDependency(tenantIds == null ? null : [...tenantIds]), app: { tenantId: 7 } })
    dispose.push(view.unmount)
    expect(view.state.tabs.some(tab => tab.name === 'users')).toBe(tenantIds == null)
  })

  it('scoped usermanager不请求全局用户列表，但可以读取角色/权限元数据', async () => {
    const api = { listUsers: vi.fn(async () => []), listRoles: vi.fn(async () => []), listPermissions: vi.fn(async () => []) }
    const user = mountBusinessScript<{ load(): Promise<void> }>('system/UserPanel.vue', { api, dependencies: userDependency([7]) })
    dispose.push(user.unmount)
    await user.state.load()
    expect(api.listUsers).not.toHaveBeenCalled()
    const roles = mountBusinessScript<{ load(): Promise<void>; canManage: boolean; save(): Promise<void> }>('system/RolePanel.vue', { api, dependencies: userDependency([7]) })
    dispose.push(roles.unmount)
    await roles.state.load()
    expect(api.listRoles).toHaveBeenCalledTimes(1)
    expect(api.listPermissions).toHaveBeenCalledTimes(1)
    expect(roles.state.canManage).toBe(false)
    await roles.state.save()
  })

  it.each([null, [7]] as const)('角色写和共享任务配置仅全局身份可用：%j', tenantIds => {
    const dependencies = userDependency(tenantIds == null ? null : [...tenantIds])
    const role = mountBusinessScript<{ canManage: boolean }>('system/RolePanel.vue', { dependencies, app: { tenantId: 7 } })
    const task = mountBusinessScript<{ canEdit: boolean; canReportConfig: boolean; schedule: { editable: boolean } }>('system/TaskPanel.vue', { dependencies, app: { tenantId: 7 } })
    dispose.push(role.unmount, task.unmount)
    task.state.schedule = { editable: true }
    expect(role.state.canManage).toBe(tenantIds == null)
    expect(task.state.canEdit).toBe(tenantIds == null)
    expect(task.state.canReportConfig).toBe(tenantIds == null)
  })
})

describe('模拟投递范围', () => {
  it('scoped用户不可空租户或越权租户，授权租户可模拟', async () => {
    const api = { simulateRoute: vi.fn(async () => ({ recipients: [] })) }
    const view = mountBusinessScript<{ q: { tenant_id: number | null }; canEmptyTenant: boolean; run(): Promise<unknown> }>('notify/SimulatePanel.vue', {
      api, dependencies: userDependency([7]), app: { tenantId: null, tenants: [{ id: 7, name: 'QA tenant' }] },
    })
    dispose.push(view.unmount)
    expect(view.state.canEmptyTenant).toBe(false)
    view.state.q.tenant_id = null; await view.state.run()
    view.state.q.tenant_id = 8; await view.state.run()
    expect(api.simulateRoute).not.toHaveBeenCalled()
    view.state.q.tenant_id = 7; await view.state.run()
    expect(api.simulateRoute).toHaveBeenCalledTimes(1)
  })

  it('全局身份在全部租户范围可选择空租户模拟', async () => {
    const api = { simulateRoute: vi.fn(async () => ({ recipients: [] })) }
    const view = mountBusinessScript<{ q: { tenant_id: number | null }; canEmptyTenant: boolean; run(): Promise<unknown> }>('notify/SimulatePanel.vue', {
      api, dependencies: userDependency(null), app: { tenantId: null, tenants: [{ id: 7 }] },
    })
    dispose.push(view.unmount)
    view.state.q.tenant_id = null
    expect(view.state.canEmptyTenant).toBe(true)
    await view.state.run()
    expect(api.simulateRoute).toHaveBeenCalledWith(expect.objectContaining({ tenant_id: null }))
  })
})
