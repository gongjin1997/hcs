const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$/

export function validRequestId(value: unknown): string | undefined {
  return typeof value === 'string' && REQUEST_ID.test(value) ? value : undefined
}

let sequence = 0

export function newRequestId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  if (globalThis.crypto?.getRandomValues) {
    return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, '0')).join('')
  }
  // 非安全上下文的旧浏览器也能关联请求；不使用租户、路径或账号生成编号。
  return `web-${Date.now().toString(36)}-${(++sequence).toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/** 同时支持 AxiosHeaders、Headers 和普通对象，始终按大小写不敏感读取。 */
export function headerRequestId(headers: unknown): string | undefined {
  if (!headers || typeof headers !== 'object') return undefined
  const record = headers as Record<string, unknown>
  if (typeof record.get === 'function') {
    return validRequestId(record.get.call(headers, 'X-Request-Id'))
  }
  const key = Object.keys(record).find((name) => name.toLowerCase() === 'x-request-id')
  return key ? validRequestId(record[key]) : undefined
}
