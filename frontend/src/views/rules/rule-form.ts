import type { RuleLevel, Severity } from '../../types'

const severities: Severity[] = ['P1', 'P2', 'P3', 'P4']

export function nextRuleLevel(levels: RuleLevel[]): RuleLevel | null {
  const available = severities.filter(severity => !levels.some(level => level.severity === severity))
  const last = levels[levels.length - 1]
  const severity = last
    ? available.find(value => severities.indexOf(value) > severities.indexOf(last.severity)) ?? available[0]
    : available[0]
  return severity ? { severity, op: last?.op ?? '>=', threshold: (last?.threshold ?? 85) - 5 } : null
}
