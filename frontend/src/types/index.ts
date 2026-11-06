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
  phone: string | null; login_enabled: boolean; notify_enabled: boolean; contact_id: number | null; owner_count: number; groups: string[]
  has_password?: boolean
}
export interface UserCreate {
  username: string; display_name: string; email?: string | null; phone?: string | null; role_id?: number | null
  tenant_ids: number[] | null; password?: string; enabled?: boolean; login_enabled?: boolean; notify_enabled?: boolean
}
export interface UserPatch {
  display_name?: string; email?: string | null; phone?: string | null; role_id?: number | null; tenant_ids?: number[] | null
  all_tenants?: boolean; password?: string; enabled?: boolean; login_enabled?: boolean; notify_enabled?: boolean
}
/** 安全人员投影：用于负责人选择，不包含角色、密码和登录信息。 */
export interface UserOption {
  id: number; username: string; display_name: string; email: string | null; phone: string | null
  enabled: boolean; notify_enabled: boolean; tenant_ids: number[] | null
}
export interface UserImportResult {
  total: number; created: number; updated: number; unchanged: number; error_count: number
  errors: { row: number; username: string | null; message: string }[]
  items: { row: number; username: string; action: 'create' | 'update' | 'unchanged'; changes: Record<string, [unknown, unknown]> }[]
  dry_run: boolean; applied: number
}
export interface Role { id: number; code: string; name: string; description: string; permissions: string[]; builtin: boolean; user_count: number }
export interface PermissionGroup { group: string; items: { code: string; name: string }[] }
export interface AuditLog {
  id: number; at: string; operator: string; username: string | null; action: string; action_name: string
  target_type: string | null; target_id: string | null; target_name: string | null; tenant_id: number | null; tenant_name: string | null
  result: 'SUCCESS' | 'FAILED'; detail: Record<string, any> | null; ip: string | null
}

export type AuthType = 'PASSWORD' | 'AKSK'
export type MetricsSource = 'AUTO' | 'AOM' | 'CES' | 'PROMETHEUS' | 'MANAGEONE' | 'NONE'
export interface TenantProject {
  id?: number; region_id: string; project_id: string; project_name: string | null; external_domain: string; env: string
  workspaces: string[]; enabled: boolean
}
export interface TenantCheck { name: string; kind: 'auth' | 'service' | 'capability'; ok: boolean; state?: string; detail?: string; message?: string; cost_ms?: number; endpoint?: string; endpoint_source?: 'tenant' | 'global' | 'mock'; network_reason?: string; retryable?: boolean }
export interface TenantCheckResult { at?: string; ok: boolean; checks: TenantCheck[]; metrics_source?: string; hcs_version?: string; configuration_warnings?: string[] }
export interface Capability { available: boolean | null; reason: string; at: string }
export interface Tenant {
  id: number; code: string; name: string; short_name: string; color: string; iam_endpoint: string; domain_name: string
  domain_id: string | null; auth_type: AuthType; username: string | null; access_key_mask: string | null; secret_set: boolean
  endpoints: Record<string, string>; enabled: boolean; report_mail_to: string[]; report_user_ids?: number[]; remark: string | null
  metrics_source: MetricsSource; metrics_effective: MetricsSource; prometheus_url: string | null
  capabilities: Record<string, Capability>; last_check: Partial<TenantCheckResult>; last_check_at: string | null
  projects: TenantProject[]; resource_count: number; user_count: number
}
export interface TenantIn {
  code: string; name: string; short_name: string; color: string; iam_endpoint: string; domain_name: string; domain_id: string | null
  auth_type: AuthType; username: string | null; access_key: string | null; secret: string | null; endpoints: Record<string, string>
  enabled: boolean; report_user_ids: number[]; remark: string | null; metrics_source: MetricsSource; prometheus_url: string | null
  projects: TenantProject[]
}
export interface SystemInfo {
  version: string; hcs_version: string; env: string; mock: boolean; metrics_sources: Record<string, string>
  capabilities: Record<string, string>; notes: Record<string, string>
}

export interface Alarm {
  id: number; rule_id: string; service: string; resource_type: string; resource_name: string
  resource_ref: number | null; tenant_id: number | null; sub_object: string; severity: Severity; status: AlarmStatus
  suppress_reason: string | null; silence_id?: number | null; title: string; check_item: string; current_value: string | null
  threshold_text: string | null; owner: string | null; first_seen: string; last_seen: string
  resolved_at: string | null; count: number; ack_by: string | null; ack_at: string | null
}
export interface AlarmLog { id: number; action: string; from_status: string | null; to_status: string | null; operator: string; note: string | null; at: string }
export interface NotificationRow {
  id: number; kind: string; channel: string; recipients: string[]; subject: string | null; status: string
  sent_at: string | null; error: string | null; severity?: Severity; retry?: number; next_retry_at?: string | null
  message_id?: string | null; created_at?: string; alarm_id?: number; tenant_id?: number | null
}
export interface AlarmDetail extends Alarm {
  fingerprint: string; detail: Record<string, any>; runbook: string | null; logs: AlarmLog[]; notifications: NotificationRow[]
  silence?: SilenceBrief | null
}
export interface AlarmStats { FIRING: number; ACKED: number; SUPPRESSED: number; P1: number; P2: number; P3: number; P4: number }

export interface Resource {
  classification?: string | null; classification_source?: string | null
  id: number; tenant_id: number | null; service: string; resource_type: string; resource_id: string; name: string; status_raw: string | null
  health: Health; region: string | null; az: string | null; ip: string | null; spec: string | null
  vcpus: number | null; memory_gb: number | null; storage_gb: number | null; created_time: string | null
  labels: Record<string, string>; attrs: Record<string, any>; metrics: Record<string, any>
  expected_state: Record<string, any>; owner: string | null; owner_user_id: number | null; business: string | null; env_type: string | null
  remark: string | null; monitored: boolean; scope_excluded?: boolean; critical: boolean; lifecycle: 'ACTIVE' | 'DELETED'
  first_seen: string; last_seen: string; deleted_at: string | null; status_since: string | null; active_alarms: number
}
export type ResourcePatch = Partial<Pick<Resource, 'owner_user_id' | 'business' | 'env_type' | 'remark' | 'critical' | 'monitored' | 'expected_state'>>
export interface LedgerColumn { key: string; label: string; width?: number }
export interface ResourceType {
  code: string; name: string; category: string; service: string; ledger: boolean; inspected: boolean
  columns: LedgerColumn[]; total: number; abnormal: number
  /** v0.4 采集开关：enabled = 当前范围内至少一个租户在采集；off_tenants = 已关闭采集的租户 */
  collect?: { enabled: boolean; off_tenants: string[]; global: boolean }
}
export interface ResourceCategory { code: string; name: string; total: number; abnormal: number }
export interface ResourceMeta {
  classifications?: ClassificationSummary
  categories: ResourceCategory[]; types: ResourceType[]; env_types: Record<string, string>
  options: { business: string[]; owner: string[] }
  /** owners 为统一人员派生的兼容显示名；历史筛选仍使用 options.owner。 */
  dict?: { business: string[]; owners: string[]; enforce: boolean }
}
export interface LedgerSummary {
  total: number; type_count: number; by_category: { code: string; name: string; total: number }[]
  compute: { ecs: number; vcpus: number | null; memory_gb: number | null }; storage_gb: number | null; unassigned: number; critical: number
  capacity_coverage?: Record<'vcpus' | 'memory_gb' | 'storage_gb', { known: number; total: number }>
  idle: { evs_available: number; eip_unbound: number; ecs_stopped: number }
  changes_7d: Partial<Record<ChangeType, number>>
  ecs_status: Record<string, number>
  last_sync: { started_at: string; status: string; api_calls: number; duration_sec: number | null } | null
}
export type ChangeType = 'CREATED' | 'UPDATED' | 'DELETED' | 'STATUS' | 'REBOOT'
export interface FieldChange { field: string; label: string; old: unknown; new: unknown }
export interface ResourceChange {
  id: number; tenant_id: number | null; resource_ref: number; resource_type: string; resource_id: string; name: string; change_type: ChangeType
  changes: FieldChange[]; source: 'SYNC' | 'MANUAL' | 'INSPECT' | 'IMPORT'; operator: string; detected_at: string
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
  run_success_rate?: number | null; health_score_now?: number | null
}
export interface ReportLedger {
  total: number; by_category: { code: string; name: string; count: number }[]
  by_type: { code: string; name: string; count: number; unassigned: number }[]
  vcpus: number | null; memory_gb: number | null; storage_gb: number | null; unassigned: number; idle: number
  capacity_coverage?: Record<'vcpus' | 'memory_gb' | 'storage_gb', { known: number; total: number }>
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
  tasks?: { code: string; name: string; runs: number; completed?: number; success: number; partial: number; failed: number; skipped?: number; running?: number; success_rate: number | null; avg_sec: number | null }[]
  capacity?: { name: string; type_name: string; metric: string; value: number }[]
  tenant?: { id: number; name: string; short_name: string } | null
  by_tenant?: { tenant_id: number; name: string; new_alarms: number; p1: number; p2: number; health_score: number | null }[]
  ecs?: { total: number; active: number; shutoff: number; error: number; status_changes: number; reboots: number; top_reboot: { name: string; count: number }[] }
  pods?: { events: number; restarts: number | null; oom: number | null; crashloop: number; backpressure: number; backpressure_min: number; evicted: number | null; top_restart: { workload: string; count: number | null; tenant_id?: number | null; project_ref?: number | null; cluster_id?: string; workload_kind?: string }[] }
  conclusion: string[]
}
export interface ReportDetail extends ReportBrief { data: ReportData; default_to?: string[]; default_user_ids?: number[] }

export interface RuleLevel { severity: Severity; op: string; threshold: number }
export interface Rule {
  id: string; name: string; resource_type: string; check_item: string; kind: 'threshold' | 'state'
  scope: Record<string, any>; expr: Record<string, any>; for_count: number; recover: Record<string, any>
  repeat_interval_sec: number | null; runbook: string | null; enabled: boolean; version: number
  updated_by: string | null; updated_at: string | null
}
export interface DryRunResult { matched: boolean; severity: Severity | null; recovered: boolean; message: string }

export type CapacityKey = 'dcs_memory' | 'gaussdb_disk' | 'ecs_cpu' | 'ecs_disk'
export interface HealthGroup { code: string; name: string; service: string; score: number | null; resources: number; assessed_resources?: number; unknown_resources?: number; abnormal: number; alarms: Record<Severity, number> }
export interface TaskBrief {
  code: string; name: string; service: string; interval_sec: number; enabled: boolean; last_status: string | null
  cron?: string | null
  last_run_at: string | null; duration_sec: number | null; resource_total: number | null; abnormal_total: number | null
  scopes?: TaskHealthTenant[]; scope_verified?: boolean
  count_basis?: 'LATEST_COMPLETE_KNOWN_PROJECTS' | 'GLOBAL_RUN' | 'UNVERIFIED_PROJECT_COVERAGE'
  counts_complete?: boolean; expected_scope_count?: number; history_unknown_scope_count?: number
  representative_scope?: { run_id: number; tenant_id: number | null; project_ref: number | null; project_scope: 'PROJECT' | 'UNKNOWN' | 'GLOBAL' } | null
}
export interface Summary {
  health_score: number | null
  kpis: { resources: number; assessed_resources?: number; unknown_resources?: number; abnormal_resources: number; active_alarms: number; firing: number; p1: number; p2: number; p3: number; p4: number; today_new: number; resolved_24h: number; mtta_min: number | null; mttr_min: number | null }
  groups: HealthGroup[]
  trend: { hours: string[]; series: Record<Severity, number[]> }
  top_alarms: Alarm[]
  capacity: Record<CapacityKey, { name: string; value: number; resource_id: number }[]>
  tasks: TaskBrief[]
  ledger: { total: number; unassigned: number; by_category: { code: string; name: string; count: number }[]; changes_7d: Partial<Record<ChangeType, number>> }
  tenants: TenantHealth[]
  ecs: { total: number; by_status: EcsStatusCount[]; with_metrics: number }
  pods: {
    restarts_24h: number | null; oom_24h: number | null; events_24h: number; crashloop: number; backpressure: number; top_restart: { workload: string; count: number | null; tenant_id?: number | null; project_ref?: number | null; cluster_id?: string; namespace?: string; workload_kind?: string; workload_name?: string; scope_key?: string }[]
    ongoing?: { id: number; event_type: string; type_name: string; color: string; level: string; workload: string; cluster_name: string; tenant_id: number; project_ref?: number | null; cluster_id?: string; namespace?: string; workload_kind?: string; workload_name?: string; first_time: string }[]
    ongoing_total?: number
  }
  updated_at: string
}
export interface EcsStatusCount { status: string; name: string; count: number }
export interface TenantHealth {
  id: number; code: string; name: string; short_name: string; color: string; auth_type: AuthType; score: number | null; resources: number; unknown_resources?: number
  abnormal: number; alarms: Record<Severity, number>; metrics_source: MetricsSource; metrics_effective?: MetricsSource; ecs: EcsStatusCount[]
  capabilities: { ces: boolean | null; prometheus: boolean | null; cce_api: boolean | null }; last_check_ok: boolean | null; last_check_at: string | null
}
export interface AlarmPushEvent { type: string; alarm: Alarm }

// ---- 容器事件（CCE Pod 事件：重启 / OOM / CrashLoop / 背压 / 驱逐 …）----
export type PodEventLevel = 'CRITICAL' | 'WARNING' | 'INFO'
export interface PodEvent {
  project_ref?: number | null
  id: number; tenant_id: number; tenant_name: string; cluster_id: string; cluster_name: string; namespace: string
  workload_kind: string; workload: string; pod: string; container: string; node: string; event_type: string; type_name: string
  color: string; level: PodEventLevel; reason: string; message: string; source: 'K8S_EVENT' | 'POD_STATUS' | 'PROBE'; count: number
  first_time: string; last_time: string; end_time: string | null; ongoing: boolean; peak: number | null; duration_sec: number | null
  detail: Record<string, any>
}
export interface PodEventStats {
  hours: number; total: number; restarts: number | null; restart_count_complete?: boolean; ongoing: number; critical: number
  by_type: { type: string; name: string; color: string; level: PodEventLevel; count: number }[]
  by_level: { level: PodEventLevel; name: string; count: number }[]
  trend: { labels: string[]; series: { restart: (number | null)[]; backpressure: number[]; other: number[] }; step: string }
  top_workloads: {
    tenant_id: number; tenant_name: string; cluster_name: string; namespace: string; workload: string; events: number; restarts: number | null
    backpressure: number; critical: number; last_time: string; types: { type: string; name: string; count: number }[]
  }[]
  pods: { total: number; ready: number; not_ready: number; crashloop: number; restarts: number; clusters: number
    fresh?: number; stale?: number; unknown?: number; scopes?: PodSnapshotScope[] }
}
export interface PodSnapshotFreshness {
  status: 'FRESH' | 'STALE' | 'UNKNOWN' | 'PARTIAL' | 'DELETED'
  last_observed_at: string | null; age_sec: number | null; stale_after_sec: number | null
}
export interface PodSnapshotScope {
  tenant_id: number | null; project_ref: number | null; tenant_name: string | null; cluster_id: string; cluster_name: string
  total: number; fresh: number; stale: number; unknown: number; ready: number; not_ready: number; crashloop: number; restarts: number
  freshness: PodSnapshotFreshness
}
export interface PodEventOptions {
  clusters: { cluster_id: string; cluster_name: string; tenant_id: number; tenant_name: string; project_ref?: number | null; namespaces?: string[] }[]
  namespaces: string[]; namespaces_by_cluster: Record<string, string[]>
  workloads: { cluster_id: string; namespace: string; kind: string; name: string; tenant_id?: number | null; project_ref?: number | null }[]
  event_types: { code: string; name: string; level: PodEventLevel; color: string; ongoing?: boolean }[]
  levels: { code: string; name: string }[]; sources: { code: string; name: string }[]
}
export interface PodContainer {
  name: string; image: string; restart_count: number; ready: boolean; state: string; reason: string | null; started_at: string | null
  last_reason: string | null; last_exit_code: number | null; last_started: string | null; last_finished: string | null; memory_limit: string | null
  init?: boolean; exit_code?: number | null; finished_at?: string | null
}
export interface PodState {
  project_ref?: number | null
  key: string; tenant_id: number; tenant_name: string; cluster_id: string; cluster_name: string; namespace: string; pod: string
  workload_kind: string; workload: string; node: string; pod_ip: string | null; phase: string; ready: boolean; status_reason: string | null
  restarts: number; started_at: string | null; labels: Record<string, string>; containers: PodContainer[]; deleted: boolean
  first_seen: string; last_seen: string
  freshness?: PodSnapshotFreshness
}
export interface PodTimeline { state: PodState | null; events: PodEvent[]; restarts_in_window: number | null; total?: number; truncated?: boolean }
export interface ProbeIn {
  tenant_id: number; name: string; cluster_id: string; namespace: string; selector: string; port: number; path: string; metric: string
  label_filter: string; agg: 'max' | 'avg' | 'sum'; op: string; threshold: number; unit: string; for_minutes: number; recover_minutes: number; enabled: boolean
}
export interface Probe extends ProbeIn {
  id: number; tenant_name: string; updated_by: string | null; updated_at: string | null
  last_result: { at: string; pods: number; observed?: number; status?: string; hit: number; max: number | null; errors: string[] } | null
}

// ================================================================ v0.4
// ---- 静默与维护窗口 ----
export type SilenceKind = 'SILENCE' | 'MAINTENANCE'
/** ACTIVE 生效中 / WAITING 周期窗口之间（本周期未到）/ PENDING 待生效 / EXPIRED 已结束 / CANCELLED 已取消 */
export type SilenceStatus = 'ACTIVE' | 'WAITING' | 'PENDING' | 'EXPIRED' | 'CANCELLED'
export interface SilenceMatchers {
  tenant_ids?: number[]; services?: string[]; resource_types?: string[]; rule_ids?: string[]; severities?: string[]
  resource_refs?: number[]; fingerprints?: string[]; name_contains?: string
}
/** 每周重复：weekdays 1=周一 … 7=周日；start / end 为平台时区 HH:mm，end ≤ start 表示跨午夜 */
export interface SilenceRecurrence { weekdays?: number[]; start?: string; end?: string }
export interface Silence {
  id: number; name: string; kind: SilenceKind; matchers: SilenceMatchers; recurrence: SilenceRecurrence
  starts_at: string; ends_at: string; reason: string; created_by: string; created_at: string; updated_at: string
  cancelled_by: string | null; cancelled_at: string | null; status: SilenceStatus
  next_window: { start: string; end: string } | null; describe: string; suppressed_now: number; suppressed_total: number; editable: boolean
}
export interface SilenceIn {
  name: string; kind: SilenceKind; matchers: SilenceMatchers; starts_at?: string | null; ends_at: string
  recurrence?: SilenceRecurrence | null; reason: string
}
export interface SilencePreview { total: number; critical: number; by_severity: Record<Severity, number>; items: Alarm[] }
export interface SilenceBrief {
  id: number; name: string; kind: SilenceKind; starts_at: string; ends_at: string; created_by: string; reason: string; status: SilenceStatus
}

// ---- 采集开关（按资源类型 × 租户启停采集）----
export interface SwitchCell { setting: boolean | null; effective: boolean; resources: number; active_alarms: number }
export interface SwitchType {
  code: string; name: string; service: string; service_name: string; category: string; ledger: boolean; tasks: string[]
  global: boolean; tenants: Record<string, SwitchCell>
}
export interface SwitchTenant { id: number; code: string; name: string; short_name: string; color: string; enabled: boolean }
export interface CollectSwitches {
  categories: { code: string; name: string }[]; types: SwitchType[]; tenants: SwitchTenant[]
  task_types: Record<string, string[]>; global_editable: boolean
}
/** tenant_id = 0 表示全局默认；租户 enabled = null 表示跟随全局 */
export interface SwitchItem { resource_type: string; tenant_id: number; enabled: boolean | null }
export interface SwitchImpact { stop: string[]; start: string[]; active_alarms: number }

// ---- 采集范围（采集开关之下，细化到命名空间 / 实例 / 工作空间下的作业）----
export type ScopeEffect = 'include' | 'exclude'
/** match：维度 → 值列表（精确值或含 * ? 的通配，区分大小写）。同一规则内各维度取“且”，同一维度内取“或”。 */
export interface ScopeRule { effect: ScopeEffect; match: Record<string, string[]>; note?: string }
export interface ScopeDimension { key: string; label: string }
export type ScopeSource = 'tenant' | 'global' | 'none'
export interface ScopeCell {
  /** 租户自己保存的规则；null = 跟随全局 */
  setting: ScopeRule[] | null; source: ScopeSource; rules: ScopeRule[]; resources: number; excluded: number
  updated_by: string | null; updated_at: string | null
}
export interface ScopeType {
  code: string; name: string; service: string; category: string; dimensions: ScopeDimension[]
  global: { rules: ScopeRule[]; updated_by: string | null; updated_at: string | null }
  tenants: Record<string, ScopeCell>
}
export interface CollectScopes {
  types: ScopeType[]; tenants: SwitchTenant[]; global_editable: boolean
  limits: { rules: number; patterns: number; pattern_length: number }
}
export interface ScopePreview {
  total: number; included: number; excluded: number; newly_excluded: number; newly_included: number
  closing_alarms: number; truncated: boolean; samples: { excluded: string[]; included: string[] }; rules: ScopeRule[]
}
export interface ScopeSaveResult {
  changed: boolean; before: ScopeRule[] | null; after: ScopeRule[] | null
  excluded: number; restored: number; closed_alarms: number
}
export interface ScopeCandidate { value: string; count: number }

// ---- 系统设置 ----
export interface SettingField { key: string; label: string; default: unknown; ge?: number; le?: number }
export interface SettingItem<T = Record<string, any>> {
  label: string; value: T; editable: boolean; fields: SettingField[]; updated_by: string | null; updated_at: string | null; customized: boolean
}
export interface Retention {
  inspection_run_days: number; notification_days: number; alarm_closed_days: number; audit_log_days: number
  resource_change_days: number; released_resource_days: number; report_days: number; pod_event_days: number
}
export interface PlatformAlarmSetting {
  enabled: boolean; task_fail_threshold: number; auth_fail_alarm: boolean; notify_fail_alarm: boolean
  heartbeat_timeout_min: number; severity: 'P1' | 'P2' | 'P3'; mail_to: string[]; recipient_user_ids?: number[]
}
export interface ReportSetting { cron: Record<string, string>; enabled: Record<string, boolean>; mail_to: string[]; recipient_user_ids?: number[] }
export interface ClassificationRule {
  id: string; resource_type: string; field: 'os' | 'name' | 'business' | 'spec'; pattern: string; category: string; enabled: boolean
}
export interface ClassificationSummary { categories: string[]; counts: Record<string, number>; total: number }
export interface ClassificationConfig { rules: ClassificationRule[]; fallback: string; categories?: string[]; counts?: Record<string, number>; total?: number }
export interface DictItem { name: string; remark: string | null; used?: number }
export interface LedgerDictSetting { business: DictItem[]; enforce: boolean }
export interface SystemSettings {
  collect?: SettingItem<{ hcs_concurrency: number; hcs_qps: Record<string, number> }>
  retention?: SettingItem<Retention>; report?: SettingItem<ReportSetting>
  platform_alarm?: SettingItem<PlatformAlarmSetting>; ledger_dict?: SettingItem<LedgerDictSetting>
}

// ---- 平台状态 ----
export interface WorkerStatus {
  name: string; label: string; status: 'OK' | 'LATE' | 'NEVER'; host?: string; pid?: number; version?: string
  started_at?: string; beat_at?: string; age_sec?: number; info?: Record<string, any>
}
export interface TaskHealthTenant {
  tenant_id: number | null; tenant_name: string | null; last_status: string; last_at: string; fails: number; last_success_at: string | null
  project_ref?: number | null; project_scope?: 'PROJECT' | 'UNKNOWN' | 'GLOBAL'; project_label?: string
  effective_status?: string | null; effective_run_id?: number | null
}
export interface TaskHealth {
  code: string; name: string; enabled: boolean; interval_sec: number; cron: string | null; tenants: TaskHealthTenant[]; status: 'OK' | 'WARN' | 'FAILED' | 'UNKNOWN' | 'DISABLED'
}
export interface PlatformStatus {
  version: string; env: string; timezone: string; api_started_at: string; database: string | null
  redis: { configured: boolean; ok: boolean | null }; sse_subscribers: number; metrics: { path: string; token_required: boolean }
  workers: WorkerStatus[]; heartbeat_timeout_min: number
  outbox: { sent_24h: number; failed_24h: number; merged_24h: number; pending: number; retrying: number; oldest_pending_at: string | null; last_sent_at: string | null }
  tasks: TaskHealth[]; task_fail_threshold: number; platform_alarms: Alarm[]; active_silences: number
  cleanup: { started_at: string; status: string; stats: Record<string, number>; deleted: number } | null
  platform_alarm: PlatformAlarmSetting
}

// ---- 台账字典 / Excel 导入 ----
export interface LedgerDict {
  enforce: boolean; business: DictItem[]; owners: DictItem[]; business_outside: { name: string; used: number }[]
  owners_outside: { name: string; used: number }[]; updated_by: string | null; updated_at: string | null; customized: boolean
}
export interface ImportChange { field: string; label: string; old: string; new: string }
export interface ImportResult {
  total: number; matched: number; changed: number; unchanged: number; error_count: number; dry_run: boolean; applied?: number
  errors: { row: number; resource_id: string | null; name: string | null; message: string }[]
  items: { row: number; id: number; resource_id: string; name: string; type_name: string; tenant: string; changes: ImportChange[] }[]
}

// ---- 通知配置维护 ----
export interface Contact {
  id: number; user_id?: number | null; username?: string | null; name: string; email: string | null; phone: string | null
  enabled: boolean; groups: { code: string; name: string; leader: boolean }[]
}
export interface ContactIn { name: string; email: string; phone?: string | null; enabled: boolean }
export interface GroupIn { code: string; name: string; member_ids: number[]; leader_ids: number[] }
export interface Escalation { after_min: number; to: string; severities: string[] }
export interface RouteIn {
  name: string; priority: number; match: { tenant_ids?: number[]; services?: string[]; severities?: string[]; resource_types?: string[] }
  group_code: string; escalation: Escalation[]; continue_match: boolean; enabled: boolean
}
export interface SimulateStep {
  id: number; name: string; priority: number; enabled: boolean; skipped: boolean; matched: boolean; group_name: string
  recipients: { id: number; name: string; email: string }[]; continue_match: boolean
}
export interface SimulateResult {
  steps: SimulateStep[]; recipients: string[]; escalation: { route: string; after_min: number; emails: string[]; names: string[]; note: string }[]
  silence: { id: number; name: string; kind: SilenceKind } | null; platform_fallback: string[] | null
}
export interface SmtpInfo { host: string; port: number; starttls: boolean; auth: boolean; sender: string; reply_to: string | null; env_tag: string; base_url: string }
