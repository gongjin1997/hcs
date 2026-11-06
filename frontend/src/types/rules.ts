import type { Rule, Severity } from './index'

export interface RuleWrite {
  name: string
  kind: Rule['kind']
  scope: Record<string, unknown>
  expr: Record<string, unknown>
  for_count: number
  recover: Record<string, unknown>
  repeat_interval_sec: number | null
  runbook: string | null
  enabled: boolean
}

export interface RuleCreate extends RuleWrite {
  id: string
  resource_type: string
  check_item: string
}

export type RuleUpdate = RuleWrite & { version?: number }

export interface RuleFactField {
  key: string
  label: string
  unit: string | null
  meaning: string
  source: string
  data_type: 'number' | 'integer' | 'string' | 'boolean' | 'object' | 'array'
  optional: boolean
  availability: 'collected' | 'derived' | 'not_collected'
  check_items: string[]
  limitations: string[]
}

export interface RuleExample {
  kind: Rule['kind']
  expr: Record<string, unknown>
  recover: Record<string, unknown>
}

export interface RuleTarget {
  resource_type: string
  name: string
  service: string
  check_item: string
  collection_tasks: string[]
  facts: RuleFactField[]
  expression_examples: RuleExample[]
  limitations: string[]
  collection_available?: boolean
}

export interface RuleDryRun {
  kind: Rule['kind']
  expr: Record<string, unknown>
  recover: Record<string, unknown>
  facts: Record<string, unknown>
  resource_type?: string
  check_item?: string
}

export interface RuleDryRunResult {
  matched: boolean
  severity: Severity | null
  recovered: boolean
  message: string
}
