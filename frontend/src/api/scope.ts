/** 显式覆盖请求层的全局租户；all 表示用户获授权的全部租户，不是绕过授权。 */
export const scopeHeaders = (tenantId: number | null): Record<string, string> => ({
  'X-Tenant-Id': tenantId == null ? 'all' : String(tenantId),
})
