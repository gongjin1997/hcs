import { afterEach, describe, expect, it, vi } from 'vitest'
import { allNodes, clickButton, findNode, mountPersonnel, setField, settlePersonnel, textOf } from './personnelHarness'
import type { ManageOneConfig, ManageOneConfigIn } from '../src/types/manageone'
import { useManageOneConfig } from '../src/views/ecs/useManageOneConfig'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
const response = (id: number): ManageOneConfig => ({ tenant_id: id, enabled: false, configured: true, selected: false,
  endpoint: 'https://manageone.test.invalid', username: 'integration', organization: '', secret_set: true })
const permissions = (...grants: string[]) => ({
  useUserStore: () => ({ user: { id: 1, tenant_ids: [7, 8] }, can: (key: string) => grants.includes(key), canAny: (...keys: string[]) => keys.some(key => grants.includes(key)) }),
})
function mountDialog(tenantId: number | null, grants: string[]) {
  const api = {
    getManageOneConfig: vi.fn(async (id: number) => response(id)),
    saveManageOneConfig: vi.fn(async (id: number, _body: ManageOneConfigIn) => ({ ...response(id), enabled: true, selected: true })),
  }
  const view = mountPersonnel('ecs/ManageOneConfigDialog.vue', {
    props: { modelValue: true, tenantId, tenantName: tenantId ? `租户 ${tenantId}` : '' },
    dependencies: {
      '@/api/manageone': api,
      '@/config/resource-navigation': { resourceViewerScopeKey: () => 'session-scope' },
      './useManageOneConfig': { useManageOneConfig },
      '@/stores/app': { useAppStore: () => ({ tenants: [{ id: 7, name: '租户 7' }, { id: 8, name: '租户 8' }] }) },
      '@/stores/user': permissions(...grants),
    },
  })
  dispose.push(view.unmount)
  return { ...view, api }
}

describe('ManageOne 配置对话框', () => {
  it('全租户范围必须先选具体租户才读取，显示短协议说明和当前来源状态', async () => {
    const view = mountDialog(null, ['tenant:view'])
    await settlePersonnel()
    expect(view.api.getManageOneConfig).not.toHaveBeenCalled()
    await setField(view.root, '租户', 8)
    expect(view.api.getManageOneConfig).toHaveBeenCalledOnce()
    expect(view.api.getManageOneConfig.mock.calls[0][0]).toBe(8)
    expect(textOf(view.root)).toContain('ManageOne 运维北向集成账号')
    expect(textOf(view.root)).toContain('当前未选为 ECS 指标来源')
    expect(textOf(view.root)).toContain('无需安装 Prometheus')
    expect(textOf(view.root)).toContain('云硬盘使用率')
    expect(textOf(view.root)).toContain('开机时间暂不支持')
  })
  it('只读角色可查看范围内配置但不显示密码输入、来源选择或保存按钮', async () => {
    const view = mountDialog(7, ['tenant:view'])
    await settlePersonnel()
    expect(view.api.getManageOneConfig).toHaveBeenCalledOnce()
    expect(view.api.getManageOneConfig.mock.calls[0][0]).toBe(7)
    expect(textOf(view.root)).toContain('已设置（不回显）')
    expect(allNodes(view.root).some(node => node.props['aria-label'] === 'ManageOne 密码')).toBe(false)
    expect(allNodes(view.root).some(node => node.tag === 'Checkbox' && textOf(node).includes('用于 ECS 指标采集'))).toBe(false)
    expect(allNodes(view.root).some(node => node.tag === 'Button' && textOf(node).trim() === '保存配置')).toBe(false)
  })
  it('tenant:manage 显示显式来源选择；PUT 失败不提示成功或回显原始错误', async () => {
    const view = mountDialog(7, ['ledger:view', 'tenant:manage'])
    await settlePersonnel()
    expect(textOf(view.root)).toContain('当前未选为 ECS 指标来源')
    expect(allNodes(view.root).some(node => node.tag === 'Checkbox' && textOf(node).includes('用于 ECS 指标采集'))).toBe(true)
    view.api.saveManageOneConfig.mockRejectedValue(new Error('private-secret'))
    await clickButton(view.root, '保存配置')
    expect(view.api.saveManageOneConfig).toHaveBeenCalledOnce()
    expect(view.api.saveManageOneConfig.mock.calls[0][1]).toMatchObject({ enabled: false, select_source: false })
    expect(findNode(view.root, 'Alert', node => node.props.title === '保存 ManageOne 配置失败，请检查配置后重试')).toBeTruthy()
    expect(textOf(view.root)).not.toContain('private-secret')
    expect(view.messages.success).not.toHaveBeenCalled()
  })
})
