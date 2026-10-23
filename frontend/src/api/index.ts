import type {
  Alarm, AlarmDetail, AlarmStats, AuditLog, ChangeType, CollectSwitches, Contact, ContactIn, DryRunResult, GroupIn, ImportResult,
  LedgerDict, LedgerDictSetting, LedgerSummary, NotificationRow, Page, PermissionGroup, PlatformStatus, PodEvent, PodEventOptions,
  PodEventStats, PodTimeline, Probe, ProbeIn, ReportBrief, ReportDetail, ReportKind, Resource, ResourceChange, ResourceDetail,
  ResourceMeta, ResourcePatch, Role, RouteIn, Rule, SettingItem, Silence, SilenceIn, SilenceMatchers, SilencePreview, SilenceStatus,
  SimulateResult, SmtpInfo, Summary, SwitchImpact, SwitchItem, SystemInfo, SystemSettings, Tenant, TenantCheckResult, TenantIn,
  UserAdmin, UserInfo,
} from '@/types'
import { del, download, get, patch, post, put } from './request'

// ---- 认证（运维组内部使用：账号密码登录，可选 7 天内免登录）----
export const login = (username: string, password: string, remember = false) =>
  post<{ access_token: string; expires_in: number; user: UserInfo }>('/auth/login', { username, password, remember }, { silent: true })
export const fetchMe = () => get<UserInfo>('/auth/me')
export interface LoginOptions { version: string; env: string | null; demo_accounts: { username: string; password: string; role: string }[] }
export const loginOptions = () => get<LoginOptions>('/auth/login-options', undefined, { silent: true })
export const changePassword = (old_password: string, new_password: string) =>
  post<null>('/auth/password', { old_password, new_password })

// ---- 总览 ----
export const fetchSummary = () => get<Summary>('/dashboard/summary')

// ---- 告警 ----
export interface AlarmQuery { status_group?: string; severity?: string[]; service?: string; resource_type?: string; resource_ref?: number; keyword?: string; page?: number; page_size?: number }
export const listAlarms = (q: AlarmQuery) => get<Page<Alarm>>('/alarms', q)
export const alarmStats = () => get<AlarmStats>('/alarms/stats')
export const getAlarm = (id: number) => get<AlarmDetail>(`/alarms/${id}`)
export const ackAlarm = (id: number, note?: string) => post<Alarm>(`/alarms/${id}/ack`, { note })
export const closeAlarm = (id: number, note?: string) => post<Alarm>(`/alarms/${id}/close`, { note })
export const batchAck = (ids: number[], note?: string) => post<{ acked: number }>('/alarms/batch-ack', { ids, note })
export const mailActionInfo = (token: string) => get<{ alarm: Alarm; action: string; recipient: string }>(`/mail-actions/${token}`, undefined, { silent: true })
export const mailActionDo = (token: string) => post<Alarm>(`/mail-actions/${token}`)

// ---- 资源台账 ----
export interface ResourceQuery {
  resource_type?: string; category?: string; health?: string[]; keyword?: string; owner?: string; business?: string
  env_type?: string; critical?: boolean; unassigned?: boolean; lifecycle?: 'ACTIVE' | 'DELETED' | 'ALL'; status?: string[]
  page?: number; page_size?: number
}
export interface ChangeQuery { days?: number; change_type?: ChangeType | ChangeType[] | ''; resource_type?: string; category?: string; keyword?: string; source?: string; page?: number; page_size?: number }
export const resourceMeta = () => get<ResourceMeta>('/resources/types')
export const ledgerSummary = () => get<LedgerSummary>('/resources/summary')
export const listResources = (q: ResourceQuery) => get<Page<Resource>>('/resources', q)
export const listChanges = (q: ChangeQuery) => get<Page<ResourceChange>>('/resources/changes', q)
export const getResource = (id: number) => get<ResourceDetail>(`/resources/${id}`)
export const patchResource = (id: number, body: ResourcePatch) => patch<Resource>(`/resources/${id}`, body)
export const syncLedger = (resource_type?: string) =>
  post<{ run_id: number; status: string; resource_total: number; changes: number; stats: Record<string, any>; errors: string | null }>(
    '/resources/sync', undefined, { params: { resource_type }, timeout: 120000 })
export const exportLedger = (q: { resource_type?: string; category?: string }) => download('/resources/export', q, '资源台账.xlsx')

// ---- 报表 ----
/** tenant_id：all = 全部（含合并报表）/ global = 仅全部租户合并报表 / 租户 ID */
export const listReports = (q: { kind?: ReportKind; tenant_id?: string; page?: number; page_size?: number }) => get<Page<ReportBrief>>('/reports', q)
export const getReport = (id: number) => get<ReportDetail>(`/reports/${id}`)
/** tenant_id 不传：有全部租户权限时生成合并报表，否则生成自己租户的报表 */
export const createReport = (kind: ReportKind, date?: string, tenant_id?: number | null) =>
  post<ReportDetail>('/reports', { kind, date, tenant_id: tenant_id ?? undefined }, { timeout: 60000 })
export const sendReport = (id: number, to: string[]) => post<ReportBrief>(`/reports/${id}/send`, { to }, { timeout: 60000 })
export const deleteReport = (id: number) => del<null>(`/reports/${id}`)
export const exportReport = (id: number) => download(`/reports/${id}/export`, undefined, '报表.xlsx')
export interface ReportSchedule {
  cron: Record<string, string>; enabled: Record<string, boolean>; mail_to: string[]; timezone: string; kinds: Record<string, string>
  jobs: { kind: string; name: string; cron: string; enabled: boolean; next_run: string | null }[]
  tenants: { id: number; name: string; mail_to: string[]; enabled: boolean }[]; note: string
  editable: boolean; updated_by: string | null; updated_at: string | null; customized: boolean
}
export const reportSchedule = () => get<ReportSchedule>('/reports/schedule')
export const saveReportSchedule = (body: { cron?: Record<string, string>; enabled?: Record<string, boolean>; mail_to?: string[] }) =>
  put<ReportSchedule>('/reports/schedule', body)

// ---- 规则 ----
export const listRules = (resource_type?: string) => get<Rule[]>('/rules', { resource_type })
export const saveRule = (id: string, body: Partial<Rule>) => put<Rule>(`/rules/${id}`, body)
export const dryRunRule = (body: { kind: string; expr: object; recover: object; facts: object }) =>
  post<DryRunResult>('/rules/dry-run', body)

// ---- 通知 ----
export interface NotifyRoute {
  id: number; name: string; priority: number; match: { services?: string[]; severities?: string[]; tenant_ids?: number[]; resource_types?: string[] }
  group_code: string; group_name: string | null; member_count: number
  tenant_names: string[]; escalation: { after_min: number; to: string; severities?: string[] }[]; continue_match: boolean; enabled: boolean
}
export interface ContactGroup {
  id: number; code: string; name: string; member_ids: number[]; leader_ids: number[]; routes: string[]
  members: { id: number; name: string; email: string; enabled?: boolean }[]; leaders: { id: number; name: string; email: string; enabled?: boolean }[]
}
export const listRoutes = () => get<NotifyRoute[]>('/notify/routes')
export const listGroups = () => get<ContactGroup[]>('/notify/groups')
export const listNotifications = (status?: string) => get<NotificationRow[]>('/notify/notifications', { status })
export const previewMail = (alarmId: number, kind = 'FIRING') =>
  get<{ subject: string; html: string; text: string; recipients: string[]; routes: string[] }>(`/notify/preview/${alarmId}`, { kind })

// ---- 容器事件 ----
export interface PodEventQuery {
  cluster_id?: string; namespace?: string; workload?: string; pod?: string; keyword?: string; event_type?: string[]; level?: string[]
  source?: string; ongoing?: boolean; hours?: number; page?: number; page_size?: number
}
export const listPodEvents = (q: PodEventQuery) => get<Page<PodEvent>>('/pod-events', q)
export const podEventStats = (q: { hours?: number; cluster_id?: string; namespace?: string }) => get<PodEventStats>('/pod-events/stats', q)
export const podEventOptions = () => get<PodEventOptions>('/pod-events/options')
/** key = 集群 ID / 命名空间 / Pod 名 */
export const podTimeline = (key: string, days = 7) => get<PodTimeline>('/pod-events/pod', { key, days })
export const listProbes = () => get<Probe[]>('/pod-events/probes')
export const createProbe = (body: ProbeIn) => post<Probe>('/pod-events/probes', body)
export const updateProbe = (id: number, body: ProbeIn) => put<Probe>(`/pod-events/probes/${id}`, body)
export const deleteProbe = (id: number) => del<null>(`/pod-events/probes/${id}`)
export const collectPodEvents = () =>
  post<{ status: string; runs: { run_id: number; tenant_id: number; tenant_name: string; status: string; events: number; error_summary: string | null }[] }>(
    '/pod-events/collect', undefined, { timeout: 60000 })

// ---- 系统：平台信息 / 巡检任务 ----
export interface TaskRun {
  id: number; tenant_id: number | null; tenant_name: string | null; trigger_type: string; started_at: string; finished_at: string | null
  status: string; resource_total: number; abnormal_total: number; api_calls: number; error_summary: string | null; stats: Record<string, any>
}
export interface InspectionTask {
  code: string; name: string; service: string; interval_sec: number; cron: string | null; enabled: boolean; params: object; runs: TaskRun[]
  next_run: string | null; next_run_source: 'scheduler' | 'estimate'; types: string[]; note: string | null; min_interval: number
  skipped_tenants: string[]
}
export interface TaskRunResult {
  status: string; skipped?: string[]
  runs: { run_id: number; tenant_id: number | null; tenant_name: string | null; status: string; resource_total: number; abnormal_total: number; api_calls: number; error_summary: string | null }[]
}
export const systemInfo = () => get<SystemInfo>('/system/info')
export const listTasks = () => get<InspectionTask[]>('/system/tasks')
export const runTask = (code: string) => post<TaskRunResult>(`/system/tasks/${code}/run`, undefined, { timeout: 120000 })
/** cron 为空字符串 = 改回按周期执行 */
export const updateTask = (code: string, body: { interval_sec?: number; cron?: string | null; enabled?: boolean }) =>
  put<{ code: string; interval_sec: number; cron: string | null; enabled: boolean; changed: boolean }>(`/system/tasks/${code}`, body)

// ---- 系统：租户接入（PASSWORD = IAM 用户名密码换 Token；AKSK = AK/SK 签名调用）----
export const listTenants = () => get<Tenant[]>('/system/tenants')
export const createTenant = (body: TenantIn) => post<Tenant>('/system/tenants', body)
export const updateTenant = (id: number, body: TenantIn) => put<Tenant>(`/system/tenants/${id}`, body)
export const deleteTenant = (id: number) => del<null>(`/system/tenants/${id}`)
export const testTenant = (id: number) => post<TenantCheckResult & { tenant: Tenant }>(`/system/tenants/${id}/test`, undefined, { timeout: 60000 })

// ---- 系统：用户 / 角色 / 权限 / 审计 ----
export interface UserCreate { username: string; display_name: string; email?: string; role_id: number; tenant_ids: number[] | null; password: string }
export interface UserPatch { display_name?: string; email?: string | null; role_id?: number; tenant_ids?: number[] | null; all_tenants?: boolean; enabled?: boolean }
export const listUsers = () => get<UserAdmin[]>('/system/users')
export const createUser = (body: UserCreate) => post<UserAdmin>('/system/users', body)
export const patchUser = (id: number, body: UserPatch) => patch<UserAdmin>(`/system/users/${id}`, body)
export const resetPassword = (id: number, password: string) => post<null>(`/system/users/${id}/reset-password`, { password })
export const listPermissions = () => get<PermissionGroup[]>('/system/permissions')
export const listRoles = () => get<Role[]>('/system/roles')
export const createRole = (body: { code: string; name: string; description?: string; permissions: string[] }) => post<Role>('/system/roles', body)
export const patchRole = (id: number, body: { name?: string; description?: string; permissions?: string[] }) => patch<Role>(`/system/roles/${id}`, body)
export const deleteRole = (id: number) => del<null>(`/system/roles/${id}`)
export interface AuditQuery { action?: string[]; keyword?: string; result?: string; days?: number; page?: number; page_size?: number }
export const listAuditLogs = (q: AuditQuery) =>
  get<Page<AuditLog> & { actions: { code: string; name: string }[] }>('/system/audit-logs', q)

// ================================================================ v0.4
// ---- 静默与维护窗口 ----
export type SilenceTab = 'current' | 'active' | 'pending' | 'expired' | 'cancelled' | 'all'
export const listSilences = (status: SilenceTab = 'current') =>
  get<{ items: Silence[]; counts: Record<SilenceStatus, number> }>('/silences', { status })
export const previewSilence = (matchers: SilenceMatchers) => post<SilencePreview>('/silences/preview', { matchers }, { silent: true })
export const createSilence = (body: SilenceIn) => post<Silence & { suppressed: number }>('/silences', body)
export const updateSilence = (id: number, body: SilenceIn) => put<Silence & { released: number; suppressed: number }>(`/silences/${id}`, body)
export const cancelSilence = (id: number) => post<Silence & { released: number }>(`/silences/${id}/cancel`)
/** 快速静默：alarm = 只这条告警；resource = 该资源全部告警；rule = 该租户下这条规则 */
export const quickSilence = (alarmId: number, body: { minutes: number; target: 'alarm' | 'resource' | 'rule'; reason?: string }) =>
  post<{ silence: Silence; suppressed: number }>(`/alarms/${alarmId}/silence`, body)

// ---- 采集开关 ----
export const getCollectSwitches = () => get<CollectSwitches>('/system/collect-switches')
export const switchImpact = (items: SwitchItem[]) => post<SwitchImpact>('/system/collect-switches/impact', { items })
export const saveCollectSwitches = (items: SwitchItem[]) =>
  put<{ changed: { tenant_id: number; resource_type: string; name: string; old: boolean | null; new: boolean | null }[]; closed_alarms: number }>(
    '/system/collect-switches', { items })

// ---- 系统设置 / 平台状态 ----
export const getSettings = () => get<SystemSettings>('/system/settings')
export const saveSetting = <T = Record<string, any>>(key: string, body: Partial<T>) =>
  put<{ value: T; changed: string[] } & Omit<SettingItem<T>, 'value'>>(`/system/settings/${key}`, body)
export const platformStatus = () => get<PlatformStatus>('/system/platform')

// ---- 台账字典 / Excel 导入 ----
export const getLedgerDict = () => get<LedgerDict>('/resources/dict')
export const saveLedgerDict = (body: LedgerDictSetting) => put<LedgerDictSetting>('/resources/dict', body)
export const downloadImportTemplate = (q: { resource_type?: string; category?: string }) =>
  download('/resources/import/template', q, '台账管理字段导入模板.xlsx')
export function importLedger(file: File, dryRun: boolean, skipErrors = false) {
  const fd = new FormData()
  fd.append('file', file)
  return post<ImportResult>('/resources/import', fd, { params: { dry_run: dryRun, skip_errors: skipErrors }, timeout: 120000, silent: true })
}

// ---- 通知配置维护 ----
export const listContacts = () => get<Contact[]>('/notify/contacts')
export const createContact = (body: ContactIn) => post<{ id: number }>('/notify/contacts', body)
export const updateContact = (id: number, body: ContactIn) => put<{ id: number }>(`/notify/contacts/${id}`, body)
export const deleteContact = (id: number) => del<{ removed_from: string[] }>(`/notify/contacts/${id}`)
export const createGroup = (body: GroupIn) => post<{ id: number }>('/notify/groups', body)
export const updateGroup = (id: number, body: GroupIn) => put<{ id: number }>(`/notify/groups/${id}`, body)
export const deleteGroup = (id: number) => del<null>(`/notify/groups/${id}`)
export const createRoute = (body: RouteIn) => post<{ id: number }>('/notify/routes', body)
export const updateRoute = (id: number, body: RouteIn) => put<{ id: number }>(`/notify/routes/${id}`, body)
export const deleteRoute = (id: number) => del<null>(`/notify/routes/${id}`)
export const reorderRoutes = (ids: number[]) => post<null>('/notify/routes/reorder', { ids })
export const simulateRoute = (body: { tenant_id?: number | null; service: string; resource_type?: string | null; severity: string; rule_id?: string; resource_name?: string }) =>
  post<SimulateResult>('/notify/simulate', body)
export const smtpInfo = () => get<SmtpInfo>('/notify/smtp')
export const sendTestMail = (to: string[]) => post<{ sent_to: string[]; elapsed_ms: number }>('/notify/test-mail', { to }, { timeout: 60000 })

// ---- 实时推送：一次性票据（EventSource 不能带请求头，登录令牌不放进 URL）----
export const streamTicket = () => post<{ ticket: string; expires_in: number }>('/stream/ticket', undefined, { silent: true })
