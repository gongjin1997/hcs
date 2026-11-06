import { download, get, post, put } from './request'
import { scopeHeaders } from './scope'
import type { EcsHistoryExporter, EcsHistoryFetcher, EcsInstancesFetcher, EcsPolicyFetcher, EcsPolicySaver, EcsQuotasOut, EcsQuotasQuery, EcsProjectQuota, EcsRequestContext } from '../types/ecs'

export const listEcsInstances: EcsInstancesFetcher = (query, { signal, tenantId }) =>
  get('/ecs/instances', { ...query, keyword: query.keyword?.trim() || undefined, tenant_id: tenantId ?? undefined },
    { signal, silent: true, headers: scopeHeaders(tenantId) })

export const getEcsHistory: EcsHistoryFetcher = (id, query, { signal, tenantId }) =>
  get(`/ecs/instances/${id}/history`, { ...query, metrics: query.metrics ? [...query.metrics] : undefined },
    { signal, silent: true, headers: scopeHeaders(tenantId), timeout: 120000 })

export const exportEcsHistory: EcsHistoryExporter = (id, query, { signal, tenantId }) =>
  download(`/ecs/instances/${id}/export`, { ...query, metrics: query.metrics ? [...query.metrics] : undefined }, 'ECS指标.xlsx',
    { signal, silent: true, headers: scopeHeaders(tenantId), timeout: 120000 })

export const getEcsPolicy: EcsPolicyFetcher = (tenantId, { signal }) =>
  get(`/system/collection-policies/${tenantId}/ecs_server`, undefined,
    { signal, silent: true, headers: scopeHeaders(tenantId) })

export const saveEcsPolicy: EcsPolicySaver = (tenantId, policy, { signal }) =>
  put(`/system/collection-policies/${tenantId}/ecs_server`, policy,
    { signal, silent: true, headers: scopeHeaders(tenantId) })

export const listEcsQuotas = (query: EcsQuotasQuery, { signal, tenantId }: EcsRequestContext) =>
  get<EcsQuotasOut>('/ecs/quotas', { ...query, tenant_id: tenantId ?? undefined },
    { signal, silent: true, headers: scopeHeaders(tenantId) })

export const refreshEcsQuota = (projectRef: number, { signal, tenantId }: EcsRequestContext) =>
  post<EcsProjectQuota>(`/ecs/quotas/${projectRef}/refresh`, undefined,
    { params: { tenant_id: tenantId ?? undefined }, signal, silent: true, headers: scopeHeaders(tenantId), timeout: 120000 })
