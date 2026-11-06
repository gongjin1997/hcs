import { describe, expect, it } from 'vitest'
import type { ScopeDimension, ScopeRule } from '../src/types'
import {
  describeRule, draftErrors, matchesPattern, newDraft, sameRules, scopeSummary, toDrafts, toPayload, type RuleDraft,
} from '../src/views/system/collect-scope'

const limits = { rules: 100, patterns: 200, pattern_length: 200 }
const dims: ScopeDimension[] = [{ key: 'namespace', label: '命名空间' }, { key: 'workload', label: '工作负载' }]

describe('采集范围编辑器', () => {
  it('提交内容去空白、去重，丢弃空维度与没有条件的规则', () => {
    const drafts: RuleDraft[] = [
      { effect: 'exclude' as const, match: { namespace: [' test-* ', 'test-*', '', 'dev'], workload: [] }, note: ' 测试环境 ' },
      { effect: 'include' as const, match: { namespace: ['  '] }, note: '' },
    ]
    expect(toPayload(drafts)).toEqual([{ effect: 'exclude', match: { namespace: ['test-*', 'dev'] }, note: '测试环境' }])
  })

  it('草稿是深拷贝，修改草稿不影响已保存的规则', () => {
    const saved: ScopeRule[] = [{ effect: 'exclude', match: { namespace: ['dev'] } }]
    const drafts = toDrafts(saved)
    drafts[0].match.namespace.push('test-*')
    expect(saved[0].match.namespace).toEqual(['dev'])
    expect(drafts[0].note).toBe('')
  })

  it('校验：没有条件的规则、条数与长度上限', () => {
    expect(draftErrors([newDraft()], limits)).toEqual(['第 1 条规则还没有填写任何条件'])
    expect(draftErrors([{ effect: 'exclude', match: { namespace: ['x'.repeat(201)] }, note: '' }], limits)).toEqual([
      '第 1 条规则的“namespace”单个值不超过 200 个字符'])
    const many = Array.from({ length: 101 }, () => ({ effect: 'exclude' as const, match: { namespace: ['a'] }, note: '' }))
    expect(draftErrors(many, limits)).toEqual(['最多 100 条规则'])
    expect(draftErrors([{ effect: 'exclude', match: { namespace: ['a'] }, note: '' }], limits)).toEqual([])
  })

  it('比较规则时忽略维度顺序，区分取值顺序与备注', () => {
    const a: ScopeRule[] = [{ effect: 'exclude', match: { namespace: ['a'], workload: ['w'] } }]
    const b: ScopeRule[] = [{ effect: 'exclude', match: { workload: ['w'], namespace: ['a'] } }]
    expect(sameRules(a, b)).toBe(true)
    expect(sameRules(a, [{ ...b[0], note: '备注' }])).toBe(false)
    expect(sameRules(a, [{ effect: 'include', match: b[0].match }])).toBe(false)
  })

  it('规则的可读描述使用维度名称', () => {
    expect(describeRule({ effect: 'exclude', match: { namespace: ['test-*', 'dev'], workload: ['web-*'] } }, dims))
      .toBe('排除：命名空间 = test-*、dev；工作负载 = web-*')
    expect(describeRule({ effect: 'include', match: { x: ['1'] } }, dims)).toBe('仅采集：x = 1')
  })

  it('矩阵单元格摘要：未设置 / 全局 / 租户 / 范围外数量', () => {
    expect(scopeSummary(undefined)).toEqual({ text: '全部采集', tone: 'all' })
    expect(scopeSummary({ source: 'none', rules: [], excluded: 0 })).toEqual({ text: '全部采集', tone: 'all' })
    expect(scopeSummary({ source: 'tenant', rules: [], excluded: 0 })).toEqual({ text: '不限制', tone: 'all' })
    const rule: ScopeRule = { effect: 'exclude', match: { namespace: ['a'] } }
    expect(scopeSummary({ source: 'tenant', rules: [rule, rule], excluded: 3 })).toEqual({ text: '2 条规则 · 范围外 3', tone: 'limited' })
    expect(scopeSummary({ source: 'global', rules: [rule], excluded: 0 })).toEqual({ text: '全局 · 1 条规则', tone: 'limited' })
  })

  it('通配与后端一致：只有 * 和 ?，区分大小写，空值不匹配', () => {
    expect(matchesPattern('test-*', 'test-1')).toBe(true)
    expect(matchesPattern('test-*', 'Test-1')).toBe(false)
    expect(matchesPattern('a?c', 'abc')).toBe(true)
    expect(matchesPattern('a?c', 'ac')).toBe(false)
    expect(matchesPattern('*', '')).toBe(false)
    expect(matchesPattern('[x]*', '[x]yz')).toBe(true)
    expect(matchesPattern('[x]*', 'xyz')).toBe(false)
    expect(matchesPattern('prod', 'prod')).toBe(true)
  })
})
