import { beforeEach, describe, expect, it, vi } from 'vitest'
const request = vi.hoisted(() => ({ get: vi.fn(async (..._args: unknown[]) => ({})), put: vi.fn(async (..._args: unknown[]) => ({})) }))
vi.mock('../src/api/request', () => request)
import { getManageOneConfig, saveManageOneConfig } from '../src/api/manageone'
import type { ManageOneConfigIn } from '../src/types/manageone'
beforeEach(() => vi.clearAllMocks())
describe('ManageOne API 租户与提交契约', () => {
  it('GET 与 PUT 显式捕获租户头及取消信号，不受顶部租户迟到变化影响', async () => {
    const context = { signal: new AbortController().signal }
    await getManageOneConfig(7, context)
    expect(request.get).toHaveBeenCalledWith('/system/tenants/7/manageone', undefined, { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
    const result = await saveManageOneConfig(7, { enabled: false, endpoint: '', auth_type: 'PASSWORD', username: '', organization: '', password: '' }, context)
    expect(result).toBeUndefined()
    expect(request.put).toHaveBeenCalledWith('/system/tenants/7/manageone', { enabled: false, endpoint: '', auth_type: 'PASSWORD', username: '', organization: '', clear_password: false, select_source: false }, { signal: context.signal, silent: true, headers: { 'X-Tenant-Id': '7' } })
  })
  it('AK/SK 白名单包含新字段，排除账号密码、未知字段和采集开关', async () => {
    const body = { enabled: true, endpoint: 'https://manageone.test.invalid', auth_type: 'AKSK', access_key: 'actual-ak', secret_key: 'actual-sk',
      clear_secret_key: false, select_source: true, username: 'irrelevant', organization: 'irrelevant', password: 'do-not-send',
      clear_password: true, unknown_secret: 'do-not-send-either', metric_keys: ['cpu'], task_enabled: true } as ManageOneConfigIn
    await saveManageOneConfig(7, body, { signal: new AbortController().signal })
    expect(request.put.mock.calls[0][1]).toEqual({ enabled: true, endpoint: 'https://manageone.test.invalid', auth_type: 'AKSK',
      access_key: 'actual-ak', secret_key: 'actual-sk', clear_secret_key: false, select_source: true })
  })
  it('AK/SK 空值不提交，显式清除 SK 不被白名单丢失', async () => {
    await saveManageOneConfig(7, { enabled: false, endpoint: '', auth_type: 'AKSK', access_key: '', secret_key: '', clear_secret_key: true },
      { signal: new AbortController().signal })
    expect(request.put.mock.calls[0][1]).toEqual({ enabled: false, endpoint: '', auth_type: 'AKSK', clear_secret_key: true, select_source: false })
  })
  it('PASSWORD 白名单继续提交原字段，排除 AK/SK 及其清除选项', async () => {
    await saveManageOneConfig(7, { enabled: false, endpoint: '', auth_type: 'PASSWORD', username: 'integration', organization: 'org',
      password: 'new-password', clear_password: false, access_key: 'do-not-send-ak', secret_key: 'do-not-send-sk', clear_secret_key: true },
      { signal: new AbortController().signal })
    expect(request.put.mock.calls[0][1]).toEqual({ enabled: false, endpoint: '', auth_type: 'PASSWORD', username: 'integration', organization: 'org',
      password: 'new-password', clear_password: false, select_source: false })
  })
  it.each([0, -1, Number.NaN])('拒绝无具体租户 %s，绝不访问 all/manageone', id => {
    expect(() => getManageOneConfig(id, { signal: new AbortController().signal })).toThrow('具体租户')
    expect(request.get).not.toHaveBeenCalled()
  })
})
