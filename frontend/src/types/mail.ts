/** 与 backend/app/schemas/mail.py 对齐；密码只写入，读取仅有 password_set。 */
export type MailSecurity = 'NONE' | 'STARTTLS' | 'SSL'

export interface SmtpConfig {
  host: string
  port: number
  security: MailSecurity
  auth: boolean
  username: string
  sender: string
  reply_to: string
}

export interface ImapConfig {
  enabled: boolean
  host: string
  port: number
  security: MailSecurity
  username: string
}

export interface PasswordUpdate {
  password?: string
  clear_password?: boolean
}

export interface MailSettings {
  smtp: SmtpConfig & { password_set: boolean }
  imap: ImapConfig & { password_set: boolean }
  source: 'database' | 'environment'
  editable: boolean
  updated_by: string | null
  updated_at: string | null
}

export interface MailSettingsUpdate {
  smtp?: Partial<SmtpConfig> & PasswordUpdate
  imap?: Partial<ImapConfig> & PasswordUpdate
}

export interface MailConnectionResult {
  protocol: 'SMTP' | 'IMAP'
  ok: boolean
  elapsed_ms: number
  message: string
}

export interface TestMailResult {
  sent_to: string[]
  elapsed_ms: number
}
