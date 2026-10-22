import axios, { type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import type { ApiEnvelope } from '@/types'

export const TOKEN_KEY = 'hi_token'
/** 顶部“租户切换”的当前租户（空 = 全部租户）；通过请求头 X-Tenant-Id 传给后端，后端再与用户的租户范围取交集 */
export const TENANT_KEY = 'hi_tenant'
export const currentTenantHeader = (): Record<string, string> => {
  const t = localStorage.getItem(TENANT_KEY)
  return t ? { 'X-Tenant-Id': t } : {}
}

export class ApiError extends Error {
  constructor(public code: number, message: string, public data?: unknown) { super(message) }
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  paramsSerializer: { indexes: null }, // 数组参数序列化为 severity=P1&severity=P2（FastAPI 约定）
})

http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  const tenant = localStorage.getItem(TENANT_KEY)
  if (tenant && !cfg.headers['X-Tenant-Id']) cfg.headers['X-Tenant-Id'] = tenant
  return cfg
})

http.interceptors.response.use(
  (resp) => {
    const body = resp.data as ApiEnvelope<unknown>
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) return body.data as any
      ElMessage.error(body.message)
      return Promise.reject(new ApiError(body.code, body.message, body.data))
    }
    return resp.data
  },
  async (err) => {
    const status: number | undefined = err.response?.status
    const body = err.response?.data as Partial<ApiEnvelope<unknown>> | undefined
    const message = body?.message || (status ? `请求失败（HTTP ${status}）` : '网络异常，请稍后重试')
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (!location.pathname.startsWith('/login') && !location.pathname.startsWith('/m/')) {
        const { default: router } = await import('@/router') // 延迟导入，避免循环依赖
        router.replace({ path: '/login', query: { redirect: location.pathname + location.search } })
      }
    }
    // 顶部选中的租户已不在用户的租户范围内（如管理员调整了授权）：回到“全部租户”并刷新
    if (status === 403 && body?.code === 40301 && localStorage.getItem(TENANT_KEY)) {
      localStorage.removeItem(TENANT_KEY)
      location.reload()
    }
    if (!(err.config as RequestConfig | undefined)?.silent && status !== 401) ElMessage.error(message)
    return Promise.reject(new ApiError(body?.code ?? status ?? -1, message, body?.data))
  },
)

export interface RequestConfig extends AxiosRequestConfig { silent?: boolean }

export const get = <T>(url: string, params?: object, cfg?: RequestConfig) => http.get<unknown, T>(url, { params, ...cfg })
export const post = <T>(url: string, data?: object, cfg?: RequestConfig) => http.post<unknown, T>(url, data, cfg)
export const put = <T>(url: string, data?: object, cfg?: RequestConfig) => http.put<unknown, T>(url, data, cfg)
export const patch = <T>(url: string, data?: object, cfg?: RequestConfig) => http.patch<unknown, T>(url, data, cfg)
export const del = <T>(url: string, cfg?: RequestConfig) => http.delete<unknown, T>(url, cfg)

/** 下载文件（Excel 导出等）：带上 token 请求二进制，文件名取 Content-Disposition（RFC 5987 filename*） */
export async function download(url: string, params?: object, fallbackName = 'export.xlsx') {
  const token = localStorage.getItem(TOKEN_KEY)
  const resp = await axios.get<Blob>(`/api/v1${url}`, {
    params, responseType: 'blob', paramsSerializer: { indexes: null },
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...currentTenantHeader() },
  }).catch(async (err) => {
    const text = err.response?.data instanceof Blob ? await err.response.data.text() : ''
    let message = '导出失败'
    try { message = JSON.parse(text).message || message } catch { /* 非 JSON 错误体 */ }
    ElMessage.error(message)
    throw new ApiError(err.response?.status ?? -1, message)
  })
  const cd = String(resp.headers['content-disposition'] || '')
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd)
  const name = m ? decodeURIComponent(m[1]) : fallbackName
  const a = document.createElement('a')
  a.href = URL.createObjectURL(resp.data)
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  return name
}
