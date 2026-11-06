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
