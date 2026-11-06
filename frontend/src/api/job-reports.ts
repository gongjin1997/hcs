import { download, get } from './request'
import { scopeHeaders } from './scope'
import type { JobReport, JobReportExporter, JobReportFetcher, JobReportQuery } from '@/types/job-reports'

const reportFilters = (query: JobReportQuery, tenantId: number | null) => ({
  keyword: query.keyword?.trim() || undefined,
  status: query.status?.length ? [...query.status] : undefined,
  status_raw: query.status_raw?.length ? [...query.status_raw] : undefined,
  engine: query.engine?.trim() || undefined,
  resource_type: query.resource_type,
  job_type: query.job_type,
  project_ref: query.project_ref,
  lifecycle: query.lifecycle,
  has_alarms: query.has_alarms,
  tenant_id: tenantId ?? undefined,
})

export const fetchJobReport: JobReportFetcher = (panel, query, { signal, tenantId }) =>
  get<JobReport>(`/job-reports/${panel}`, {
    ...reportFilters(query, tenantId),
    page: query.page,
    page_size: query.page_size,
  }, {
    signal,
    silent: true,
    headers: scopeHeaders(tenantId),
  })

export const exportJobReport: JobReportExporter = (panel, query, { signal, tenantId }) =>
  download('/job-reports/' + panel + '/export', reportFilters(query, tenantId), '作业报表.xlsx', {
    signal,
    silent: true,
    headers: scopeHeaders(tenantId),
  })
