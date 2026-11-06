export interface ManageOneConfig {
  tenant_id: number
  enabled: boolean
  configured: boolean
  selected: boolean
  endpoint: string | null
  username: string | null
  organization: string | null
  secret_set: boolean
}

export interface ManageOneConfigIn {
  enabled: boolean
  endpoint: string
  username: string
  organization: string
  password?: string
  clear_password?: boolean
  select_source?: boolean
}

export interface ManageOneRequestContext { signal: AbortSignal }
export type ManageOneFetcher = (tenantId: number, context: ManageOneRequestContext) => Promise<ManageOneConfig>
export type ManageOneSaver = (tenantId: number, body: ManageOneConfigIn, context: ManageOneRequestContext) => Promise<void>
