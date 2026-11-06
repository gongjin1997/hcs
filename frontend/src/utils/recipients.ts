import type { UserOption } from '../types'

export function recipientUnavailable(user: UserOption, tenantId: number | null, globalOnly = false) {
  if (!user.enabled) return '已停用'
  if (!user.notify_enabled) return '通知已关闭'
  if (!user.email?.trim()) return '缺少邮箱'
  if (globalOnly && user.tenant_ids != null) return '无全部租户权限'
  if (tenantId != null && user.tenant_ids != null && !user.tenant_ids.includes(tenantId)) return '无此租户权限'
  return ''
}

export function recipientRows(users: UserOption[], selected: number[], tenantId: number | null, globalOnly = false) {
  const rows = users.map(user => ({ id: user.id, label: `${user.display_name}（${user.username}）`, email: user.email,
    reason: recipientUnavailable(user, tenantId, globalOnly) }))
  for (const id of selected) if (!rows.some(row => row.id === id)) {
    rows.push({ id, label: `#${id}`, email: null, reason: '当前不可选' })
  }
  return rows
}
