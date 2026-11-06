import type { SmsSeverity, SmsTenantConfig, SmsTenantUpdate } from '@/types/sms'

export interface SmsTenantDraft {
  enabled: boolean; endpoint: string; username: string; password: string; clear_password: boolean
  expand_port: string; verify_tls: boolean; severities: SmsSeverity[]
}
export const blankSmsDraft = (): SmsTenantDraft => ({
  enabled: false, endpoint: '', username: '', password: '', clear_password: false,
  expand_port: '', verify_tls: true, severities: ['P1', 'P2'],
})
export function validateSmsDraft(form: SmsTenantDraft, passwordSet: boolean): string {
  if (form.enabled || form.endpoint.trim()) {
    try {
      const url = new URL(form.endpoint.trim())
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || /[\s{}]/.test(form.endpoint.trim())) throw new Error()
    } catch { return '请填写完整的 HTTP / HTTPS 网关或代理地址，账号和密码单独填写' }
  }
  if (form.enabled && !form.username.trim()) return '启用短信须填写独立的短信账号'
  if (form.password && /^\*+$/.test(form.password)) return '请填写实际短信密码，留空保留原密码'
  if (form.enabled && (form.clear_password || (!passwordSet && !form.password))) return '启用短信须设置密码，且不能同时清除密码'
  if (form.enabled && !form.severities.length) return '启用短信须至少选择一个告警级别'
  if (form.severities.some(level => !['P1', 'P2', 'P3'].includes(level))) return '短信级别只能选择 P1、P2 或 P3'
  if (form.expand_port.trim() && !/^[0-9]{1,14}$/.test(form.expand_port.trim())) return '扩展端口只能填写不超过 14 位数字'
  return ''
}
export function smsUpdate(form: SmsTenantDraft): SmsTenantUpdate {
  return {
    enabled: form.enabled, endpoint: form.endpoint.trim(), username: form.username.trim(),
    expand_port: form.expand_port.trim(), verify_tls: form.verify_tls, severities: [...form.severities],
    clear_password: form.clear_password, ...(form.password && !form.clear_password ? { password: form.password } : {}),
  }
}
export function applySmsConfig(form: SmsTenantDraft, config: SmsTenantConfig) {
  Object.assign(form, { enabled: config.enabled === true, endpoint: config.endpoint, username: config.username,
    password: '', clear_password: false, expand_port: config.expand_port,
    verify_tls: config.verify_tls !== false, severities: [...config.severities] })
}
export const smsStatusText = (status: string) => ({
  ACCEPTED: '网关已受理', GATEWAY_ACCEPTED: '网关已受理', SENT: '网关已受理', SUCCESS: '网关已受理',
  PENDING: '待处理', FAILED: '失败', SKIPPED: '已跳过', CANCELLED: '已取消', UNKNOWN: '结果待核对', UNCERTAIN: '结果待核对',
}[status.toUpperCase()] ?? '结果待核对')
