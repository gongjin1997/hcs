export interface CceSource {
  state: string
  attempted_at?: string
  succeeded_at?: string | null
  freshness?: string
  reason?: string
}
export interface CceNode {
  id: string
  name: string
  phase: string | null
  last_probe_at: string | null
  freshness: string
  health: string
  server_id: string | null
  private_ip: string | null
  flavor: string | null
  ecs_resource_ref?: number | null
}
export interface CceCondition { type: string | null; status: string; reason: string | null; message: string | null; last_probe_at: string | null }
export interface CcePool {
  id: string
  name: string
  current_nodes: number | null
  creating_nodes: number | null
  deleting_nodes: number | null
  synced_nodes: number | null
  conditions: CceCondition[]
}
export interface CceSnapshot {
  key: string
  tenant_id: number
  tenant_name: string
  project_ref: number
  project_name: string
  cluster_id: string
  last_seen: string
  freshness: string
  missing: boolean
  sources: Record<string, CceSource>
  cluster: {
    cluster_id: string; name: string; version: string | null; phase: string | null
    reason: string | null; message: string | null; job_id: string | null
    capacity: { total_nodes: number | null; active_nodes: number | null; total_cpu_raw: string | null; total_memory_raw: string | null; memory_unit: string; nodes_consistent?: boolean | null; active_count_consistent?: boolean | null }
    endpoints: { type: string | null; scheme: string; host: string; port: number | null; url: string }[]
    addons: { name: string | null; version: string | null; status: string | null }[] | null
  }
  nodes: CceNode[]
  nodepools: CcePool[]
  native: Record<string, unknown>
}
export interface CceList { items: CceSnapshot[]; total: number; page: number; page_size: number }
export interface CceSettings { enabled: boolean; resolve_owners: boolean; resource_metrics: boolean }
export interface CceSettingsOut {
  tenant_id: number; settings: CceSettings; master_enabled: boolean; task_enabled: boolean
  effective_enabled: boolean; interval_sec: number
}
