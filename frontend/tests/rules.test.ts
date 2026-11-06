import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, type EffectScope } from 'vue'
import type { Rule, RuleLevel } from '../src/types'
import type { RuleDryRun, RuleDryRunResult, RuleTarget } from '../src/types/rules'
import { useRuleCatalog } from '../src/views/rules/useRuleCatalog'
import { useRuleDryRun } from '../src/views/rules/useRuleDryRun'
import { nextRuleLevel } from '../src/views/rules/rule-form'

const scopes: EffectScope[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })

function pending<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const rule: Rule = {
  id: 'CUSTOM_CPU', name: 'CPU', resource_type: 'ecs_server', check_item: 'cpu', kind: 'threshold',
  expr: { value: 'cpu_util', levels: [{ severity: 'P2', op: '>=', threshold: 90 }] },
  scope: {}, recover: {}, for_count: 1, enabled: false, version: 1, repeat_interval_sec: null,
  runbook: null, updated_by: 'QA', updated_at: null,
}
const target: RuleTarget = {
  resource_type: 'ecs_server', service: 'ecs', name: 'ECS', check_item: 'cpu', collection_tasks: ['ecs_status'],
  facts: [], expression_examples: [], limitations: [],
}
const dryBody: RuleDryRun = { kind: 'threshold', expr: rule.expr, recover: {}, facts: { cpu_util: 95 }, resource_type: 'ecs_server', check_item: 'cpu' }
const hit: RuleDryRunResult = { matched: true, severity: 'P2', recovered: false, message: 'CPU 95%' }

function catalog() {
  const requests: { signal: AbortSignal; rows: ReturnType<typeof pending<Rule[]>>; targets: ReturnType<typeof pending<RuleTarget[]>> }[] = []
  const scope = effectScope()
  scopes.push(scope)
  const model = scope.run(() => useRuleCatalog({
    rules(signal) {
      const request = { signal, rows: pending<Rule[]>(), targets: pending<RuleTarget[]>() }
      requests.push(request)
      return request.rows.promise
    },
    targets() { return requests[requests.length - 1].targets.promise },
  }))!
  void model.load()
  return { model, requests, scope }
}

function dryRun() {
  const requests: { signal: AbortSignal; response: ReturnType<typeof pending<RuleDryRunResult>> }[] = []
  const scope = effectScope()
  scopes.push(scope)
  const model = scope.run(() => useRuleDryRun((_body, signal) => {
    const response = pending<RuleDryRunResult>()
    requests.push({ signal, response })
    return response.promise
  }))!
  return { model, requests, scope }
}

describe('规则列表加载', () => {
  it('检查项目录失败仍能查看规则，并给出独立错误供重试', async () => {
    const { model, requests } = catalog()
    requests[0].rows.resolve([rule])
    requests[0].targets.reject(new Error('检查项不可用'))
    await nextTick()
    await nextTick()
    expect(model.rules.value).toEqual([rule])
    expect(model.targets.value).toEqual([])
    expect(model.targetError.value).toBe('检查项不可用')
    expect(model.loadError.value).toBe('')
    expect(model.loading.value).toBe(false)
    const retry = model.load()
    requests[1].rows.resolve([rule])
    requests[1].targets.resolve([target])
    await retry
    expect(model.targets.value).toEqual([target])
    expect(model.targetError.value).toBe('')
  })

  it('首次加载失败给出错误，不显示为空目录或未捕获异常', async () => {
    const { model, requests } = catalog()
    requests[0].rows.reject(new Error('规则不可用'))
    requests[0].targets.resolve([target])
    await nextTick(); await nextTick()
    expect(model.rules.value).toEqual([])
    expect(model.loadError.value).toBe('规则不可用')
    expect(model.targetError.value).toBe('')
    expect(model.loading.value).toBe(false)
  })

  it('刷新失败保留已加载规则', async () => {
    const { model, requests } = catalog()
    requests[0].rows.resolve([rule]); requests[0].targets.resolve([target])
    await nextTick(); await nextTick()
    const refresh = model.load()
    requests[1].rows.reject(new Error('刷新失败')); requests[1].targets.resolve([target])
    await refresh
    expect(model.rules.value).toEqual([rule])
    expect(model.loadError.value).toBe('刷新失败')
    expect(model.loading.value).toBe(false)
  })

  it('重叠刷新取消旧请求，不让旧成功或失败覆盖当前列表', async () => {
    const { model, requests } = catalog()
    const refresh = model.load()
    expect(requests[0].signal.aborted).toBe(true)
    requests[0].rows.resolve([rule]); requests[0].targets.reject(new Error('旧错误'))
    await nextTick(); await nextTick()
    expect(model.loading.value).toBe(true)
    expect(model.rules.value).toEqual([])
    expect(model.targetError.value).toBe('')
    requests[1].rows.resolve([{ ...rule, version: 2 }]); requests[1].targets.resolve([target])
    await refresh
    expect(model.rules.value[0].version).toBe(2)
  })

  it('最新列表加载后，迟到的旧目录也不覆盖当前列表', async () => {
    const { model, requests } = catalog()
    const latest = model.load()
    requests[1].rows.resolve([{ ...rule, version: 2 }]); requests[1].targets.resolve([target])
    await latest
    requests[0].rows.resolve([rule]); requests[0].targets.resolve([])
    await nextTick(); await nextTick()
    expect(model.rules.value[0].version).toBe(2)
    expect(model.targets.value).toEqual([target])
    expect(model.loading.value).toBe(false)
  })

  it('离开页面取消请求且不再更新状态', async () => {
    const { model, requests, scope } = catalog()
    scope.stop()
    expect(requests[0].signal.aborted).toBe(true)
    requests[0].rows.resolve([rule]); requests[0].targets.resolve([target])
    await nextTick(); await nextTick()
    await model.load()
    expect(requests).toHaveLength(1)
    expect(model.rules.value).toEqual([])
    expect(model.loading.value).toBe(false)
  })
})

describe('试运行生命周期', () => {
  it('关闭弹窗或修改输入会清空结果并忽略过期成功', async () => {
    const { model, requests } = dryRun()
    const running = model.run(dryBody)
    expect(model.running.value).toBe(true)
    model.clear()
    expect(requests[0].signal.aborted).toBe(true)
    requests[0].response.resolve(hit)
    await running
    expect(model.result.value).toBeNull()
    expect(model.running.value).toBe(false)
    expect(model.error.value).toBe('')
  })

  it('以最新试运行为准，过期失败不清除新请求的加载状态', async () => {
    const { model, requests } = dryRun()
    const old = model.run(dryBody)
    const current = model.run({ ...dryBody, facts: { cpu_util: 0 } })
    requests[0].response.reject(new Error('过期失败'))
    await old
    expect(model.running.value).toBe(true)
    expect(model.error.value).toBe('')
    requests[1].response.resolve({ ...hit, matched: false, severity: null })
    await current
    expect(model.result.value?.matched).toBe(false)
    expect(model.running.value).toBe(false)
  })

  it('当前失败可重试，离开页面取消请求并禁止之后运行', async () => {
    const { model, requests, scope } = dryRun()
    const first = model.run(dryBody)
    requests[0].response.reject(new Error('表达式错误'))
    await first
    expect(model.error.value).toBe('表达式错误')
    const retry = model.run(dryBody)
    expect(model.error.value).toBe('')
    scope.stop()
    expect(requests[1].signal.aborted).toBe(true)
    requests[1].response.resolve(hit)
    await retry
    await model.run(dryBody)
    expect(requests).toHaveLength(2)
    expect(model.result.value).toBeNull()
    expect(model.running.value).toBe(false)
  })
})

describe('阈值级别与页面接入', () => {
  const level = (severity: RuleLevel['severity']): RuleLevel => ({ severity, op: '>=', threshold: 90 })
  it('优先添加下一较低级别，P4 后仍选未使用级别而不重复', () => {
    expect(nextRuleLevel([level('P2')])?.severity).toBe('P3')
    expect(nextRuleLevel([level('P4')])?.severity).toBe('P1')
    expect(['P2', 'P4']).not.toContain(nextRuleLevel([level('P2'), level('P4')])?.severity)
    expect(nextRuleLevel([])?.severity).toBe('P1')
    expect(nextRuleLevel(['P1', 'P2', 'P3', 'P4'].map(value => level(value as RuleLevel['severity'])))).toBeNull()
  })

  it.each(['RuleListView.vue', 'RuleEditorDialog.vue'])('%s 可编译并保留权限/刷新入口', name => {
    const filename = resolve(import.meta.dirname, '../src/views/rules', name)
    const { descriptor, errors } = parse(readFileSync(filename, 'utf8'), { filename })
    expect(errors).toEqual([])
    expect(() => compileScript(descriptor, { id: name })).not.toThrow()
    expect(compileTemplate({ source: descriptor.template!.content, filename, id: name }).errors).toEqual([])
    if (name === 'RuleListView.vue') {
      expect(descriptor.template!.content).toContain(':refresh="load"')
      expect(descriptor.template!.content).toContain('v-if="canWrite"')
    } else {
      expect(descriptor.template!.content).toContain(':close-on-press-escape="!saving"')
      expect(descriptor.template!.content).toContain(':disabled="!canWrite || saving"')
      expect(descriptor.scriptSetup!.content).toContain('clearDryRun()')
      expect(descriptor.template!.content).not.toContain('v-model="form.for_count" :min="1" :max="10"')
      expect(descriptor.template!.content).not.toContain('v-model="form.recover.below" :min="0"')
    }
  })
})
