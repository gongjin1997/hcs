import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import type { Alarm, Summary } from '../src/types'
import * as dict from '../src/utils/dict'
import * as workspaceNavigation from '../src/config/workspace-navigation'

const disposers: (() => void)[] = []
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()) })
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
const settle = async () => { await Promise.resolve(); await Vue.nextTick(); await Promise.resolve() }
// Execute the actual view script in a Vue effect scope; browser QA covers the rendered controls.
function loadScript(file: string, names: string[], dependencies: Record<string, unknown>) {
  const source = readFileSync(resolve(import.meta.dirname, '../src', file), 'utf8')
  const { descriptor } = parse(source)
  const { outputText } = ts.transpileModule(descriptor.scriptSetup!.content + `\nexport { ${names.join(',')} }`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  const output: Record<string, unknown> = {}
  const scope = Vue.effectScope()
  scope.run(() => new Function('require', 'exports', outputText)((name: string) => {
    if (name === 'vue') return { ...Vue, onMounted: () => {}, onBeforeUnmount: () => {} }
    if (name === '@/composables/useLatestRequest') return { useLatestRequest }
    if (name === '@/config/workspace-navigation') return workspaceNavigation
    if (name === '@/utils/dict') return dict
    if (name === '@/utils/format' || name === '@element-plus/icons-vue' || name.endsWith('.vue')) return {}
    if (name in dependencies) return dependencies[name]
    throw new Error(`Unexpected dependency ${name}`)
  }, output))
  disposers.push(() => scope.stop())
  return { output, dispose: () => scope.stop() }
}
function dashboard() {
  const fetchSummary = vi.fn()
  const loaded = loadScript('views/dashboard/DashboardView.vue', ['load', 'data', 'loadError', 'loading', 'capacityOption'], {
    '@/api': { fetchSummary }, '@/stores/app': { useAppStore: () => ({}) },
    '@/stores/user': { useUserStore: () => ({ can: () => true, canAny: () => true }) }, 'vue-router': { useRouter: () => ({ push: vi.fn() }) },
  })
  return { ...loaded, fetchSummary, view: loaded.output as unknown as {
    load: () => Promise<void>; data: Vue.Ref<Summary | undefined>; loadError: Vue.Ref<string>; loading: Vue.Ref<boolean>;
    capacityOption: Vue.ComputedRef<{ xAxis: { max: number } }>;
  } }
}
describe('运行总览刷新', () => {
  it('较早的自动刷新晚返回时不会覆盖新结果', async () => {
    const { view, fetchSummary } = dashboard(), old = deferred<Summary>(), next = deferred<Summary>()
    fetchSummary.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const a = view.load(), b = view.load()
    next.resolve({ health_score: 93 } as Summary); await b
    old.resolve({ health_score: 11 } as Summary); await a
    expect(view.data.value?.health_score).toBe(93)
    expect(view.loading.value).toBe(false)
  })
  it('初次加载失败显示错误，并能用同一刷新操作恢复', async () => {
    const { view, fetchSummary } = dashboard()
    fetchSummary.mockRejectedValueOnce(new Error('请求超时'))
    await view.load()
    expect(view.data.value).toBeUndefined()
    expect(view.loadError.value).toBe('请求超时')
    fetchSummary.mockResolvedValueOnce({ health_score: 100 })
    await view.load()
    expect(view.loadError.value).toBe('')
    expect(view.data.value?.health_score).toBe(100)
  })
  it('离开页面后在途请求不回填', async () => {
    const { view, fetchSummary, dispose } = dashboard(), request = deferred<Summary>()
    fetchSummary.mockReturnValueOnce(request.promise)
    const pending = view.load()
    dispose(); request.resolve({ health_score: 97 } as Summary); await pending
    expect(view.data.value).toBeUndefined()
  })
  it('容量超100%仍完整显示真实数值', () => {
    const { view } = dashboard()
    view.data.value = { capacity: { ecs_cpu: [{ name: '临界资源', value: 123.4 }] } } as Summary
    expect(view.capacityOption.value.xAxis.max).toBeGreaterThanOrEqual(123.4)
  })
})

type MailInfo = { alarm: Alarm; action: string; recipient: string }
const mailInfo = (id: number, status = 'FIRING'): MailInfo => ({ alarm: { id, status } as Alarm, action: 'ack', recipient: 'reader@example.test' })
function mail() {
  const route = Vue.reactive({ params: { token: 'first' } })
  const requests: ReturnType<typeof deferred<MailInfo>>[] = []
  const actions: ReturnType<typeof deferred<Alarm>>[] = []
  const mailActionInfo = vi.fn(() => { const request = deferred<MailInfo>(); requests.push(request); return request.promise })
  const mailActionDo = vi.fn(() => { const request = deferred<Alarm>(); actions.push(request); return request.promise })
  class ApiError extends Error {}
  const loaded = loadScript('views/mobile/MailActionView.vue', ['info', 'done', 'doAck', 'error', 'acting'], {
    '@/api': { mailActionInfo, mailActionDo }, '@/api/request': { ApiError }, 'vue-router': { useRoute: () => route },
  })
  return { route, requests, actions, mailActionInfo, mailActionDo, view: loaded.output as unknown as {
    info: Vue.Ref<MailInfo | undefined>; done: Vue.Ref<boolean>; doAck: () => Promise<void>; acting: Vue.Ref<boolean>;
  } }
}
describe('邮件处理页切换链接', () => {
  it('同一路由切换token重新加载，不显示上条告警', async () => {
    const { route, requests, view, mailActionInfo } = mail()
    route.params.token = 'second'
    expect(mailActionInfo).toHaveBeenLastCalledWith('second')
    requests[1].resolve(mailInfo(2)); await settle()
    requests[0].resolve(mailInfo(1)); await settle()
    expect(view.info.value?.alarm.id).toBe(2)
  })
  it('认领期间切换链接，旧认领结果不覆盖新告警', async () => {
    const { route, requests, actions, view } = mail()
    requests[0].resolve(mailInfo(1)); await settle()
    const pending = view.doAck()
    route.params.token = 'second'
    requests[1].resolve(mailInfo(2)); await settle()
    actions[0].resolve(mailInfo(1, 'ACKED').alarm); await pending
    expect(view.info.value?.alarm.id).toBe(2)
    expect(view.done.value).toBe(false)
  })
  it('认领中重复点击只发送一次操作，失败后可重试', async () => {
    const { requests, actions, view, mailActionDo } = mail()
    requests[0].resolve(mailInfo(1)); await settle()
    const pending = view.doAck()
    await view.doAck()
    expect(mailActionDo).toHaveBeenCalledOnce()
    actions[0].reject(new Error('网络失败')); await pending
    expect(view.acting.value).toBe(false)
    const retry = view.doAck()
    actions[1].resolve(mailInfo(1, 'ACKED').alarm); await retry
    expect(view.done.value).toBe(true)
  })
})
