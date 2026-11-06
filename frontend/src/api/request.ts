import axios, { type AxiosError, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import type { ApiEnvelope } from '@/types'
import { headerRequestId, newRequestId, validRequestId } from '@/utils/request-id'

export const TOKEN_KEY = 'hi_token'
/** 顶部“租户切换”的当前租户（空 = 全部租户）；通过请求头 X-Tenant-Id 传给后端，后端再与用户的租户范围取交集 */
export const TENANT_KEY = 'hi_tenant'
export const currentTenantHeader = (): Record<string, string> => {
  const t = localStorage.getItem(TENANT_KEY)
  return t ? { 'X-Tenant-Id': t } : {}
}

export class ApiError extends Error {
  public readonly requestId: string | undefined
  public readonly rawMessage: string

  constructor(public code: number, message: string, public data?: unknown, requestId?: string) {
    const id = validRequestId(requestId)
    super(id ? `${message}（请求 ID：${id}）` : message)
    this.rawMessage = message
    this.requestId = id
  }
}

function errorRequestId(headers: unknown, data: unknown, cfg?: AxiosRequestConfig): string | undefined {
  const bodyId = data && typeof data === 'object' ? (data as { request_id?: unknown }).request_id : undefined
  return headerRequestId(headers) ?? validRequestId(bodyId) ?? headerRequestId(cfg?.headers)
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  paramsSerializer: { indexes: null }, // 数组参数序列化为 severity=P1&severity=P2（FastAPI 约定）
})

http.interceptors.request.use((cfg) => {
  cfg.headers.set('X-Request-Id', headerRequestId(cfg.headers) ?? newRequestId(), true)
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  const tenant = localStorage.getItem(TENANT_KEY)
  if (tenant && !cfg.headers['X-Tenant-Id']) cfg.headers['X-Tenant-Id'] = tenant
  return cfg
}, undefined, { synchronous: true })

http.interceptors.response.use(
  (resp) => {
    const body = resp.data as ApiEnvelope<unknown>
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) return body.data
      const error = new ApiError(body.code, body.message, body.data, errorRequestId(resp.headers, body.data, resp.config))
      if (!(resp.config as RequestConfig).silent) ElMessage.error(error.message)
      return Promise.reject(error)
    }
    return resp.data
  },
  handleRequestError,
)

export interface RequestConfig extends AxiosRequestConfig { silent?: boolean }

async function handleRequestError(error: unknown): Promise<never> {
  if (axios.isCancel(error)) throw error
  const err = error as AxiosError<Partial<ApiEnvelope<unknown>> | Blob>
  const cfg = err.config as RequestConfig | undefined
  if (cfg?.signal?.aborted) throw error
  const status = err.response?.status
  let body: Partial<ApiEnvelope<unknown>> | undefined
  const data = err.response?.data
  if (data instanceof Blob) {
    try { body = JSON.parse(await data.text()) } catch { /* 非 JSON 错误体。 */ }
  } else body = data
  if (cfg?.signal?.aborted) throw new axios.CanceledError()
  const message = body?.message || (status ? `请求失败（HTTP ${status}）` : '网络异常，请稍后重试')
  const token = localStorage.getItem(TOKEN_KEY)
  const sameSession = !!token && cfg?.headers?.Authorization === `Bearer ${token}`
  if (status === 401 && sameSession) {
    const { useUserStore } = await import('@/stores/user')
    // 等待模块加载期间可能已重新登录，旧会话错误不能退出新账号。
    if (localStorage.getItem(TOKEN_KEY) === token) {
      useUserStore().clearSession()
      if (!location.pathname.startsWith('/login') && !location.pathname.startsWith('/m/')) {
        const { default: router } = await import('@/router')
        if (!localStorage.getItem(TOKEN_KEY)) await router.replace({ path: '/login', query: { redirect: location.pathname + location.search } })
      }
    }
  }
  const reason = (body?.data as { reason?: string } | undefined)?.reason
  const tenant = localStorage.getItem(TENANT_KEY)
  if (status === 403 && reason === 'tenant_scope' && sameSession && tenant && cfg?.headers?.['X-Tenant-Id'] === tenant) {
    localStorage.removeItem(TENANT_KEY)
    location.reload()
  }
  const failure = new ApiError(body?.code ?? status ?? -1, message, body?.data, errorRequestId(err.response?.headers, body?.data, cfg))
  if (!cfg?.silent && status !== 401) ElMessage.error(failure.message)
  throw failure
}

export const get = <T>(url: string, params?: object, cfg?: RequestConfig) => http.get<unknown, T>(url, { params, ...cfg })
export const post = <T>(url: string, data?: object, cfg?: RequestConfig) => http.post<unknown, T>(url, data, cfg)
export const put = <T>(url: string, data?: object, cfg?: RequestConfig) => http.put<unknown, T>(url, data, cfg)
export const patch = <T>(url: string, data?: object, cfg?: RequestConfig) => http.patch<unknown, T>(url, data, cfg)
export const del = <T>(url: string, cfg?: RequestConfig) => http.delete<unknown, T>(url, cfg)

/** 下载文件（Excel 导出等）：带上 token 请求二进制，文件名取 Content-Disposition（RFC 5987 filename*） */
export async function download(url: string, params?: object, fallbackName = 'export.xlsx', cfg: RequestConfig = {}) {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = new axios.AxiosHeaders({ ...(token ? { Authorization: `Bearer ${token}` } : {}), ...currentTenantHeader(), ...cfg.headers })
  headers.set('X-Request-Id', headerRequestId(headers) ?? newRequestId(), true)
  const resp = await axios.get<Blob>(`/api/v1${url}`, {
    ...cfg, params, responseType: 'blob', paramsSerializer: { indexes: null },
    headers,
  }).catch(handleRequestError)
  if (cfg.signal?.aborted) throw new axios.CanceledError('下载已取消')
  const cd = String(resp.headers['content-disposition'] || '')
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd)
  let name = fallbackName
  if (m) { try { name = decodeURIComponent(m[1]) } catch { name = m[1] } }
  const a = document.createElement('a')
  a.href = URL.createObjectURL(resp.data)
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  return name
}
