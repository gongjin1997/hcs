export type Severity = 'P1' | 'P2' | 'P3' | 'P4'
export type AlarmStatus = 'FIRING' | 'ACKED' | 'SUPPRESSED' | 'RESOLVED' | 'CLOSED'
export type Health = 'OK' | 'INFO' | 'WARNING' | 'MAJOR' | 'CRITICAL' | 'UNKNOWN'

export interface ApiEnvelope<T> { code: number; message: string; data: T }
export interface Page<T> { items: T[]; total: number; page: number; page_size: number }

// ---- 用户 / 角色 / 租户（RBAC：角色 = 权限点集合；数据权限 = 可访问的租户）----
export interface TenantBrief { id: number; code: string; name: string; short_name: string; color: string; auth_type: AuthType; enabled: boolean }
export interface UserInfo {
  id: number; username: string; display_name: string; email?: string | null
  role_id: number | null; role_code: string | null; role_name: string; permissions: string[]
  tenant_ids: number[] | null          // null = 全部租户
  tenants: TenantBrief[]               // 可访问的租户（顶部租户切换）
  is_admin: boolean
}
export interface UserAdmin {
  id: number; username: string; display_name: string; email?: string | null; role_id: number | null; role_code: string | null
  role_name: string; tenant_ids: number[] | null; enabled: boolean; last_login_at: string | null; created_at: string | null; locked: boolean
}
export interface Role { id: number; code: string; name: string; description: string; permissions: string[]; builtin: boolean; user_count: number }
export interface PermissionGroup { group: string; items: { code: string; name: string }[] }
export interface AuditLog {
  id: number; at: string; operator: string; username: string | null; action: string; action_name: string
  target_type: string | null; target_id: string | null; target_name: string | null; tenant_id: number | null; tenant_name: string | null
  result: 'SUCCESS' | 'FAILED'; detail: Record<string, any> | null; ip: string | null
}

export type AuthType = 'PASSWORD' | 'AKSK'
export type MetricsSource = 'AUTO' | 'CES' | 'PROMETHEUS' | 'NONE'
export interface TenantProject {
  id?: number; region_id: string; project_id: string; project_name: string | null; external_domain: string; env: string
  workspaces: string[]; enabled: boolean
}
export interface TenantCheck { name: string; kind: 'auth' | 'service' | 'capability'; ok: boolean; state?: string; detail?: string; message?: string; cost_ms?: number }
export interface TenantCheckResult { at?: string; ok: boolean; checks: TenantCheck[]; metrics_source?: string; hcs_version?: string }
export interface Capability { available: boolean | null; reason: string; at: string }
export interface Tenant {
  id: number; code: string; name: string; short_name: string; color: string; iam_endpoint: string; domain_name: string
  domain_id: string | null; auth_type: AuthType; username: string | null; access_key_mask: string | null; secret_set: boolean
  endpoints: Record<string, string>; enabled: boolean; report_mail_to: string[]; remark: string | null
  metrics_source: MetricsSource; metrics_effective: MetricsSource; prometheus_url: string | null
  capabilities: Record<string, Capability>; last_check: Partial<TenantCheckResult>; last_check_at: string | null
  projects: TenantProject[]; resource_count: number; user_count: number
}
export interface TenantIn {
  code: string; name: string; short_name: string; color: string; iam_endpoint: string; domain_name: string; domain_id: string | null
  auth_type: AuthType; username: string | null; access_key: string | null; secret: string | null; endpoints: Record<string, string>
  enabled: boolean; report_mail_to: string[]; remark: string | null; metrics_source: MetricsSource; prometheus_url: string | null
  projects: TenantProject[]
}
export interface SystemInfo {
  version: string; hcs_version: string; env: string; mock: boolean; metrics_sources: Record<string, string>
  capabilities: Record<string, string>; notes: Record<string, string>
}

export interface Alarm {
  id: number; rule_id: string; service: string; resource_type: string; resource_name: string
  resource_ref: number | null; tenant_id: number | null; sub_object: string; severity: Severity; status: AlarmStatus
  suppress_reason: string | null; title: string; check_item: string; current_value: string | null
  threshold_text: string | null; owner: string | null; first_seen: string; last_seen: string
  resolved_at: string | null; count: number; ack_by: string | null; ack_at: string | null
}
export interface AlarmLog { id: number; action: string; from_status: string | null; to_status: string | null; operator: string; note: string | null; at: string }
export interface NotificationRow {
  id: number; kind: string; channel: string; recipients: string[]; subject: string | null; status: string
  sent_at: string | null; error: string | null; severity?: Severity; retry?: number; created_at?: string; alarm_id?: number; tenant_id?: number | null
}
export interface AlarmDetail extends Alarm {
  fingerprint: string; detail: Record<string, any>; runbook: string | null; logs: AlarmLog[]; notifications: NotificationRow[]
}
export interface AlarmStats { FIRING: number; ACKED: number; SUPPRESSED: number; P1: number; P2: number; P3: number; P4: number }

export interface Resource {
  id: number; tenant_id: number | null; service: string; resource_type: string; resource_id: string; name: string; status_raw: string | null
  health: Health; region: string | null; az: string | null; ip: string | null; spec: string | null
  vcpus: number | null; memory_gb: number | null; storage_gb: number | null; created_time: string | null
  labels: Record<string, string>; attrs: Record<string, any>; metrics: Record<string, any>
  expected_state: Record<string, any>; owner: string | null; business: string | null; env_type: string | null
  remark: string | null; monitored: boolean; critical: boolean; lifecycle: 'ACTIVE' | 'DELETED'
  first_seen: string; last_seen: string; deleted_at: string | null; status_since: string | null; active_alarms: number
}
export type ResourcePatch = Partial<Pick<Resource, 'owner' | 'business' | 'env_type' | 'remark' | 'critical' | 'monitored' | 'expected_state'>>
export interface LedgerColumn { key: string; label: string; width?: number }
export interface ResourceType {
  code: string; name: string; category: string; service: string; ledger: boolean; inspected: boolean
  columns: LedgerColumn[]; total: number; abnormal: number
}
export interface ResourceCategory { code: string; name: string; total: number; abnormal: number }
export interface ResourceMeta {
  categories: ResourceCategory[]; types: ResourceType[]; env_types: Record<string, string>
  options: { business: string[]; owner: string[] }
}
export interface LedgerSummary {
  total: number; type_count: number; by_category: { code: string; name: string; total: number }[]
  compute: { ecs: number; vcpus: number; memory_gb: number }; storage_gb: number; unassigned: number; critical: number
  idle: { evs_available: number; eip_unbound: number; ecs_stopped: number }
  changes_7d: Partial<Record<ChangeType, number>>
  ecs_status: Record<string, number>
  last_sync: { started_at: string; status: string; api_calls: number; duration_sec: number | null } | null
}
export type ChangeType = 'CREATED' | 'UPDATED' | 'DELETED' | 'STATUS' | 'REBOOT'
export interface FieldChange { field: string; label: string; old: unknown; new: unknown }
export interface ResourceChange {
  id: number; tenant_id: number | null; resource_ref: number; resource_type: string; resource_id: string; name: string; change_type: ChangeType
  changes: FieldChange[]; source: 'SYNC' | 'MANUAL' | 'INSPECT'; operator: string; detected_at: string
}
export interface ResourceDetail {
  resource: Resource; alarms: Alarm[]; changes: ResourceChange[]; status_changes: ResourceChange[]
  tenant: { id: number; name: string; short_name: string; color: string } | null
}

// ---- 报表 ----
export type ReportKind = 'DAILY' | 'WEEKLY' | 'MONTHLY' | 'LEDGER'
export interface ReportBrief {
  id: number; kind: ReportKind; title: string; period_start: string; period_end: string; summary: Record<string, number | null>
  tenant_id: number | null; tenant_name: string
  created_by: string; created_at: string; sent_to: string[]; sent_at: string | null; send_error: string | null
}
export interface ReportOverview {
  new_alarms: number; p1: number; p2: number; p3: number; p4: number; resolved: number; mtta_min: number | null
  mttr_min: number | null; ack_rate: number | null; active_at_end?: number; inspection_runs?: number
  run_success_rate?: number | null; health_score_now?: number
}
export interface ReportLedger {
  total: number; by_category: { code: string; name: string; count: number }[]
  by_type: { code: string; name: string; count: number; unassigned: number }[]
  vcpus: number; memory_gb: number; storage_gb: number; unassigned: number; idle: number
  changes_count: Partial<Record<ChangeType, number>>
  changes: { change_type: ChangeType; type_name: string; name: string; detected_at: string; summary: string }[]
}
export interface ReportData {
  kind: ReportKind; kind_name: string; title: string; period: { start: string; end: string; label: string }; generated_at: string
  ledger: ReportLedger
  overview?: ReportOverview; previous?: ReportOverview | null; compare?: Record<string, number | null> | null
  trend?: { labels: string[]; series: Record<Severity, number[]> }
  by_service?: { service: string; name: string; count: number; p1: number; p2: number; p3: number; p4: number }[]
  top_resources?: { name: string; type_name: string; count: number; max_severity: Severity }[]
  top_rules?: { rule_id: string; name: string; count: number }[]
  key_alarms?: { severity: Severity; title: string; resource_name: string; type_name: string; first_seen: string; duration_min: number | null; status: AlarmStatus; ack_by: string | null }[]
  tasks?: { code: string; name: string; runs: number; success: number; partial: number; failed: number; success_rate: number | null; avg_sec: number | null }[]
  capacity?: { name: string; type_name: string; metric: string; value: number }[]
  tenant?: { id: number; name: string; short_name: string } | null
  by_tenant?: { tenant_id: number; name: string; new_alarms: number; p1: number; p2: number; health_score: number | null }[]
  ecs?: { total: number; active: number; shutoff: number; error: number; status_changes: number; reboots: number; top_reboot: { name: string; count: number }[] }
  pods?: { events: number; restarts: number; oom: number; crashloop: number; backpressure: number; backpressure_min: number; evicted: number; top_restart: { workload: string; count: number }[] }
  conclusion: string[]
}
export interface ReportDetail extends ReportBrief { data: ReportData; default_to?: string[] }

export interface RuleLevel { severity: Severity; op: string; threshold: number }
export interface Rule {
  id: string; name: string; resource_type: string; check_item: string; kind: 'threshold' | 'state'
  scope: Record<string, any>; expr: Record<string, any>; for_count: number; recover: Record<string, any>
  repeat_interval_sec: number | null; runbook: string | null; enabled: boolean; version: number
  updated_by: string | null; updated_at: string | null
}
export interface DryRunResult { matched: boolean; severity: Severity | null; recovered: boolean; message: string }

export type CapacityKey = 'dcs_memory' | 'gaussdb_disk' | 'ecs_cpu' | 'ecs_disk'
export interface HealthGroup { code: string; name: string; service: string; score: number; resources: number; abnormal: number; alarms: Record<Severity, number> }
export interface TaskBrief {
  code: string; name: string; service: string; interval_sec: number; enabled: boolean; last_status: string | null
  last_run_at: string | null; duration_sec: number | null; resource_total: number; abnormal_total: number
}
export interface Summary {
  health_score: number
  kpis: { resources: number; abnormal_resources: number; active_alarms: number; firing: number; p1: number; p2: number; p3: number; p4: number; today_new: number; resolved_24h: number; mtta_min: number | null; mttr_min: number | null }
  groups: HealthGroup[]
  trend: { hours: string[]; series: Record<Severity, number[]> }
  top_alarms: Alarm[]
  capacity: Record<CapacityKey, { name: string; value: number; resource_id: number }[]>
  tasks: TaskBrief[]
  ledger: { total: number; unassigned: number; by_category: { code: string; name: string; count: number }[]; changes_7d: Partial<Record<ChangeType, number>> }
  tenants: TenantHealth[]
  ecs: { total: number; by_status: EcsStatusCount[]; with_metrics: number }
  pods: {
    restarts_24h: number; oom_24h: number; events_24h: number; crashloop: number; backpressure: number; top_restart: { workload: string; count: number }[]
    ongoing?: { id: number; event_type: string; type_name: string; color: string; level: string; workload: string; cluster_name: string; tenant_id: number; first_time: string }[]
    ongoing_total?: number
  }
  updated_at: string
}
export interface EcsStatusCount { status: string; name: string; count: number }
export interface TenantHealth {
  id: number; code: string; name: string; short_name: string; color: string; auth_type: AuthType; score: number; resources: number
  abnormal: number; alarms: Record<Severity, number>; metrics_source: MetricsSource; ecs: EcsStatusCount[]
  capabilities: { ces: boolean | null; prometheus: boolean | null; cce_api: boolean | null }; last_check_ok: boolean | null; last_check_at: string | null
}
export interface AlarmPushEvent { type: string; alarm: Alarm }

// ---- 容器事件（CCE Pod 事件：重启 / OOM / CrashLoop / 背压 / 驱逐 …）----
export type PodEventLevel = 'CRITICAL' | 'WARNING' | 'INFO'
export interface PodEvent {
  id: number; tenant_id: number; tenant_name: string; cluster_id: string; cluster_name: string; namespace: string
  workload_kind: string; workload: string; pod: string; container: string; node: string; event_type: string; type_name: string
  color: string; level: PodEventLevel; reason: string; message: string; source: 'K8S_EVENT' | 'POD_STATUS' | 'PROBE'; count: number
  first_time: string; last_time: string; end_time: string | null; ongoing: boolean; peak: number | null; duration_sec: number | null
  detail: Record<string, any>
}
export interface PodEventStats {
  hours: number; total: number; restarts: number; ongoing: number; critical: number
  by_type: { type: string; name: string; color: string; level: PodEventLevel; count: number }[]
  by_level: { level: PodEventLevel; name: string; count: number }[]
  trend: { labels: string[]; series: { restart: number[]; backpressure: number[]; other: number[] }; step: string }
  top_workloads: {
    tenant_id: number; tenant_name: string; cluster_name: string; namespace: string; workload: string; events: number; restarts: number
    backpressure: number; critical: number; last_time: string; types: { type: string; name: string; count: number }[]
  }[]
  pods: { total: number; ready: number; not_ready: number; crashloop: number; restarts: number; clusters: number }
}
export interface PodEventOptions {
  clusters: { cluster_id: string; cluster_name: string; tenant_id: number; tenant_name: string }[]
  namespaces: string[]; namespaces_by_cluster: Record<string, string[]>
  workloads: { cluster_id: string; namespace: string; kind: string; name: string }[]
  event_types: { code: string; name: string; level: PodEventLevel; color: string; ongoing?: boolean }[]
  levels: { code: string; name: string }[]; sources: { code: string; name: string }[]
}
export interface PodContainer {
  name: string; image: string; restart_count: number; ready: boolean; state: string; reason: string | null; started_at: string | null
  last_reason: string | null; last_exit_code: number | null; last_started: string | null; last_finished: string | null; memory_limit: string | null
}
export interface PodState {
  key: string; tenant_id: number; tenant_name: string; cluster_id: string; cluster_name: string; namespace: string; pod: string
  workload_kind: string; workload: string; node: string; pod_ip: string | null; phase: string; ready: boolean; status_reason: string | null
  restarts: number; started_at: string | null; labels: Record<string, string>; containers: PodContainer[]; deleted: boolean
  first_seen: string; last_seen: string
}
export interface PodTimeline { state: PodState | null; events: PodEvent[]; restarts_in_window: number }
export interface ProbeIn {
  tenant_id: number; name: string; cluster_id: string; namespace: string; selector: string; port: number; path: string; metric: string
  label_filter: string; agg: 'max' | 'avg' | 'sum'; op: string; threshold: number; unit: string; for_minutes: number; recover_minutes: number; enabled: boolean
}
export interface Probe extends ProbeIn {
  id: number; tenant_name: string; updated_by: string | null; updated_at: string | null
  last_result: { at: string; pods: number; hit: number; max: number | null; errors: string[] } | null
}
