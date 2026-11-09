export type ManageOneAuthType = 'PASSWORD' | 'AKSK'

export interface ManageOneConfig {
  tenant_id: number
  enabled: boolean
  configured: boolean
  selected: boolean
  endpoint: string | null
  auth_type: ManageOneAuthType
  access_key_mask: string | null
  secret_key_set: boolean
  username: string | null
  organization: string | null
  secret_set: boolean
}

export interface ManageOneConfigIn {
  enabled: boolean
  endpoint: string
  auth_type: ManageOneAuthType
  username?: string
  organization?: string
  password?: string
  clear_password?: boolean
  access_key?: string
  secret_key?: string
  clear_secret_key?: boolean
  select_source?: boolean
}

export interface ManageOneRequestContext { signal: AbortSignal }
export type ManageOneFetcher = (tenantId: number, context: ManageOneRequestContext) => Promise<ManageOneConfig>
export type ManageOneSaver = (tenantId: number, body: ManageOneConfigIn, context: ManageOneRequestContext) => Promise<void>
