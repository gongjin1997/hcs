export type CoverageState = 'tenant_disabled' | 'collection_disabled' | 'no_projects' | 'projects_disabled'
  | 'inspection_disabled' | 'configuration_invalid' | 'no_resources' | 'no_eligible_resources'
  | 'not_inspected' | 'stale' | 'partial' | 'covered'
export interface MonitoringMetric {
  key: string
  label: string
  unit: string | null
  availability: 'collected' | 'derived' | 'not_collected'
}
export interface MonitoringCoverageRow {
  tenant_id: number
  tenant_name: string
  resource_type: 'ecs_server' | 'dcs_instance' | 'gaussdb_instance'
  resource_name: string
  resources: number
  out_of_scope_resources?: number
  eligible_resources: number
  inspected_resources: number
  unknown_resources: number
  abnormal_resources: number
  partial_resources: number
  stale_resources: number
  last_inspected_at: string | null
  projects: number
  enabled_projects: number
  collection_enabled: boolean
  ledger_enabled: boolean
  inspection_enabled: boolean
  metrics_enabled: boolean
  tenant_enabled: boolean
  ledger_task_enabled: boolean
  inspection_task_enabled: boolean
  metrics: MonitoringMetric[]
  state: CoverageState
  state_label: string
  blockers: string[]
  blocker_labels: string[]
  source_label: string
}
export interface MonitoringCoverage {
  evaluated_at: string
  rows: MonitoringCoverageRow[]
}
