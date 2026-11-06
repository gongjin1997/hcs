import { beforeEach, describe, expect, it, vi } from 'vitest'
const request = vi.hoisted(() => ({ get: vi.fn(async () => ({})), put: vi.fn(async () => ({})) }))
vi.mock('../src/api/request', () => request)
import { getManageOneConfig, saveManageOneConfig } from '../src/api/manageone'
beforeEach(() => vi.clearAllMocks())
describe('ManageOne API 租户与提交契约', () => {
  it('GET 与 PUT 显式捕获租户头及取消信号，不受顶部租户迟到变化影响', async () => {
    const context = { signal: new AbortController().signal }
    await getManageOneConfig(7, context)
    expect(request.get).toHaveBeenCalledWith('/system/tenants/7/manageone', undefined, { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
    await saveManageOneConfig(7, { enabled: false, endpoint: '', username: '', organization: '', password: '' }, context)
    expect(request.put).toHaveBeenCalledWith('/system/tenants/7/manageone', { enabled: false, endpoint: '', username: '', organization: '', clear_password: false, select_source: false }, { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
  })
  it.each([0, -1, Number.NaN])('拒绝无具体租户 %s，绝不访问 all/manageone', id => {
    expect(() => getManageOneConfig(id, { signal: new AbortController().signal })).toThrow('具体租户')
    expect(request.get).not.toHaveBeenCalled()
  })
})
