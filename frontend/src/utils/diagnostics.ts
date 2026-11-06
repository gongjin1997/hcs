import type { App } from 'vue'
import { newRequestId, validRequestId } from './request-id'

export type ClientErrorKind = 'vue' | 'error' | 'unhandledrejection'
export interface ClientErrorPayload {
  kind: ClientErrorKind
  error_name: string
  message: string
  route: string
  stack: string
  request_id?: string
}

interface DiagnosticsOptions {
  /** 仅在身份已确认时返回当前凭据；凭据只用于同源鉴权头。 */
  accessToken: () => string | null
  route: () => string
  /** 来自 app store 的当前选择；不读取浏览器存储，也不写入日志正文。 */
  tenantId?: () => number | null
}

interface ScriptLocation { filename: string; line: number; column: number }

const MESSAGES: Record<ClientErrorKind, string> = {
  vue: 'Vue runtime error',
  error: 'Window runtime error',
  unhandledrejection: 'Unhandled promise rejection',
}
const ERROR_NAMES = new Set(['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'ChunkLoadError', 'NetworkError', 'URIError', 'EvalError'])
const ROUTE = /^\/(?:dashboard|bigscreen|alarms|ledger|pod-events|reports|rules|silences|notify|system|dcs|guides|login|403|resources|job-reports(?:\/(?:dgc|flink|realtime|batch|mrs))?)$/
const MINUTE = 60_000
const FRAME = /(?:^|[/\\\s(])([A-Za-z0-9_.-]+\.(?:js|ts|vue))(?:\?[^\s()]*)?:(\d{1,7}):(\d{1,7})\)?$/

export function diagnosticRoute(path: string): string {
  const route = path.split(/[?#]/, 1)[0]
  if (/^\/m\/action\//.test(route)) return '/m/action/:token'
  return ROUTE.test(route) ? route : '/'
}

function field(error: unknown, key: string): unknown {
  try { return error && typeof error === 'object' ? (error as Record<string, unknown>)[key] : undefined } catch { return undefined }
}

/** 仅信任已加载的本站脚本名称，不能把任意 Error.stack 中的名称当作技术元数据。 */
function loadedScriptFiles(): Set<string> {
  const names = new Set<string>()
  const add = (value: string) => {
    try {
      const url = new URL(value, location.origin)
      if (url.origin !== location.origin || !/^\/(?:assets|src)\//.test(url.pathname)) return
      const name = url.pathname.split('/').pop() ?? ''
      if (/^[A-Za-z0-9_.-]{1,120}\.(?:js|ts|vue)$/.test(name)) names.add(name)
    } catch { /* 缺少浏览器元数据时不发送文件名。 */ }
  }
  try {
    for (const script of Array.from(document.scripts)) add(script.src)
    for (const entry of performance.getEntriesByType('resource').slice(-128)) {
      const resource = entry as PerformanceResourceTiming
      if (resource.initiatorType === 'script' || resource.initiatorType === 'link') add(resource.name)
    }
  } catch { /* 浏览器可能限制 Performance API。 */ }
  return names
}

function safeStack(error: unknown, scripts: Set<string>, position?: ScriptLocation): string {
  const frames: string[] = []
  const stack = field(error, 'stack')
  if (typeof stack === 'string') {
    for (const line of stack.slice(0, 8192).split('\n').slice(0, 30)) {
      if (!/^\s*at\s|@(?:https?:\/\/|\/)/.test(line)) continue
      const frame = FRAME.exec(line.trim())
      if (frame && scripts.has(frame[1])) frames.push(`${frame[1]}:${frame[2]}:${frame[3]}`)
      if (frames.length === 5) break
    }
  }
  if (!frames.length && position) {
    try {
      const name = new URL(position.filename, location.origin).pathname.split('/').pop() ?? ''
      if (scripts.has(name) && Number.isInteger(position.line) && position.line > 0 && position.line <= 9_999_999
        && Number.isInteger(position.column) && position.column > 0 && position.column <= 9_999_999) {
        frames.push(`${name}:${position.line}:${position.column}`)
      }
    } catch { /* 跨域 Script error 不带可用位置。 */ }
  }
  return frames.join('\n')
}

export function createDiagnostics(options: DiagnosticsOptions) {
  const local = new Map<string, number>()
  const sent = new Map<string, number>()
  const pending = new Set<AbortController>()
  let disposed = false
  let reporting = false
  let scripts = new Set<string>()
  let scriptsAt = -Infinity

  async function send(payload: ClientErrorPayload, token: string, tenantId?: string): Promise<void> {
    const controller = new AbortController()
    pending.add(controller)
    const timeout = setTimeout(() => controller.abort(), 5000)
    try {
      // 独立于 axios：失败不重试，不触发业务拦截器、提示或新的错误上报。
      await fetch('/api/v1/diagnostics/client-errors', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json', Authorization: `Bearer ${token}`, 'X-Request-Id': newRequestId(),
          ...(tenantId ? { 'X-Tenant-Id': tenantId } : {}),
        },
        body: JSON.stringify(payload),
        credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error', cache: 'no-store',
        signal: controller.signal,
      })
    } catch { /* 离线、过期或上报接口失败均静默结束。 */ }
    finally { clearTimeout(timeout); pending.delete(controller) }
  }

  function report(kind: ClientErrorKind, error: unknown, position?: ScriptLocation): void {
    if (disposed || reporting) return
    reporting = true
    try {
      const name = field(error, 'name')
      if (name === 'AbortError' || name === 'CanceledError' || field(error, 'code') === 'ERR_CANCELED') return
      const now = Date.now()
      for (const records of [local, sent]) {
        for (const [key, time] of records) if (now - time >= MINUTE) records.delete(key)
      }
      if (local.size >= 10 && sent.size >= 10) return
      if (now - scriptsAt >= 1000) { scripts = loadedScriptFiles(); scriptsAt = now }
      const payload: ClientErrorPayload = {
        kind,
        error_name: typeof name === 'string' && ERROR_NAMES.has(name) ? name : kind === 'vue' ? 'VueError' : kind === 'unhandledrejection' ? 'UnhandledRejection' : 'Error',
        message: MESSAGES[kind],
        route: diagnosticRoute(options.route()),
        stack: safeStack(error, scripts, position),
      }
      const requestId = validRequestId(field(error, 'requestId'))
      if (requestId) payload.request_id = requestId
      // 请求编号不参与去重，同一故障不能借新编号绕过一分钟一次的限制。
      const fingerprint = JSON.stringify([payload.kind, payload.error_name, payload.route, payload.stack])
      if (local.size < 10 && !local.has(fingerprint)) {
        local.set(fingerprint, now)
        // Vue 的全局 handler 替代默认控制台输出；未登录、离线仍须可本地定位。
        // 不传原始 Error；即使控制台被扩展包装后抛错，也不能递归或阻止远端上报。
        try { console.error('[client-error]', payload) } catch { /* 本地日志失败不影响应用。 */ }
      }
      if (typeof navigator !== 'undefined' && navigator.onLine === false) return
      const token = options.accessToken()
      if (!token || sent.size >= 10) return
      let tenantId: string | undefined
      try {
        const selected = options.tenantId?.()
        if (typeof selected === 'number' && Number.isSafeInteger(selected) && selected > 0) tenantId = String(selected)
      } catch { /* 租户状态不可读时仍保留故障记录，由后端按用户范围校验。 */ }
      // 捕获后作为值传递，后续切租户不能改变在途上报；跨租户故障分别去重。
      const scopedFingerprint = JSON.stringify([fingerprint, tenantId ?? null])
      if (sent.has(scopedFingerprint)) return
      sent.set(scopedFingerprint, now)
      void send(payload, token, tenantId)
    } catch { /* 诊断不能影响应用的错误处理。 */ }
    finally { reporting = false }
  }

  function dispose(): void {
    disposed = true
    for (const controller of pending) controller.abort()
    pending.clear()
    local.clear()
    sent.clear()
  }

  return { report, dispose }
}

const installed = new WeakMap<App, () => void>()

export function installDiagnostics(app: App, options: DiagnosticsOptions): () => void {
  const existing = installed.get(app)
  if (existing) return existing
  const diagnostics = createDiagnostics(options)
  const previous = app.config.errorHandler
  const vueHandler: NonNullable<App['config']['errorHandler']> = (error, instance, info) => {
    diagnostics.report('vue', error)
    previous?.(error, instance, info)
  }
  const onError = (event: ErrorEvent) => diagnostics.report('error', event.error, { filename: event.filename, line: event.lineno, column: event.colno })
  const onRejection = (event: PromiseRejectionEvent) => diagnostics.report('unhandledrejection', event.reason)
  app.config.errorHandler = vueHandler
  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  let stopped = false
  const dispose = () => {
    if (stopped) return
    stopped = true
    diagnostics.dispose()
    window.removeEventListener('error', onError)
    window.removeEventListener('unhandledrejection', onRejection)
    if (app.config.errorHandler === vueHandler) app.config.errorHandler = previous
    installed.delete(app)
  }
  installed.set(app, dispose)
  app.onUnmount(dispose)
  return dispose
}
