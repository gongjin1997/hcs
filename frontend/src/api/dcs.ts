import { download, get } from './request'
import type { DcsHistoryExporter, DcsHistoryFetcher, DcsInstanceFetcher, DcsInstancesFetcher } from '../types/dcs'
import { scopeHeaders } from './scope'

export const getDcsInstance: DcsInstanceFetcher = (id, { signal, tenantId }) =>
  get(`/dcs/instances/${id}`, undefined, { signal, silent: true, headers: scopeHeaders(tenantId) })

export const listDcsInstances: DcsInstancesFetcher = (query, { signal, tenantId, projectRef }) =>
  get('/dcs/instances', {
    ...query, keyword: query.keyword?.trim() || undefined,
    tenant_id: tenantId ?? undefined, project_ref: projectRef ?? query.project_ref,
  }, { signal, silent: true, headers: scopeHeaders(tenantId) })

export const getDcsHistory: DcsHistoryFetcher = (query, { signal, tenantId }) =>
  get('/dcs/history', { ...query, resource_ids: [...query.resource_ids] }, {
    timeout: 120000, signal, silent: true, headers: scopeHeaders(tenantId),
  })

export const exportDcsHistory: DcsHistoryExporter = (query, { signal, tenantId }) =>
  download('/dcs/export', { ...query, resource_ids: [...query.resource_ids] }, 'DCS内存指标.xlsx', {
    timeout: 120000, signal, silent: true, headers: scopeHeaders(tenantId),
  })


export const getDcsMetricsHistory: DcsHistoryFetcher = (query, { signal, tenantId }) =>
  get('/dcs/metrics/history', { ...query, resource_ids: [...query.resource_ids] }, {
    timeout: 120000, signal, silent: true, headers: scopeHeaders(tenantId),
  })

export const exportDcsMetricsHistory: DcsHistoryExporter = (query, { signal, tenantId }) =>
  download('/dcs/metrics/export', { ...query, resource_ids: [...query.resource_ids] }, 'DCS原生指标.xlsx', {
    timeout: 120000, signal, silent: true, headers: scopeHeaders(tenantId),
  })
