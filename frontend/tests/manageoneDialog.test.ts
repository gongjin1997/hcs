import { afterEach, describe, expect, it, vi } from 'vitest'
import { allNodes, clickButton, findNode, mountPersonnel, setField, settlePersonnel, textOf } from './personnelHarness'
import type { ManageOneConfig, ManageOneConfigIn } from '../src/types/manageone'
import { useManageOneConfig } from '../src/views/ecs/useManageOneConfig'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
const response = (id: number): ManageOneConfig => ({ tenant_id: id, enabled: false, configured: true, selected: false,
  endpoint: 'https://manageone.test.invalid', auth_type: 'PASSWORD', access_key_mask: null, secret_key_set: false,
  username: 'integration', organization: '', secret_set: true })
const permissions = (...grants: string[]) => ({
  useUserStore: () => ({ user: { id: 1, tenant_ids: [7, 8] }, can: (key: string) => grants.includes(key), canAny: (...keys: string[]) => keys.some(key => grants.includes(key)) }),
})
function mountDialog(tenantId: number | null, grants: string[], config: Partial<ManageOneConfig> = {}) {
  const api = {
    getManageOneConfig: vi.fn(async (id: number) => ({ ...response(id), ...config })),
    saveManageOneConfig: vi.fn(async (_id: number, _body: ManageOneConfigIn): Promise<void> => undefined),
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
    expect(textOf(view.root)).toContain('ManageOne 运维北向独立凭据')
    expect(textOf(view.root)).toContain('当前未选为 ECS 指标来源')
    expect(textOf(view.root)).toContain('无需安装 Prometheus')
    expect(textOf(view.root)).toContain('云硬盘使用率')
    expect(textOf(view.root)).toContain('开机时间暂不支持')
  })
  it('新空配置默认 AK/SK，只有当前方式的输入；保存时不携带密码', async () => {
    const view = mountDialog(7, ['tenant:view', 'tenant:manage'], { configured: false, endpoint: '', username: '', organization: null, secret_set: false })
    await settlePersonnel()
    expect(findNode(view.root, 'Select', node => node.props['aria-label'] === 'ManageOne 认证方式').props.modelValue).toBe('AKSK')
    expect(allNodes(view.root).some(node => node.props['aria-label'] === 'ManageOne 密码')).toBe(false)
    await setField(view.root, '运维北向 baseURL', 'https://manageone.test.invalid')
    await setField(view.root, 'AK', 'actual-ak')
    await setField(view.root, 'SK', 'actual-sk')
    await setField(view.root, '启用配置', true)
    await clickButton(view.root, '保存配置')
    expect(view.api.saveManageOneConfig.mock.calls[0][1]).toEqual({ enabled: true, endpoint: 'https://manageone.test.invalid',
      auth_type: 'AKSK', access_key: 'actual-ak', secret_key: 'actual-sk', clear_secret_key: false, select_source: false })
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne AK').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne SK').props.modelValue).toBe('')
    expect(textOf(view.root)).not.toMatch(/actual-ak|actual-sk/)
    expect(view.messages.success).toHaveBeenCalledOnce()
  })
  it('旧 PASSWORD 配置显示原账号；切换方式再返回时草稿已清理', async () => {
    const view = mountDialog(7, ['tenant:view', 'tenant:manage'])
    await settlePersonnel()
    expect(findNode(view.root, 'Select', node => node.props['aria-label'] === 'ManageOne 认证方式').props.modelValue).toBe('PASSWORD')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne 集成账号').props.modelValue).toBe('integration')
    await setField(view.root, '密码', 'draft-password')
    await setField(view.root, '认证方式', 'AKSK')
    expect(allNodes(view.root).some(node => node.props['aria-label'] === 'ManageOne 密码')).toBe(false)
    await setField(view.root, 'AK', 'draft-ak')
    await setField(view.root, 'SK', 'draft-sk')
    await setField(view.root, '认证方式', 'PASSWORD')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne 密码').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne 集成账号').props.modelValue).toBe('')
    await setField(view.root, '认证方式', 'AKSK')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne AK').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne SK').props.modelValue).toBe('')
  })
  it('只读 AK/SK 配置仅显示掩码和 SK 设置状态', async () => {
    const view = mountDialog(7, ['tenant:view'], { auth_type: 'AKSK', username: '', secret_set: false, access_key_mask: 'AKTE****LAST', secret_key_set: true })
    await settlePersonnel()
    expect(textOf(view.root)).toContain('AKTE****LAST')
    expect(textOf(view.root)).toContain('已设置（不回显）')
    expect(allNodes(view.root).some(node => ['ManageOne AK', 'ManageOne SK', 'ManageOne 密码'].includes(String(node.props['aria-label'])))).toBe(false)
    expect(allNodes(view.root).some(node => node.tag === 'Button' && textOf(node).trim() === '保存配置')).toBe(false)
    expect(findNode(view.root, 'Form').props.disabled).toBe(true)
  })
  it('更换 AK 必须重填 SK，禁用后才能清除旧 SK', async () => {
    const view = mountDialog(7, ['tenant:view', 'tenant:manage'], { enabled: true, auth_type: 'AKSK', username: '', secret_set: false,
      access_key_mask: 'AKTE****LAST', secret_key_set: true })
    await settlePersonnel()
    expect(findNode(view.root, 'Checkbox', node => node.props['aria-label'] === '清除 ManageOne SK').props.disabled).toBe(true)
    await setField(view.root, 'AK', 'new-ak')
    await clickButton(view.root, '保存配置')
    expect(view.api.saveManageOneConfig).not.toHaveBeenCalled()
    expect(findNode(view.root, 'Alert').props.title).toContain('重新填写 SK')
    await setField(view.root, '启用配置', false)
    const clear = findNode(view.root, 'Checkbox', node => node.props['aria-label'] === '清除 ManageOne SK')
    expect(clear.props.disabled).toBe(false)
    ;(clear.props['onUpdate:modelValue'] as (value: boolean) => void)(true)
    await setField(view.root, 'AK', 'new-ak')
    await clickButton(view.root, '保存配置')
    expect(view.api.saveManageOneConfig.mock.calls[0][1]).toMatchObject({ enabled: false, access_key: 'new-ak', clear_secret_key: true })
    expect(view.api.saveManageOneConfig.mock.calls[0][1]).not.toHaveProperty('secret_key')
  })
  it('关闭后重新打开及租户切换都清理 AK/SK 输入', async () => {
    const view = mountDialog(7, ['tenant:view', 'tenant:manage'], { auth_type: 'AKSK', username: '', secret_set: false, access_key_mask: 'AKTE****LAST', secret_key_set: true })
    await settlePersonnel()
    await setField(view.root, 'AK', 'draft-ak'); await setField(view.root, 'SK', 'draft-sk')
    view.updateProps({ modelValue: false }); await settlePersonnel()
    view.updateProps({ modelValue: true }); await settlePersonnel()
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne AK').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne SK').props.modelValue).toBe('')
    await setField(view.root, 'AK', 'draft-ak'); await setField(view.root, 'SK', 'draft-sk')
    view.updateProps({ tenantId: 8 }); await settlePersonnel()
    expect(view.api.getManageOneConfig.mock.calls.at(-1)?.[0]).toBe(8)
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne AK').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne SK').props.modelValue).toBe('')
  })
  it('AK/SK 保存失败不保留 AK/SK 输入或展示上游错误', async () => {
    const view = mountDialog(7, ['tenant:view', 'tenant:manage'], { auth_type: 'AKSK', username: '', secret_set: false, access_key_mask: 'AKTE****LAST', secret_key_set: true })
    await settlePersonnel()
    await setField(view.root, 'AK', 'failed-ak'); await setField(view.root, 'SK', 'failed-sk')
    view.api.saveManageOneConfig.mockRejectedValue(new Error('failed-ak failed-sk'))
    await clickButton(view.root, '保存配置')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne AK').props.modelValue).toBe('')
    expect(findNode(view.root, 'Input', node => node.props['aria-label'] === 'ManageOne SK').props.modelValue).toBe('')
    expect(textOf(view.root)).not.toMatch(/failed-ak|failed-sk/)
    expect(view.messages.success).not.toHaveBeenCalled()
  })
  it('范围外租户不读取或显示凭据表单', async () => {
    const view = mountDialog(99, ['tenant:view', 'tenant:manage'])
    await settlePersonnel()
    expect(view.api.getManageOneConfig).not.toHaveBeenCalled()
    expect(textOf(view.root)).toContain('无此租户')
    expect(allNodes(view.root).some(node => node.tag === 'Form')).toBe(false)
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
