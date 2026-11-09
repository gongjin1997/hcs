import { get, put } from './request'
import { scopeHeaders } from './scope'
import type { ManageOneFetcher, ManageOneSaver } from '../types/manageone'

function path(tenantId: number) {
  if (!Number.isSafeInteger(tenantId) || tenantId <= 0) throw new Error('ManageOne 配置需要具体租户')
  return `/system/tenants/${tenantId}/manageone`
}

export const getManageOneConfig: ManageOneFetcher = (tenantId, { signal }) =>
  get(path(tenantId), undefined, { signal, silent: true, headers: scopeHeaders(tenantId) })

export const saveManageOneConfig: ManageOneSaver = async (tenantId, body, { signal }) => {
  await put<void>(path(tenantId), {
    enabled: body.enabled, endpoint: body.endpoint, auth_type: body.auth_type,
    ...(body.auth_type === 'AKSK' ? {
      ...(body.access_key ? { access_key: body.access_key } : {}),
      ...(body.secret_key ? { secret_key: body.secret_key } : {}),
      clear_secret_key: body.clear_secret_key ?? false,
    } : {
      username: body.username ?? '', organization: body.organization ?? '',
      ...(body.password ? { password: body.password } : {}),
      clear_password: body.clear_password ?? false,
    }),
    select_source: body.select_source ?? false,
  }, { signal, silent: true, headers: scopeHeaders(tenantId) })
}
