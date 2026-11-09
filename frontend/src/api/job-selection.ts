import { get, put } from './request'
import { scopeHeaders } from './scope'

export type JobSelectionType = 'dgc_realtime_job' | 'dgc_batch_job'
export type JobSelectionMode = 'inherit' | 'selected' | 'all'
export interface JobSelectionTarget {
  id: number; name: string; enabled: boolean
  projects: { id: number; project_id: string; name: string; enabled: boolean; workspaces: string[] }[]
}
export interface JobSelection {
  tenant_id: number; resource_type: JobSelectionType; project_ref: number; workspace: string
  mode: JobSelectionMode; jobs: string[]; configured: boolean; invalid?: boolean
  updated_by?: string | null; updated_at?: string | null
}
export interface JobSelectionBody {
  project_ref: number; workspace: string; mode: 'selected' | 'all'; jobs: string[]
}
export interface JobDiscovery {
  items: { name: string; job_type: string | null; status: string | null }[]
  total: number; page: number; page_size: number
}
export interface JobSelectionSave {
  changed: boolean; before: Pick<JobSelection, 'mode' | 'jobs' | 'configured'>
  after: Pick<JobSelection, 'mode' | 'jobs' | 'configured'>
  excluded: number; restored: number; closed_alarms: number
}
const path = (tenant: number, type: JobSelectionType) => `/system/job-selections/${tenant}/${type}`
export const getJobSelectionTargets = (signal?: AbortSignal) =>
  get<JobSelectionTarget[]>('/system/job-selections/targets', undefined, { headers: scopeHeaders(null), signal })
export const getJobSelection = (tenant: number, type: JobSelectionType, project_ref: number, workspace: string, signal?: AbortSignal) =>
  get<JobSelection>(path(tenant, type), { project_ref, workspace }, { headers: scopeHeaders(tenant), signal })
export const discoverSelectionJobs = (tenant: number, type: JobSelectionType,
  query: { project_ref: number; workspace: string; page: number; page_size: number; keyword: string }, signal?: AbortSignal) =>
  get<JobDiscovery>(`${path(tenant, type)}/jobs`, query, { headers: scopeHeaders(tenant), signal })
export const saveJobSelection = (tenant: number, type: JobSelectionType, body: JobSelectionBody) =>
  put<JobSelectionSave>(path(tenant, type), body, { headers: scopeHeaders(tenant) })
