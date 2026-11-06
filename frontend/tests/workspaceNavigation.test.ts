import { describe, expect, it } from 'vitest'
import { JOB_NAVIGATION } from '../src/config/job-navigation'
import { WORKSPACES, healthGroupLocation, permittedItems, workspaceById, workspaceFor, workspaceMeta, workspacePermissions } from '../src/config/workspace-navigation'
import { tenantChangeQuery, tenantViewKey } from '../src/config/resource-navigation'

describe('业务工作区唯一入口定义', () => {
  it('每个旧页面只属于一个工作区，告警与监控不会混入其他业务', () => {
    const paths = WORKSPACES.flatMap(item => item.items.map(page => page.path))
    expect(new Set(paths).size).toBe(paths.length)
    expect(workspaceFor('/ecs')?.id).toBe('monitoring')
    expect(workspaceFor('/dcs')?.id).toBe('monitoring')
    expect(workspaceFor('/gaussdb')?.id).toBe('monitoring')
    for (const path of ['/alarms', '/rules', '/notify', '/silences']) expect(workspaceFor(path)?.id).toBe('alarms')
    expect(workspaceFor('/system')).toBeUndefined()
    expect(workspaceFor('/ledger')).toBeUndefined()
  })

  it('按业务权限逐项显示，不因工作区可访问而扩大权限', () => {
    const reportOnly = (...permissions: string[]) => permissions.includes('report:view')
    const ledgerOnly = (...permissions: string[]) => permissions.includes('ledger:view')
    expect(permittedItems(workspaceById('monitoring'), reportOnly).map(item => item.path)).toEqual(['/monitoring', '/dcs'])
    expect(permittedItems(workspaceById('monitoring'), ledgerOnly).map(item => item.path)).toEqual(['/monitoring', '/ecs', '/gaussdb'])
    expect(permittedItems(workspaceById('alarms'), (...permissions) => permissions.includes('rule:view')).map(item => item.path)).toEqual(['/rules'])
    expect(permittedItems(workspaceById('alarms'), () => false)).toEqual([])
    expect(workspacePermissions('monitoring')).toEqual(['ledger:view', 'report:view'])
  })

  it('路由、面包屑和页内导航取同一标题、权限及一级选中项', () => {
    for (const workspace of WORKSPACES) for (const item of workspace.items) {
      expect(workspaceMeta(item.path)).toEqual({ title: item.title, perm: item.perm, activeMenu: workspace.menuPath })
    }
    expect(() => workspaceMeta('/not-a-workspace')).toThrow()
    expect(workspaceById('jobs').items.map(item => item.path)).toEqual(JOB_NAVIGATION.map(item => item.path))
  })

  it('总览分组落到实际业务，不将仅巡检的作业塞入台账筛选', () => {
    const all = () => true
    expect(healthGroupLocation('dgc_realtime', all)).toEqual({ path: '/job-reports/realtime' })
    expect(healthGroupLocation('dgc_batch', all)).toEqual({ path: '/job-reports/batch' })
    expect(healthGroupLocation('mrs', all)).toEqual({ path: '/job-reports/mrs' })
    expect(healthGroupLocation('gaussdb', all)).toEqual({ path: '/gaussdb' })
    expect(healthGroupLocation('dcs', all)).toEqual({ path: '/dcs' })
    expect(healthGroupLocation('ecs', all)).toEqual({ path: '/ecs' })
    expect(healthGroupLocation('cce', all)).toEqual({ path: '/pod-events' })
    expect(healthGroupLocation('invalid', all)).toBeNull()
    expect(healthGroupLocation('dcs', (...permissions) => permissions.includes('ledger:view'))).toBeNull()
    expect(healthGroupLocation('cce', () => false)).toBeNull()
  })

  it('切租户移除监控、台账和告警链接上的旧资源上下文，保留业务筛选', () => {
    expect(tenantChangeQuery('/ecs', { instance_id: '11', tenant_id: '1', tab: 'disk', keyword: 'api' }))
      .toEqual({ keyword: 'api' })
    expect(tenantChangeQuery('/ledger', { id: '124', tenant_id: '1', type: 'dcs_instance', keyword: 'cache' }))
      .toEqual({ type: 'dcs_instance', keyword: 'cache' })
    expect(tenantChangeQuery('/alarms', { id: '18', resource_ref: '124', tenant_id: '1', status_group: 'all', service: 'dcs' }))
      .toEqual({ status_group: 'all', service: 'dcs' })
    expect(tenantChangeQuery('/dashboard', { tenant_id: '1' })).toEqual({ tenant_id: '1' })
  })

  it('台账与告警在清理旧范围时保持页面挂载，其余页面按范围重挂载', () => {
    expect(tenantViewKey('/ledger', 1)).toBe(tenantViewKey('/ledger', 2))
    expect(tenantViewKey('/alarms', 1)).toBe(tenantViewKey('/alarms', 2))
    expect(tenantViewKey('/dcs', 1)).not.toBe(tenantViewKey('/dcs', 2))
  })
})
