import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import axios, { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import ts from 'typescript'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type * as RequestApi from '../src/api/request'
import * as requestIds from '../src/utils/request-id'

const storage = new Map<string, string>()
const showError = vi.fn(), reload = vi.fn(), replace = vi.fn()
const clearSession = vi.fn(() => { storage.delete('hi_token'); storage.delete('hi_tenant') })
let pending: { config: InternalAxiosRequestConfig; reply: (data: unknown, status?: number, headers?: object) => void; cancel: () => void; networkFailure: () => void }[]
const anchor = { href: '', download: '', click: vi.fn() }

function setup(): typeof RequestApi {
  const adapter = (config: InternalAxiosRequestConfig) => new Promise<AxiosResponse>((ok, fail) => pending.push({
    config,
    reply(data, status = 200, headers = {}) {
      const response = { data, status, headers, config, statusText: String(status) }
      if (status >= 400) fail(new AxiosError('request failed', undefined, config, undefined, response))
      else ok(response)
    },
    cancel: () => fail(new axios.CanceledError()),
    networkFailure: () => fail(new AxiosError('Network Error', 'ERR_NETWORK', config)),
  }))
  const dependencies: Record<string, unknown> = {
    axios: { default: { ...axios, create: (config: object) => axios.create({ ...config, adapter }), get: (url: string, config: object) => axios.get(url, { ...config, adapter }) } },
    'element-plus': { ElMessage: { error: showError } },
    '@/stores/user': { useUserStore: () => ({ clearSession }) },
    '@/router': { default: { replace } },
    '@/utils/request-id': requestIds,
  }
  const source = readFileSync(resolve(import.meta.dirname, '../src/api/request.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const output = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`)
    return dependencies[name]
  }, output)
  return output as typeof RequestApi
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  storage.clear(); storage.set('hi_token', 'session-a'); storage.set('hi_tenant', '1')
  pending = []
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) ?? null, removeItem: (key: string) => storage.delete(key) })
  vi.stubGlobal('location', { pathname: '/dcs', search: '?view=memory', reload })
  vi.stubGlobal('document', { createElement: () => anchor })
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

describe('请求关联编号', () => {
  it('每次请求生成独立合法编号，包括成功请求', async () => {
    const api = setup()
    const first = api.get('/resources'), second = api.get('/alarms')
    await Promise.resolve()
    const ids = pending.map(({ config }) => config.headers['X-Request-Id'])
    expect(ids.every((id) => requestIds.validRequestId(id))).toBe(true)
    expect(ids[0]).not.toBe(ids[1])
    pending.forEach((request) => request.reply({ code: 0, data: [] }))
    await Promise.all([first, second])
  })
  it('合法的大小写不同请求头透传，服务端编号用于错误提示且仅提示一次', async () => {
    const request = setup().get('/resources', undefined, { headers: { 'x-request-id': 'client-id_12345678' } })
    await Promise.resolve()
    expect(pending[0].config.headers.get('X-Request-Id')).toBe('client-id_12345678')
    pending[0].reply({ code: 50001, message: '服务异常' }, 500, { 'x-request-id': 'server-id_12345678' })
    await expect(request).rejects.toMatchObject({ rawMessage: '服务异常', message: '服务异常（请求 ID：server-id_12345678）', requestId: 'server-id_12345678' })
    expect(showError).toHaveBeenCalledExactlyOnceWith('服务异常（请求 ID：server-id_12345678）')
  })
  it.each(['short', 'bad id 12345678', 'x'.repeat(65), false, null])('非法或禁用编号 %s 被替换', async (value) => {
    const request = setup().get('/resources', undefined, { headers: { 'X-Request-Id': value } })
    await Promise.resolve()
    const id = pending[0].config.headers.get('X-Request-Id')
    expect(requestIds.validRequestId(id)).toBe(id)
    expect(id).not.toBe(value)
    pending[0].reply({ code: 0, data: [] })
    await request
  })
  it('无响应时错误仍带发送编号，silent不增加提示', async () => {
    const request = setup().get('/resources', undefined, { silent: true })
    await Promise.resolve()
    const id = pending[0].config.headers.get('X-Request-Id')
    pending[0].networkFailure()
    await expect(request).rejects.toMatchObject({ code: -1, requestId: id, rawMessage: '网络异常，请稍后重试', message: `网络异常，请稍后重试（请求 ID：${id}）` })
    expect(showError).not.toHaveBeenCalled()
  })
  it('业务错误也携带编号并只弹一次，非法响应编号不进入提示', async () => {
    const request = setup().post('/rules', {}, { headers: { 'X-Request-Id': 'client-id_12345678' } })
    await Promise.resolve()
    pending[0].reply({ code: 40901, message: '已存在', data: { request_id: 'invalid request id' } }, 200, { 'x-request-id': 'bad id' })
    await expect(request).rejects.toMatchObject({ requestId: 'client-id_12345678' })
    expect(showError).toHaveBeenCalledExactlyOnceWith('已存在（请求 ID：client-id_12345678）')
  })
  it('没有合法响应头时采用后端错误体中的编号', async () => {
    const request = setup().get('/resources')
    await Promise.resolve()
    pending[0].reply({ code: 50001, message: '内部错误', data: { request_id: 'body-id_12345678' } }, 500)
    await expect(request).rejects.toMatchObject({ requestId: 'body-id_12345678' })
    expect(showError).toHaveBeenCalledExactlyOnceWith('内部错误（请求 ID：body-id_12345678）')
  })
  it('XLSX下载使用同样的编号透传和Blob错误提示', async () => {
    const request = setup().download('/dcs/export', { resource_ids: [1, 2] }, 'export.xlsx', { headers: { 'x-request-id': 'export-id_12345678', 'X-Tenant-Id': 'all' } })
    await Promise.resolve()
    expect(pending[0].config.headers.get('X-Request-Id')).toBe('export-id_12345678')
    expect(pending[0].config.headers.get('X-Tenant-Id')).toBe('all')
    pending[0].reply(new Blob([JSON.stringify({ code: 40901, message: '导出失败' })]), 409, { 'X-Request-Id': 'export-server_12345678' })
    await expect(request).rejects.toMatchObject({ requestId: 'export-server_12345678', code: 40901 })
    expect(showError).toHaveBeenCalledExactlyOnceWith('导出失败（请求 ID：export-server_12345678）')
    expect(anchor.click).not.toHaveBeenCalled()
  })
  it('解析Blob错误期间取消，不触发提示或会话清理', async () => {
    const controller = new AbortController()
    let resolveText!: (text: string) => void
    const body = new Blob()
    vi.spyOn(body, 'text').mockReturnValue(new Promise((resolve) => { resolveText = resolve }))
    const request = setup().download('/dcs/export', undefined, 'export.xlsx', { signal: controller.signal })
    await Promise.resolve()
    pending[0].reply(body, 401)
    await vi.waitFor(() => expect(body.text).toHaveBeenCalledOnce())
    controller.abort()
    resolveText(JSON.stringify({ code: 40101, message: '已过期' }))
    await expect(request).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    expect(showError).not.toHaveBeenCalled()
    expect(clearSession).not.toHaveBeenCalled()
  })
})
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
const denied = (reason?: string) => ({ code: 40301, message: '无权操作', data: reason ? { reason } : { required: ['user:manage'] } })

describe('请求的会话和租户边界', () => {
  it('请求发起时捕获租户和会话，切换租户不改变已发请求', async () => {
    const request = setup().get('/resources')
    storage.set('hi_tenant', '2'); storage.set('hi_token', 'session-b')
    await Promise.resolve()
    expect(pending[0].config.headers['X-Tenant-Id']).toBe('1')
    expect(pending[0].config.headers.Authorization).toBe('Bearer session-a')
    pending[0].reply({ code: 0, data: [1] })
    await expect(request).resolves.toEqual([1])
  })
  it('旧会话的401不清除新账号登录', async () => {
    const request = setup().get('/auth/me')
    storage.set('hi_token', 'session-b')
    await Promise.resolve()
    pending[0].reply({ code: 40101, message: '已过期' }, 401)
    await expect(request).rejects.toMatchObject({ code: 40101 })
    expect(storage.get('hi_token')).toBe('session-b')
    expect(clearSession).not.toHaveBeenCalled()
    expect(replace).not.toHaveBeenCalled()
  })
  it('当前会话401清除全局登录状态并保留返回地址', async () => {
    const request = setup().get('/auth/me')
    await Promise.resolve()
    pending[0].reply({ code: 40101, message: '已过期' }, 401)
    await expect(request).rejects.toMatchObject({ code: 40101 })
    expect(clearSession).toHaveBeenCalledOnce()
    expect(replace).toHaveBeenCalledWith({ path: '/login', query: { redirect: '/dcs?view=memory' } })
  })
  it('业务权限403不会清空合法租户或刷新页面', async () => {
    const request = setup().get('/system/users')
    await Promise.resolve(); pending[0].reply(denied(), 403)
    await expect(request).rejects.toMatchObject({ code: 40301 })
    expect(storage.get('hi_tenant')).toBe('1')
    expect(reload).not.toHaveBeenCalled()
  })
  it('旧租户范围失效不会清空新租户', async () => {
    const request = setup().get('/resources')
    storage.set('hi_tenant', '2')
    await Promise.resolve(); pending[0].reply(denied('tenant_scope'), 403)
    await expect(request).rejects.toMatchObject({ code: 40301 })
    expect(storage.get('hi_tenant')).toBe('2')
    expect(reload).not.toHaveBeenCalled()
  })
  it('仅当前租户确实失权时回到全部租户', async () => {
    const request = setup().get('/resources')
    await Promise.resolve(); pending[0].reply(denied('tenant_scope'), 403)
    await expect(request).rejects.toMatchObject({ code: 40301 })
    expect(storage.has('hi_tenant')).toBe(false)
    expect(reload).toHaveBeenCalledOnce()
  })
  it('主动取消不弹网络错误', async () => {
    const request = setup().get('/resources')
    await Promise.resolve(); pending[0].cancel()
    await expect(request).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    expect(showError).not.toHaveBeenCalled()
  })
  it('HTTP200中的业务错误也遵守silent', async () => {
    const request = setup().get('/resources', undefined, { silent: true })
    await Promise.resolve(); pending[0].reply({ code: 40901, message: '处理中' })
    await expect(request).rejects.toMatchObject({ code: 40901 })
    expect(showError).not.toHaveBeenCalled()
  })
  it('Excel导出401也清理会话并保留业务错误码', async () => {
    const request = setup().download('/dcs/export')
    await Promise.resolve(); pending[0].reply(new Blob([JSON.stringify({ code: 40101, message: '登录已失效' })]), 401)
    await expect(request).rejects.toMatchObject({ code: 40101, rawMessage: '登录已失效' })
    expect(clearSession).toHaveBeenCalledOnce()
    expect(anchor.click).not.toHaveBeenCalled()
  })
  it('文件名含百分号但未URL编码时仍能下载', async () => {
    const request = setup().download('/reports/1/export')
    await Promise.resolve(); pending[0].reply(new Blob(['xlsx']), 200, { 'content-disposition': 'attachment; filename="指标100%.xlsx"' })
    await expect(request).resolves.toBe('指标100%.xlsx')
    expect(anchor.download).toBe('指标100%.xlsx')
    expect(anchor.click).toHaveBeenCalledOnce()
  })
})
