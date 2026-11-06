import { download, get } from './request'
import { scopeHeaders } from './scope'
import type { GaussApi, GaussDirectoryApi, GaussDiskApi, GaussRequestContext } from '../types/gaussdb'

const config = ({ signal, tenantId }: GaussRequestContext) => ({
  signal, silent: true, timeout: 120000,
  headers: scopeHeaders(tenantId),
})
const path = (id: number) => `/gaussdb/instances/${encodeURIComponent(String(id))}`

export const gaussDirectoryApi: GaussDirectoryApi = {
  instance: (id, context) => get(path(id), undefined, config(context)),
  projects: context => get('/gaussdb/projects', undefined, config(context)),
}

export const listGaussInstances: GaussApi['instances'] = (query, context) =>
  get('/gaussdb/instances', { ...query, keyword: query.keyword?.trim() || undefined }, config(context))
export const getGaussSummary: GaussApi['summary'] = (query, context) => get('/gaussdb/summary', { ...query }, config(context))
export const getGaussOverview: GaussApi['overview'] = (id, context) => get(`${path(id)}/overview`, undefined, config(context))
export const listGaussSlowNodes: GaussApi['slowNodes'] = (id, context) => get(`${path(id)}/slow-nodes`, undefined, config(context))
export const getGaussSlowSql: GaussApi['slowSql'] = (id, query, context) =>
  get(`${path(id)}/slow-sql`, { ...query, node_ids: [...query.node_ids] }, config(context))
export const getGaussSlowSqlDetail: GaussApi['slowDetail'] = (id, query, context) =>
  get(`${path(id)}/slow-sql/detail`, { ...query, node_ids: [...query.node_ids] }, config(context))
export const getGaussSessions: GaussApi['sessions'] = (id, query, context) => get(`${path(id)}/sessions`, { ...query }, config(context))
export const getGaussInspections: GaussApi['inspections'] = (id, query, context) => get(`${path(id)}/inspections`, { ...query }, config(context))
export const getGaussInspectionDetail: GaussApi['inspectionDetail'] = (id, recordId, context) =>
  get(`${path(id)}/inspections/${encodeURIComponent(recordId)}`, undefined, config(context))
export const exportGaussPanel: GaussApi['export'] = (id, query, context) => download(`${path(id)}/export`,
  { ...query, ...(query.node_ids ? { node_ids: [...query.node_ids] } : {}) }, 'GaussDB诊断.xlsx', config(context))

export const gaussApi: GaussApi = {
  instances: listGaussInstances, summary: getGaussSummary, overview: getGaussOverview, slowNodes: listGaussSlowNodes,
  slowSql: getGaussSlowSql, slowDetail: getGaussSlowSqlDetail, sessions: getGaussSessions,
  inspections: getGaussInspections, inspectionDetail: getGaussInspectionDetail, export: exportGaussPanel,
}

export const gaussDiskApi: GaussDiskApi = {
  history: (id, query, context) => get(`${path(id)}/disk-history`, { ...query }, config(context)),
  export: (id, query, context) => download(`${path(id)}/export`, { panel: 'disk-history', ...query }, 'GaussDB存储趋势.xlsx', config(context)),
}
