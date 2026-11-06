import { createApp } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createDiagnostics, diagnosticRoute, installDiagnostics, type ClientErrorPayload } from '../src/utils/diagnostics'
import { newRequestId, validRequestId } from '../src/utils/request-id'

const fetcher = vi.fn<typeof fetch>()
const disposers: (() => void)[] = []
const origin = 'https://example.test'
let token: string | null
let route: string
let target: EventTarget
const options = { accessToken: () => token, route: () => route }
const payload = (index = 0): ClientErrorPayload => JSON.parse(String(fetcher.mock.calls[index][1]?.body))
function error(line = 12, requestId?: string): Error {
  return Object.assign(new TypeError('FAKE_BUSINESS_CONTENT user@example.test'), {
    stack: `TypeError: FAKE_BUSINESS_CONTENT\n    at secretFunction (${origin}/assets/index-A1b2c3d4.js?token=FAKE_QUERY_TOKEN:${line}:9)\nhandler@${origin}/assets/RuleListView-X1y2z3w4.js:51:4`,
    requestId,
  })
}
function reporter() {
  const diagnostics = createDiagnostics(options)
  disposers.push(diagnostics.dispose)
  return diagnostics
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-27T00:00:00Z'))
  token = 'FAKE_LOGIN_CREDENTIAL'
  route = '/rules?keyword=FAKE_SEARCH_CONTENT#FAKE_HASH'
  target = new EventTarget()
  fetcher.mockReset().mockResolvedValue(new Response(null, { status: 204 }))
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('window', target)
  vi.stubGlobal('location', { origin })
  vi.stubGlobal('navigator', { onLine: true })
  vi.stubGlobal('document', { scripts: [{ src: `${origin}/assets/index-A1b2c3d4.js?token=FAKE_SCRIPT_QUERY` }] })
  vi.stubGlobal('performance', { getEntriesByType: () => [
    { name: `${origin}/assets/RuleListView-X1y2z3w4.js`, initiatorType: 'script' },
    { name: `${origin}/api/v1/resources?token=FAKE_API_QUERY`, initiatorType: 'fetch' },
    { name: 'https://external.test/assets/FAKE_EXTERNAL.js', initiatorType: 'script' },
  ] })
})
afterEach(() => {
  disposers.splice(0).forEach((dispose) => dispose())
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('浏览器诊断隐私与发送边界', () => {
  it('只发送固定字段、白名单错误名、已加载脚本的文件行列及有效关联编号', async () => {
    reporter().report('vue', error(12, 'request-id_12345678'))
    await Promise.resolve()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(payload()).toEqual({ kind: 'vue', error_name: 'TypeError', message: 'Vue runtime error', route: '/rules', stack: 'index-A1b2c3d4.js:12:9\nRuleListView-X1y2z3w4.js:51:4', request_id: 'request-id_12345678' })
    const serialized = JSON.stringify(payload())
    for (const value of ['FAKE_', 'secretFunction', 'example.test', 'user@', 'Bearer', 'Authorization']) expect(serialized).not.toContain(value)
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('/api/v1/diagnostics/client-errors')
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error', cache: 'no-store' })
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toBe('Bearer FAKE_LOGIN_CREDENTIAL')
    expect(validRequestId(headers.get('X-Request-Id'))).toBeTruthy()
    expect(new TextEncoder().encode(serialized).length).toBeLessThan(8192)
  })
  it.each([
    ['/m/action/FAKE_MAIL_TOKEN?token=FAKE_QUERY#hash', '/m/action/:token'],
    ['/m/action/FAKE_MAIL_TOKEN/anything', '/m/action/:token'],
    ['/guides?type=FAKE_RESOURCE', '/guides'],
    ['/job-reports/flink?name=FAKE_JOB', '/job-reports/flink'],
    ['/job-reports/FAKE_CUSTOM_NAME', '/'],
    ['/customers/FAKE_CUSTOMER_NAME', '/'],
    ['https://example.test/rules?token=FAKE_TOKEN', '/'],
  ])('路径 %s 不暴露动态片段或查询', (input, expected) => {
    expect(diagnosticRoute(input)).toBe(expected)
  })
  it('邮件操作异常与任意拒绝对象不发送token、错误原文或对象内容', () => {
    route = '/m/action/FAKE_MAIL_TOKEN?token=FAKE_QUERY'
    reporter().report('unhandledrejection', { name: 'FAKE_SECRET_NAME', message: 'FAKE_BUSINESS', stack: 'FAKE_TOKEN.js:12:3', data: { user: 'FAKE_USER' }, requestId: 'bad id' })
    expect(payload()).toEqual({ kind: 'unhandledrejection', error_name: 'UnhandledRejection', message: 'Unhandled promise rejection', route: '/m/action/:token', stack: '' })
    expect(JSON.stringify(payload())).not.toContain('FAKE_')
  })
  it('未知脚本、外站脚本和原始消息中的伪frame全部丢弃', () => {
    const value = Object.assign(new Error('FAKE_SECRET'), { stack: `Error: index-A1b2c3d4.js:99:1\n at save (${origin}/m/action/FAKE_TOKEN.js:1:2)\n at send (https://external.test/assets/FAKE_EXTERNAL.js:3:4)\n at unknown (${origin}/assets/FAKE_PAYLOAD.js:5:6)` })
    reporter().report('error', value)
    expect(payload().stack).toBe('')
  })
  it('任意字符串拒绝和恶意getter不能泄漏原文或中断应用', () => {
    const diagnostics = reporter()
    expect(() => diagnostics.report('unhandledrejection', 'FAKE_RAW_SECRET')).not.toThrow()
    const throwing = Object.defineProperties({}, { name: { get() { throw new Error('FAKE_SECRET') } }, stack: { get() { throw new Error('FAKE_SECRET') } }, requestId: { get() { throw new Error('FAKE_SECRET') } } })
    expect(() => diagnostics.report('vue', throwing)).not.toThrow()
    expect(JSON.stringify(fetcher.mock.calls.map((_, index) => payload(index)))).not.toContain('FAKE_')
  })
  it('未认证或离线时不发送，认证恢复后可正常发送', () => {
    const diagnostics = reporter()
    token = null
    diagnostics.report('vue', error())
    token = 'FAKE_LOGIN_CREDENTIAL'
    vi.stubGlobal('navigator', { onLine: false })
    diagnostics.report('vue', error())
    expect(fetcher).not.toHaveBeenCalled()
    vi.stubGlobal('navigator', { onLine: true })
    diagnostics.report('vue', error())
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it.each([{ name: 'AbortError' }, { name: 'CanceledError' }, { code: 'ERR_CANCELED' }])('主动取消 %o 不上报', (value) => {
    reporter().report('unhandledrejection', value)
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('相同故障即使关联编号不同，一分钟只上报一次', async () => {
    const diagnostics = reporter()
    diagnostics.report('vue', error(12, 'request-id_11111111'))
    diagnostics.report('vue', error(12, 'request-id_22222222'))
    await vi.advanceTimersByTimeAsync(59_999)
    diagnostics.report('vue', error())
    expect(fetcher).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    diagnostics.report('vue', error())
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('滚动一分钟至多10个不同故障，额度到期后恢复', async () => {
    const diagnostics = reporter()
    for (let line = 1; line <= 12; line++) diagnostics.report('error', error(line))
    expect(fetcher).toHaveBeenCalledTimes(10)
    await vi.advanceTimersByTimeAsync(60_000)
    diagnostics.report('error', error(13))
    expect(fetcher).toHaveBeenCalledTimes(11)
  })
  it('失败不重试、不递归；响应体不解析且不会产生二次异常', async () => {
    fetcher.mockRejectedValueOnce(new Error('FAKE_SERVER_SECRET'))
    const diagnostics = reporter()
    diagnostics.report('error', error())
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher).toHaveBeenCalledOnce()
    fetcher.mockResolvedValueOnce(new Response('not-json', { status: 500 }))
    diagnostics.report('error', error(13))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('上报超时终止，dispose终止在途请求且不再发送', async () => {
    fetcher.mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const diagnostics = reporter()
    diagnostics.report('error', error())
    const firstSignal = fetcher.mock.calls[0][1]?.signal
    expect(firstSignal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(firstSignal?.aborted).toBe(true)
    diagnostics.report('error', error(13))
    const secondSignal = fetcher.mock.calls[1][1]?.signal
    diagnostics.dispose()
    expect(secondSignal?.aborted).toBe(true)
    diagnostics.report('error', error(14))
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('脚本元数据不可用时仍发送安全类别，不发送原始stack', () => {
    vi.stubGlobal('document', {})
    reporter().report('error', error())
    expect(payload().stack).toBe('')
  })
  it('无Web Crypto的浏览器仍生成不包含业务数据的合法独立编号', () => {
    vi.stubGlobal('crypto', undefined)
    const first = newRequestId(), second = newRequestId()
    expect(validRequestId(first)).toBe(first)
    expect(validRequestId(second)).toBe(second)
    expect(first).not.toBe(second)
  })
})

describe('Vue与window入口', () => {
  it('没有旧Vue handler且未登录时仍保留安全本地日志，不匿名上报', () => {
    token = null
    route = '/m/action/FAKE_MAIL_TOKEN?token=FAKE_QUERY'
    const app = createApp({})
    disposers.push(installDiagnostics(app, options))
    app.config.errorHandler?.(error(12, 'request-id_12345678'), null, 'render FAKE_COMPONENT_PAYLOAD')
    expect(fetcher).not.toHaveBeenCalled()
    expect(console.error).toHaveBeenCalledExactlyOnceWith('[client-error]', {
      kind: 'vue', error_name: 'TypeError', message: 'Vue runtime error', route: '/m/action/:token',
      stack: 'index-A1b2c3d4.js:12:9\nRuleListView-X1y2z3w4.js:51:4', request_id: 'request-id_12345678',
    })
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('FAKE_')
  })
  it('离线只写本地日志；联网后相同故障可上报，不重复写本地日志', () => {
    vi.stubGlobal('navigator', { onLine: false })
    const accessToken = vi.fn(() => token)
    const diagnostics = createDiagnostics({ ...options, accessToken })
    disposers.push(diagnostics.dispose)
    diagnostics.report('error', error())
    diagnostics.report('error', error())
    expect(console.error).toHaveBeenCalledOnce()
    expect(accessToken).not.toHaveBeenCalled()
    expect(fetcher).not.toHaveBeenCalled()
    vi.stubGlobal('navigator', { onLine: true })
    diagnostics.report('error', error())
    expect(console.error).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('未登录时的本地去重不会吞掉登录后的首次远端上报', () => {
    const diagnostics = reporter()
    token = null
    diagnostics.report('vue', error())
    token = 'FAKE_LOGIN_CREDENTIAL'
    diagnostics.report('vue', error())
    diagnostics.report('vue', error())
    expect(console.error).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('本地与远端分别限流，离线同类故障一分钟只记一次且最多10条', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    const diagnostics = reporter()
    for (let line = 1; line <= 12; line++) {
      diagnostics.report('error', error(line))
      diagnostics.report('error', error(line, 'new-request_12345678'))
    }
    expect(console.error).toHaveBeenCalledTimes(10)
    expect(fetcher).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(60_000)
    diagnostics.report('error', error(1))
    expect(console.error).toHaveBeenCalledTimes(11)
  })
  it('控制台包装器抛错并同步触发window错误时不递归、不阻断上报', () => {
    const app = createApp({})
    disposers.push(installDiagnostics(app, options))
    vi.mocked(console.error).mockImplementation(() => {
      target.dispatchEvent(Object.assign(new Event('error'), { error: error(13), filename: '', lineno: 0, colno: 0 }))
      throw new Error('FAKE_CONSOLE_FAILURE')
    })
    expect(() => app.config.errorHandler?.(error(), null, 'render function')).not.toThrow()
    expect(console.error).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('鉴权状态读取失败也不会吞掉已脱敏的本地记录', () => {
    const diagnostics = createDiagnostics({ ...options, accessToken: () => { throw new Error('FAKE_AUTH_ERROR') } })
    disposers.push(diagnostics.dispose)
    expect(() => diagnostics.report('vue', error())).not.toThrow()
    expect(console.error).toHaveBeenCalledOnce()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('安装去重，捕获三个入口，保留旧Vue handler且不阻止默认事件', () => {
    const app = createApp({})
    const previous = vi.fn()
    app.config.errorHandler = previous
    const dispose = installDiagnostics(app, options)
    disposers.push(dispose)
    expect(installDiagnostics(app, options)).toBe(dispose)
    const value = error()
    app.config.errorHandler?.(value, null, 'render function')
    const rejection = Object.assign(new Event('unhandledrejection', { cancelable: true }), { reason: value })
    target.dispatchEvent(rejection)
    target.dispatchEvent(Object.assign(new Event('error'), { error: value, filename: `${origin}/assets/index-A1b2c3d4.js?token=FAKE_QUERY`, lineno: 12, colno: 9 }))
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect([payload(0).kind, payload(1).kind, payload(2).kind]).toEqual(['vue', 'unhandledrejection', 'error'])
    expect(previous).toHaveBeenCalledExactlyOnceWith(value, null, 'render function')
    expect(rejection.defaultPrevented).toBe(false)
    dispose()
    expect(app.config.errorHandler).toBe(previous)
    target.dispatchEvent(Object.assign(new Event('unhandledrejection'), { reason: error(13) }))
    expect(fetcher).toHaveBeenCalledTimes(3)
  })
  it('没有Error对象的window错误仅从已加载脚本读取行列', () => {
    const app = createApp({})
    disposers.push(installDiagnostics(app, options))
    target.dispatchEvent(Object.assign(new Event('error'), { error: null, message: 'FAKE_RAW_MESSAGE', filename: `${origin}/assets/index-A1b2c3d4.js?token=FAKE_QUERY`, lineno: 21, colno: 3 }))
    expect(payload().stack).toBe('index-A1b2c3d4.js:21:3')
    expect(JSON.stringify(payload())).not.toContain('FAKE_')
  })
  it('重复清理不会移除后续安装的处理器', () => {
    const app = createApp({})
    const first = installDiagnostics(app, options)
    first()
    const second = installDiagnostics(app, options)
    disposers.push(second)
    first()
    expect(installDiagnostics(app, options)).toBe(second)
    app.config.errorHandler?.(error(), null, 'render function')
    expect(fetcher).toHaveBeenCalledOnce()
  })
})

describe('诊断请求的租户范围', () => {
  it.each([1, 27, Number.MAX_SAFE_INTEGER])('仅从getter捕获正整数 %s 到请求头', (tenantId) => {
    const diagnostics = createDiagnostics({ ...options, tenantId: () => tenantId })
    disposers.push(diagnostics.dispose)
    diagnostics.report('vue', error())
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('X-Tenant-Id')).toBe(String(tenantId))
    expect(payload()).toEqual({ kind: 'vue', error_name: 'TypeError', message: 'Vue runtime error', route: '/rules', stack: 'index-A1b2c3d4.js:12:9\nRuleListView-X1y2z3w4.js:51:4' })
  })
  it.each([null, undefined, 0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '27', '1e2', 'all', '27\r\nFAKE_HEADER: value', { id: 27, name: 'FAKE_BUSINESS' }])('非法租户值 %s 不进入header、payload或本地日志', (value) => {
    const diagnostics = createDiagnostics({ ...options, tenantId: () => value as number | null })
    disposers.push(diagnostics.dispose)
    expect(() => diagnostics.report('vue', error())).not.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).has('X-Tenant-Id')).toBe(false)
    expect(payload()).not.toHaveProperty('tenant_id')
    expect(payload()).not.toHaveProperty('tenantId')
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('FAKE_')
  })
  it('切换租户和全部范围时捕获各次发送值，不覆盖在途请求；同一故障按scope去重', () => {
    let selected: number | null = 7
    const tenantId = vi.fn(() => selected)
    const getItem = vi.fn(() => { throw new Error('浏览器存储不应被读取') })
    vi.stubGlobal('localStorage', { getItem })
    fetcher.mockImplementation(() => new Promise(() => {}))
    const diagnostics = createDiagnostics({ ...options, tenantId })
    disposers.push(diagnostics.dispose)
    diagnostics.report('vue', error())
    selected = 8
    diagnostics.report('vue', error())
    selected = null
    diagnostics.report('vue', error())
    selected = 7
    diagnostics.report('vue', error())
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect(fetcher.mock.calls.map(([, init]) => new Headers(init?.headers).get('X-Tenant-Id'))).toEqual(['7', '8', null])
    expect(console.error).toHaveBeenCalledOnce()
    expect(getItem).not.toHaveBeenCalled()
    expect(tenantId).toHaveBeenCalledTimes(4)
    expect(payload(0)).toEqual(payload(1))
    expect(payload(1)).toEqual(payload(2))
  })
  it('未认证和离线时不读取租户getter，getter失败也不阻断本地记录或上报', () => {
    const tenantId = vi.fn((): number | null => { throw new Error('FAKE_TENANT_STATE') })
    const diagnostics = createDiagnostics({ ...options, tenantId })
    disposers.push(diagnostics.dispose)
    token = null
    diagnostics.report('vue', error())
    token = 'FAKE_LOGIN_CREDENTIAL'
    vi.stubGlobal('navigator', { onLine: false })
    diagnostics.report('vue', error())
    expect(tenantId).not.toHaveBeenCalled()
    vi.stubGlobal('navigator', { onLine: true })
    expect(() => diagnostics.report('vue', error())).not.toThrow()
    expect(console.error).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).has('X-Tenant-Id')).toBe(false)
    expect(JSON.stringify(payload())).not.toContain('FAKE_')
  })
  it('跨租户去重仍共享每分钟最多10次的远端额度', () => {
    let selected = 1
    const diagnostics = createDiagnostics({ ...options, tenantId: () => selected })
    disposers.push(diagnostics.dispose)
    for (selected = 1; selected <= 12; selected++) diagnostics.report('vue', error())
    expect(fetcher).toHaveBeenCalledTimes(10)
    expect(console.error).toHaveBeenCalledOnce()
  })
})
