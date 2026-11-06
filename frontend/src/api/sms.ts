import { get, put } from './request'
import type { SmsGlobalConfig, SmsNotification, SmsTenantConfig, SmsTenantSummaries, SmsTenantUpdate } from '@/types/sms'

const globalConfig = { silent: true, headers: { 'X-Tenant-Id': 'all' } }
const tenantConfig = (tenantId: number) => ({ silent: true, headers: { 'X-Tenant-Id': String(tenantId) } })

export const getSmsGlobal = () => get<SmsGlobalConfig>('/system/sms', undefined, globalConfig)
export const saveSmsGlobal = (enabled: boolean) => put<SmsGlobalConfig>('/system/sms', { enabled }, globalConfig)
/** 当前账号可管理的各租户短信概况（系统配置 → 升级短信）。 */
export const listTenantSms = () => get<SmsTenantSummaries>('/system/sms/tenants', undefined, globalConfig)
export const getTenantSms = (tenantId: number) => get<SmsTenantConfig>(`/system/tenants/${tenantId}/sms`, undefined, tenantConfig(tenantId))
export const saveTenantSms = (tenantId: number, body: SmsTenantUpdate) => put<SmsTenantConfig>(`/system/tenants/${tenantId}/sms`, body, tenantConfig(tenantId))
export const getSmsNotifications = (tenantId: number) => get<SmsNotification[]>(
  '/system/sms/notifications', { tenant_id: tenantId }, tenantConfig(tenantId),
)
