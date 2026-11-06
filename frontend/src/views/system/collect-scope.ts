import type { ScopeCell, ScopeDimension, ScopeEffect, ScopeRule } from '../../types'

/** 采集范围编辑器的纯逻辑：草稿 ↔ 提交内容、校验、文字描述。组件只负责展示。 */
export interface RuleDraft { effect: ScopeEffect; match: Record<string, string[]>; note: string }

export const EFFECT_OPTIONS: { value: ScopeEffect; label: string; hint: string }[] = [
  { value: 'exclude', label: '排除', hint: '命中的对象不采集、不评估、不告警' },
  { value: 'include', label: '仅采集', hint: '只采集命中的对象，其余都不采集' },
]
export const effectLabel = (effect: ScopeEffect) => (effect === 'include' ? '仅采集' : '排除')

/** 各维度的填写提示（占位文字）。 */
export const DIMENSION_HINTS: Record<string, string> = {
  name: '名称，如 redis-prod-* ', id: '资源 ID', cluster: '集群名称或 ID', namespace: '命名空间，如 test-*',
  workload: '控制器名称；Deployment 的 Pod 由 ReplicaSet 管理，名称带随机后缀，建议 web-* 通配',
  workspace: '工作空间名称或 ID', job: '作业名称，如 tmp_*', node: '节点名称',
  env: 'PROD / TEST / DEV', business: '业务系统（台账字段）', owner: '负责人（台账字段）', critical: 'true 或 false',
}

export const newDraft = (effect: ScopeEffect = 'exclude'): RuleDraft => ({ effect, match: {}, note: '' })

export const toDrafts = (rules: ScopeRule[]): RuleDraft[] => rules.map(rule => ({
  effect: rule.effect, match: Object.fromEntries(Object.entries(rule.match).map(([dim, values]) => [dim, [...values]])),
  note: rule.note ?? '',
}))

const clean = (values: string[]) => [...new Set(values.map(value => value.trim()).filter(Boolean))]

/** 提交内容：去空白、去重，丢弃没有取值的维度；没有任何条件的规则被丢弃（由 draftErrors 先提示）。 */
export function toPayload(drafts: RuleDraft[]): ScopeRule[] {
  const rules: ScopeRule[] = []
  for (const draft of drafts) {
    const match: Record<string, string[]> = {}
    for (const [dim, values] of Object.entries(draft.match)) {
      const cleaned = clean(values)
      if (cleaned.length) match[dim] = cleaned
    }
    if (!Object.keys(match).length) continue
    const note = draft.note.trim()
    rules.push({ effect: draft.effect, match, ...(note ? { note } : {}) })
  }
  return rules
}

export function draftErrors(drafts: RuleDraft[], limits: { rules: number; patterns: number; pattern_length: number }): string[] {
  const errors: string[] = []
  if (drafts.length > limits.rules) errors.push(`最多 ${limits.rules} 条规则`)
  drafts.forEach((draft, index) => {
    const no = index + 1
    const dims = Object.entries(draft.match).filter(([, values]) => clean(values).length)
    if (!dims.length) errors.push(`第 ${no} 条规则还没有填写任何条件`)
    for (const [dim, values] of dims) {
      if (clean(values).length > limits.patterns) errors.push(`第 ${no} 条规则的“${dim}”最多 ${limits.patterns} 个值`)
      if (clean(values).some(value => value.length > limits.pattern_length)) errors.push(`第 ${no} 条规则的“${dim}”单个值不超过 ${limits.pattern_length} 个字符`)
    }
  })
  return errors
}

const stable = (rules: ScopeRule[]) => JSON.stringify(rules.map(rule => ({
  effect: rule.effect, note: rule.note ?? '',
  match: Object.keys(rule.match).sort().map(dim => [dim, rule.match[dim]]),
})))
export const sameRules = (a: ScopeRule[], b: ScopeRule[]) => stable(a) === stable(b)

const label = (dims: ScopeDimension[], key: string) => dims.find(dim => dim.key === key)?.label ?? key

/** 排除：命名空间 = test-*、dev ｜ 仅采集：工作空间 = ws1 */
export const describeRule = (rule: ScopeRule, dims: ScopeDimension[]) =>
  `${effectLabel(rule.effect)}：${Object.entries(rule.match).map(([dim, values]) => `${label(dims, dim)} = ${values.join('、')}`).join('；')}`

export type ScopeTone = 'all' | 'limited'
/** 采集开关矩阵里每个单元格的范围摘要。 */
export function scopeSummary(cell: Pick<ScopeCell, 'source' | 'rules' | 'excluded'> | undefined): { text: string; tone: ScopeTone } {
  if (!cell || (cell.source === 'none')) return { text: '全部采集', tone: 'all' }
  const base = cell.rules.length ? `${cell.rules.length} 条规则` : '不限制'
  const text = `${cell.source === 'global' ? '全局 · ' : ''}${base}${cell.excluded ? ` · 范围外 ${cell.excluded}` : ''}`
  return { text, tone: cell.rules.length ? 'limited' : 'all' }
}

/** 通配匹配（与后端一致：只有 * 和 ? 是通配，区分大小写，空值不匹配任何模式）。仅用于“试一试”提示。 */
export function matchesPattern(pattern: string, value: string): boolean {
  if (!value) return false
  if (!pattern.includes('*') && !pattern.includes('?')) return pattern === value
  let p = 0, v = 0, star = -1, mark = 0
  while (v < value.length) {
    if (p < pattern.length && pattern[p] === '*') { star = p; mark = v; p++ }
    else if (p < pattern.length && (pattern[p] === '?' || pattern[p] === value[v])) { p++; v++ }
    else if (star !== -1) { p = star + 1; mark++; v = mark }
    else return false
  }
  while (p < pattern.length && pattern[p] === '*') p++
  return p === pattern.length
}
