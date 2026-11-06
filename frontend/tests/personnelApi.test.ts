import { beforeEach, describe, expect, it, vi } from 'vitest'

const request = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), download: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { createUser, downloadUserImportTemplate, exportLedger, exportUsers, getClassifications, importUsers, listUserOptions, patchUser, saveClassifications, saveReportSchedule, sendReport } from '../src/api'
import { sendMailTest } from '../src/api/mail'

beforeEach(() => { vi.clearAllMocks() })
describe('统一人员接口契约', () => {
  it('收件人选项包含停用人员状态，仍绑定请求租户', async () => {
    await listUserOptions(7, undefined, true)
    expect(request.get).toHaveBeenCalledWith('/system/user-options', { tenant_id: 7, include_disabled: true }, { signal: undefined, headers: { 'X-Tenant-Id': '7' } })
  })
  it('报表、定时配置和测试邮件只提交人员 ID', async () => {
    await sendReport(9, [101])
    await saveReportSchedule({ recipient_user_ids: [102] })
    await sendMailTest([103])
    expect(request.post).toHaveBeenCalledWith('/reports/9/send', { recipient_user_ids: [101] }, { timeout: 60000 })
    expect(request.put).toHaveBeenCalledWith('/reports/schedule', { recipient_user_ids: [102] })
    expect(request.post).toHaveBeenCalledWith('/notify/test-mail', { user_ids: [103] }, expect.objectContaining({ timeout: 60000 }))
  })
  it('分类配置写入不含统计，导出携带分类筛选', async () => {
    const signal = new AbortController().signal
    await getClassifications()
    await saveClassifications({ rules: [], fallback: '其他', counts: { 其他: 2 } })
    await exportLedger({ resource_type: 'ecs_server', classification: 'MRS' }, { signal, headers: { 'X-Tenant-Id': '7' } })
    expect(request.get).toHaveBeenCalledWith('/resources/classifications')
    expect(request.put).toHaveBeenCalledWith('/resources/classifications', { rules: [], fallback: '其他' }, { headers: { 'X-Tenant-Id': 'all' } })
    expect(request.download).toHaveBeenCalledWith('/resources/export', { resource_type: 'ecs_server', classification: 'MRS' }, '资源台账.xlsx',
      { signal, headers: { 'X-Tenant-Id': '7' } })
  })
  it('导出 / 模板使用 XLSX blob 下载，范围不受顶栏租户选择影响', async () => {
    await exportUsers()
    await downloadUserImportTemplate()
    expect(request.download).toHaveBeenNthCalledWith(1, '/system/users/export', undefined, '用户管理.xlsx', { headers: { 'X-Tenant-Id': 'all' } })
    expect(request.download).toHaveBeenNthCalledWith(2, '/system/users/import/template', undefined, '用户导入模板.xlsx', { headers: { 'X-Tenant-Id': 'all' } })
  })

  it.each([true, false])('导入用 multipart 文件与 dry_run=%s，无 skip_errors', async dryRun => {
    const file = new File(['fixture'], '人员.xlsx')
    const signal = new AbortController().signal
    await importUsers(file, dryRun, signal)
    const [path, body, config] = request.post.mock.calls[0]
    expect(path).toBe('/system/users/import')
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)
    expect(config).toMatchObject({ params: { dry_run: dryRun }, signal, headers: { 'X-Tenant-Id': 'all' } })
    expect(config.params).not.toHaveProperty('skip_errors')
  })

  it.each([7, 8, null])('负责人读取资源所属租户 %s 的安全用户选项', async tenantId => {
    const signal = new AbortController().signal
    await listUserOptions(tenantId, signal)
    expect(request.get).toHaveBeenCalledWith('/system/user-options', { tenant_id: tenantId ?? undefined }, {
      signal, headers: { 'X-Tenant-Id': tenantId == null ? 'all' : String(tenantId) },
    })
  })

  it('仅登记人员不传密码 / 角色，首次开通通过 PATCH 传初始密码', async () => {
    const body = { username: 'directory', display_name: '人员', tenant_ids: [7], login_enabled: false, notify_enabled: true, phone: '123' }
    await createUser(body)
    expect(request.post).toHaveBeenCalledWith('/system/users', body)
    await patchUser(9, { login_enabled: true, role_id: 2, password: 'initial-password' })
    expect(request.patch).toHaveBeenCalledWith('/system/users/9', { login_enabled: true, role_id: 2, password: 'initial-password' })
  })
})
