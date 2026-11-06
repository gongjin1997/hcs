import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import * as Pinia from 'pinia'
import ts from 'typescript'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AlarmStats, UserInfo } from '../src/types'
import type { useAppStore as AppFactory } from '../src/stores/app'
import type { useUserStore as UserFactory } from '../src/stores/user'

const storage = new Map<string, string>()
const api = { alarmStats: vi.fn(), login: vi.fn(), fetchMe: vi.fn(), logout: vi.fn() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
const stats = (n: number): AlarmStats => ({ FIRING: n, ACKED: 0, SUPPRESSED: 0, P1: n, P2: 0, P3: 0, P4: 0 })
const person = (ids = [1, 2]): UserInfo => ({
  id: 1, username: 'reviewer', display_name: '检查账号', role_id: 1, role_code: 'viewer', role_name: '只读',
  permissions: ['alarm:view'], tenant_ids: ids, is_admin: false,
  tenants: ids.map(id => ({ id, code: `tenant-${id}`, name: `租户${id}`, short_name: `租户${id}`, color: '#000', auth_type: 'AKSK', enabled: true })),
})
function setup() {
  const cache: Record<string, Record<string, unknown>> = {}
  const load = (name: string): Record<string, unknown> => {
    if (name === 'pinia') return Pinia
    if (name === '@/api') return api
    if (name === '@/api/request') return { TENANT_KEY: 'hi_tenant', TOKEN_KEY: 'hi_token' }
    const file = name === './user' || name === '@/stores/user' ? 'user' : name === './app' || name === '@/stores/app' ? 'app' : ''
    if (!file) throw new Error(`Unexpected dependency ${name}`)
    if (cache[file]) return cache[file]
    const output: Record<string, unknown> = cache[file] = {}
    const source = readFileSync(resolve(import.meta.dirname, `../src/stores/${file}.ts`), 'utf8')
    const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
    new Function('require', 'exports', outputText)(load, output)
    return output
  }
  const app = (load('./app').useAppStore as typeof AppFactory)()
  const user = (load('./user').useUserStore as typeof UserFactory)()
  user.token = 'initial-session'
  user.user = person()
  storage.set('hi_token', user.token)
  return { app, user }
}
beforeEach(() => {
  Pinia.setActivePinia(Pinia.createPinia())
  storage.clear()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  })
  vi.resetAllMocks()
  api.alarmStats.mockResolvedValue(stats(0))
})
afterEach(() => { vi.unstubAllGlobals() })

describe('全局统计与登录范围', () => {
  it('旧租户统计晚返回时不覆盖新租户', async () => {
    const { app } = setup(), old = deferred<AlarmStats>(), next = deferred<AlarmStats>()
    api.alarmStats.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const pending = app.refreshStats()
    app.setTenant(2)
    next.resolve(stats(2))
    await Promise.resolve(); await Promise.resolve()
    old.resolve(stats(99))
    await pending
    expect(app.stats?.FIRING).toBe(2)
  })
  it('切换租户立即清空旧计数', () => {
    const { app } = setup()
    app.stats = stats(8)
    app.setTenant(2)
    expect(app.stats).toBeNull()
  })
  it('退出后清空租户、实时状态和计数，忽略仍在途的响应', async () => {
    const { app, user } = setup(), pending = deferred<AlarmStats>()
    app.tenantId = 2
    storage.set('hi_tenant', '2')
    app.stats = stats(4)
    app.streamConnected = true
    api.alarmStats.mockReturnValueOnce(pending.promise)
    const loading = app.refreshStats()
    user.clearSession()
    pending.resolve(stats(99))
    await loading
    expect(app.tenantId).toBeNull()
    expect(app.stats).toBeNull()
    expect(app.streamConnected).toBe(false)
    expect(storage.has('hi_tenant')).toBe(false)
  })
  it('新账号登录后校验已有租户范围', async () => {
    const { app, user } = setup()
    app.tenantId = 2
    storage.set('hi_tenant', '2')
    api.login.mockResolvedValue({ access_token: 'new-session', user: person([1]) })
    await user.login('reader', 'example-password')
    expect(app.tenantId).toBeNull()
    expect(storage.has('hi_tenant')).toBe(false)
    expect(user.user?.tenant_ids).toEqual([1])
  })
  it('同一租户并发刷新也只接受最后一次请求', async () => {
    const { app } = setup(), old = deferred<AlarmStats>(), next = deferred<AlarmStats>()
    api.alarmStats.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const a = app.refreshStats(), b = app.refreshStats()
    next.resolve(stats(3)); await b
    old.resolve(stats(10)); await a
    expect(app.stats?.FIRING).toBe(3)
  })
  it('退出后在途的用户信息不能重新填回旧账号', async () => {
    const { user } = setup(), pending = deferred<UserInfo>()
    api.fetchMe.mockReturnValueOnce(pending.promise)
    const loading = user.fetchMe()
    user.clearSession()
    pending.resolve(person([2]))
    await loading
    expect(user.user).toBeNull()
  })
  it('更新授权后撤销的租户选择同步清除', async () => {
    const { app, user } = setup()
    app.tenantId = 2
    storage.set('hi_tenant', '2')
    api.fetchMe.mockResolvedValueOnce(person([1]))
    await user.fetchMe()
    expect(app.tenantId).toBeNull()
    expect(storage.has('hi_tenant')).toBe(false)
  })
  it('并发拉取身份时较早响应不能恢复旧授权', async () => {
    const { user } = setup(), old = deferred<UserInfo>(), latest = deferred<UserInfo>()
    api.fetchMe.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = user.fetchMe(), b = user.fetchMe()
    latest.resolve(person([1])); await b
    old.resolve(person([1, 2])); await a
    expect(user.user?.tenant_ids).toEqual([1])
  })
  it('登录请求返回前已退出时不会恢复会话', async () => {
    const { user } = setup(), pending = deferred<{ access_token: string; user: UserInfo }>()
    api.login.mockReturnValueOnce(pending.promise)
    const loading = user.login('reader', 'example-password')
    user.clearSession()
    pending.resolve({ access_token: 'late-session', user: person() })
    await loading
    expect(user.token).toBe('')
    expect(user.user).toBeNull()
    expect(storage.has('hi_token')).toBe(false)
  })
  it('自动刷新失败可恢复，不留下未处理的 Promise', async () => {
    const { app } = setup()
    api.alarmStats.mockRejectedValueOnce(new Error('offline'))
    await expect(app.refreshStats()).resolves.toBeUndefined()
    api.alarmStats.mockResolvedValueOnce(stats(1))
    await app.refreshStats()
    expect(app.stats?.FIRING).toBe(1)
  })
})
