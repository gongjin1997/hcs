import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Contact, UserAdmin, UserCreate, UserImportResult, UserPatch } from '../src/types'
import { allNodes, clickButton, findNode, formField, mountPersonnel, setField, settlePersonnel, textOf } from './personnelHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
const user = (overrides: Partial<UserAdmin> = {}): UserAdmin => ({
  id: 9, username: 'directory', display_name: '登记人员', email: null, phone: '123', role_id: null, role_code: null,
  role_name: '未分配角色', tenant_ids: [7], enabled: true, login_enabled: false, notify_enabled: true,
  contact_id: 91, owner_count: 2, groups: ['运维组'], last_login_at: null, created_at: null, locked: false, has_password: false, ...overrides,
})
function usersApi(rows: UserAdmin[] = []) {
  return {
    listUsers: vi.fn(async () => rows), listRoles: vi.fn(async () => [{ id: 2, code: 'oncall', name: '值班员', permissions: [], description: '' }]),
    listPermissions: vi.fn(async () => []), listTenants: vi.fn(async () => []),
    createUser: vi.fn(async (_body: UserCreate) => user()), patchUser: vi.fn(async (_id: number, _body: UserPatch) => user()), resetPassword: vi.fn(),
    exportUsers: vi.fn(async () => '人员.xlsx'), downloadUserImportTemplate: vi.fn(async () => '模板.xlsx'),
  }
}
async function mountUsers(rows: UserAdmin[] = [], tenantIds: number[] | null = null) {
  const api = usersApi(rows)
  const view = mountPersonnel('system/UserPanel.vue', { api, user: { id: 1, tenant_ids: tenantIds } })
  dispose.push(view.unmount)
  await settlePersonnel()
  return { ...view, api }
}

describe('统一用户 / 人员表单', () => {
  it('默认仅登记人员，角色 / 密码可留空，传电话与独立开关', async () => {
    const { root, api } = await mountUsers()
    await clickButton(root, '新增用户')
    expect(findNode(formField(root, '开通登录'), 'Switch').props.modelValue).toBe(false)
    expect(allNodes(root).some(item => item.tag === 'FormItem' && item.props.label === '初始密码')).toBe(false)
    await setField(root, '账号', 'directory')
    await setField(root, '姓名', '登记人员')
    await setField(root, '电话', ' 12345 ')
    await setField(root, '接收通知', false)
    await clickButton(root, '保存')
    expect(api.createUser).toHaveBeenCalledWith(expect.objectContaining({
      username: 'directory', display_name: '登记人员', phone: '12345', role_id: null,
      enabled: true, login_enabled: false, notify_enabled: false,
    }))
    expect(api.createUser.mock.calls[0][0]).not.toHaveProperty('password')
  })

  it('新增开通登录后显示初始密码且要求角色，关闭后不传隐藏密码', async () => {
    const { root, api } = await mountUsers()
    await clickButton(root, '新增用户')
    await setField(root, '开通登录', true)
    const rules = findNode(root, 'Form').props.rules as { role_id: { required: boolean }[]; password: { required?: boolean }[] }
    expect(rules.role_id[0].required).toBe(true)
    expect(rules.password[0].required).toBe(true)
    await setField(root, '初始密码', 'do-not-submit-hidden')
    await setField(root, '开通登录', false)
    await setField(root, '账号', 'directory')
    await setField(root, '姓名', '登记人员')
    await clickButton(root, '保存')
    expect(api.createUser.mock.calls[0][0]).not.toHaveProperty('password')
  })

  it.each([false, true])('空租户范围仅允许不开通登录的人员（login_enabled=%s）', async loginEnabled => {
    const { root, api, messages } = await mountUsers()
    await clickButton(root, '新增用户')
    await setField(root, '账号', 'directory')
    await setField(root, '姓名', '登记人员')
    await setField(root, '数据范围', false)
    if (loginEnabled) {
      await setField(root, '开通登录', true)
      await setField(root, '角色', 2)
      await setField(root, '初始密码', 'initial-password')
    }
    await clickButton(root, '保存')
    if (loginEnabled) {
      expect(api.createUser).not.toHaveBeenCalled()
      expect(messages.warning).toHaveBeenCalledWith(expect.stringContaining('至少选择一个租户'))
    } else expect(api.createUser).toHaveBeenCalledWith(expect.objectContaining({ tenant_ids: [], login_enabled: false }))
  })

  it('已登记人员首次开通登录用 PATCH 同时提交 login_enabled / role_id / password', async () => {
    const { root, api } = await mountUsers([user()])
    expect(textOf(root)).toContain('仅手机号')
    expect(allNodes(root).some(item => item.tag === 'Button' && textOf(item) === '重置密码')).toBe(false)
    await clickButton(root, '编辑')
    expect(findNode(formField(root, '账号'), 'Input').props.disabled).toBe(true)
    await setField(root, '开通登录', true)
    await setField(root, '角色', 2)
    await setField(root, '初始密码', 'initial-password')
    await clickButton(root, '保存')
    expect(api.patchUser).toHaveBeenCalledWith(9, expect.objectContaining({ login_enabled: true, role_id: 2, password: 'initial-password', phone: '123' }))
  })

  it('已开通登录者编辑基本信息不显示 / 不提交密码', async () => {
    const { root, api } = await mountUsers([user({ login_enabled: true, role_id: 2 })])
    await clickButton(root, '编辑')
    expect(allNodes(root).some(item => item.tag === 'FormItem' && item.props.label === '初始密码')).toBe(false)
    await setField(root, '电话', '999')
    await clickButton(root, '保存')
    expect(api.patchUser.mock.calls[0][1]).not.toHaveProperty('password')
  })

  it.each([true, undefined])('已有密码或旧接口未返回密码标志时，重新启用登录不重置密码（has_password=%s）', async hasPassword => {
    const { root, api } = await mountUsers([user({ has_password: hasPassword, login_enabled: false, role_id: 2 })])
    await clickButton(root, '编辑')
    await setField(root, '开通登录', true)
    expect(allNodes(root).some(item => item.tag === 'FormItem' && item.props.label === '初始密码')).toBe(false)
    await clickButton(root, '保存')
    expect(api.patchUser).toHaveBeenCalledWith(9, expect.objectContaining({ login_enabled: true, role_id: 2 }))
    expect(api.patchUser.mock.calls[0][1]).not.toHaveProperty('password')
  })

  it('本人的总启停 / 登录开关不可关闭', async () => {
    const { root } = await mountUsers([user({ id: 1, login_enabled: true, role_id: 2 })])
    expect(findNode(root, 'Switch').props.disabled).toBe(true)
    await clickButton(root, '编辑')
    expect(findNode(formField(root, '人员启用'), 'Switch').props.disabled).toBe(true)
    expect(findNode(formField(root, '开通登录'), 'Switch').props.disabled).toBe(true)
  })

  it('有 user:manage 但租户范围受限时不读取全局人员，也不显示导入导出', async () => {
    const { root, api } = await mountUsers([], [7])
    expect(api.listUsers).not.toHaveBeenCalled()
    expect(allNodes(root).some(item => item.tag === 'Button' && /Excel|新增用户|下载模板/.test(textOf(item)))).toBe(false)
  })

  it('表格标题栏可下载模板和导出', async () => {
    const { root, api } = await mountUsers()
    await clickButton(root, '下载模板')
    await clickButton(root, '导出 Excel')
    expect(api.downloadUserImportTemplate).toHaveBeenCalledOnce()
    expect(api.exportUsers).toHaveBeenCalledOnce()
  })
})

const contacts: Contact[] = [
  { id: 11, user_id: 101, username: 'alpha', name: '同名人员', email: 'alpha@example.test', phone: null, enabled: true, groups: [] },
  { id: 12, user_id: 102, username: 'beta', name: '同名人员', email: null, phone: null, enabled: false, groups: [] },
]
describe('只读通知人员目录与联系组引用', () => {
  it('目录无独立 CRUD，按账号搜索并链接到真实 users 页签', async () => {
    const view = mountPersonnel('notify/ContactPanel.vue', { props: { contacts } })
    dispose.push(view.unmount)
    expect(textOf(view.root)).toContain('本页只读')
    expect(allNodes(view.root).some(item => item.tag === 'Button' && /新建联系人|编辑|删除/.test(textOf(item)))).toBe(false)
    await clickButton(view.root, '前往用户管理')
    expect(view.push).toHaveBeenCalledWith('/system?tab=users')
    const search = findNode(view.root, 'Input')
    ;(search.props['onUpdate:modelValue'] as (value: string) => void)('beta')
    await settlePersonnel()
    expect(findNode(view.root, 'Table').props.data).toEqual([contacts[1]])
  })

  it('通知管理员无全局人员管理权限时保留只读目录', () => {
    const view = mountPersonnel('notify/ContactPanel.vue', { props: { contacts }, can: permission => permission === 'notify:manage' })
    dispose.push(view.unmount)
    expect(allNodes(view.root).some(item => item.tag === 'Button')).toBe(false)
    expect(textOf(view.root)).toContain('用户管理')
  })

  it('按姓名和账号区分同名人员，组成员 / 组长提交联系人投影 ID', async () => {
    const createGroup = vi.fn(async () => ({ id: 1 }))
    const view = mountPersonnel('notify/GroupDialog.vue', { api: { createGroup }, props: { visible: true, group: null, contacts } })
    dispose.push(view.unmount)
    const { root } = view
    expect(findNode(root, 'Option', item => item.props.label === '同名人员（alpha）').props.value).toBe(11)
    expect(findNode(root, 'Option', item => item.props.label === '同名人员（beta）').props.disabled).toBe(true)
    expect(textOf(root)).toContain('系统管理 → 用户管理')
    await setField(root, '代码', 'ops-team')
    await setField(root, '名称', '运维组')
    await setField(root, '成员', [11])
    await setField(root, '组长', [11])
    await clickButton(root, '保存')
    expect(createGroup).toHaveBeenCalledWith({ code: 'ops-team', name: '运维组', member_ids: [11], leader_ids: [11] })
  })

  it('系统管理实际接受 ?tab=users', async () => {
    const view = mountPersonnel('system/SystemView.vue', { api: { systemInfo: vi.fn(async () => ({ mock: false })) }, dependencies: {
      'vue-router': { useRoute: () => ({ query: { tab: 'users' } }), useRouter: () => ({ replace: vi.fn() }) },
    } })
    dispose.push(view.unmount)
    await settlePersonnel()
    expect(findNode(view.root, 'Tabs').props.modelValue).toBe('users')
  })
})

const preview = (error = false): UserImportResult => ({
  total: 1, created: 0, updated: 1, unchanged: 0, error_count: error ? 1 : 0,
  errors: error ? [{ row: 2, username: 'directory', message: '角色不存在' }] : [],
  items: [{ row: 2, username: 'directory', action: 'update', changes: { 姓名: ['旧姓名', '新姓名'] } }], dry_run: true, applied: 0,
})
describe('人员导入预览界面', () => {
  it.each([false, true])('展示变更与错误，无跳过错误入口（错误=%s）', async error => {
    const importUsers = vi.fn(async () => preview(error))
    const view = mountPersonnel('system/UserImportDialog.vue', { api: { importUsers }, props: { visible: true, canManage: true } })
    dispose.push(view.unmount)
    const file = new File(['fixture'], '人员.xlsx')
    await (findNode(view.root, 'Upload').props['on-change'] as (file: object) => Promise<void>)({ raw: file })
    await settlePersonnel()
    expect(importUsers).toHaveBeenCalledWith(file, true, expect.any(AbortSignal))
    expect(textOf(view.root)).toContain(error ? '角色不存在' : '旧姓名')
    const confirm = findNode(view.root, 'Button', item => textOf(item).startsWith('确认导入'))
    expect(confirm.props.disabled).toBe(error)
    expect(allNodes(view.root).some(item => item.tag === 'Checkbox')).toBe(false)
    if (!error) {
      importUsers.mockResolvedValueOnce({ ...preview(), dry_run: false, applied: 1 })
      await clickButton(view.root, '确认导入（1 位人员）')
      expect(importUsers).toHaveBeenLastCalledWith(file, false)
      expect(view.messages.success).toHaveBeenCalledOnce()
    }
  })
})
