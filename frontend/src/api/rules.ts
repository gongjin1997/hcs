import { get, post, put } from './request'
import type { Rule } from '@/types'
import type { RuleCreate, RuleDryRun, RuleDryRunResult, RuleTarget, RuleUpdate } from '@/types/rules'

export const listRules = (resource_type?: string) => get<Rule[]>('/rules', { resource_type })
export const getRuleTargets = () => get<RuleTarget[]>('/rules/targets')
export const createRule = (body: RuleCreate) => post<Rule>('/rules', body)
export const saveRule = (id: string, body: RuleUpdate) => put<Rule>(`/rules/${encodeURIComponent(id)}`, body)
export const dryRunRule = (body: RuleDryRun) => post<RuleDryRunResult>('/rules/dry-run', body)
