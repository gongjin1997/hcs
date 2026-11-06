export type SmsSeverity = 'P1' | 'P2' | 'P3'

export interface SmsGlobalConfig { enabled: boolean; enabled_since?: string | null }

export interface SmsTenantConfig {
  enabled: boolean
  endpoint: string
  username: string
  password_set: boolean
  expand_port: string
  verify_tls: boolean
  severities: SmsSeverity[]
  revision: number
  enabled_since?: string | null
}

export interface SmsTenantUpdate extends Omit<SmsTenantConfig, 'password_set' | 'enabled_since' | 'revision'> {
  password?: string
  clear_password?: boolean
}

export interface SmsNotification {
  id: number
  status: string
  alarm_id: number | null
  user_id: number | null
  tenant_id: number
  phone_mask: string
  created_at: string
  sent_at: string | null
  error: string | null
  gateway_message_id: string | null
  gateway_code?: string | null
  claimed_at?: string | null
}

/** 系统配置页的租户短信概况；不含网关地址、账号与密码。 */
export interface SmsTenantSummary {
  tenant_id: number
  tenant_name: string
  short_name: string
  tenant_enabled: boolean
  enabled: boolean
  configured: boolean
  severities: SmsSeverity[]
  /** 总开关、租户开关与凭据都就绪，当前会发送 */
  effective: boolean
}
export interface SmsTenantSummaries { global_enabled: boolean; items: SmsTenantSummary[] }
