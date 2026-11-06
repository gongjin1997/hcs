import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as resourceNavigation from '../src/config/resource-navigation'
import * as Vue from 'vue'
import dayjs from 'dayjs'
import { vi } from 'vitest'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import { useTable } from '../src/composables/useTable'
import { useResourceRouteScope } from '../src/composables/useResourceRouteScope'
import * as dictionary from '../src/utils/dict'
import * as format from '../src/utils/format'
import * as reportTitle from '../src/views/reports/report-title'
import * as podClusterOptions from '../src/views/pods/cluster-options'
import * as collectScope from '../src/views/system/collect-scope'

interface Node { children: Node[]; parent: Node | null }
const node = (): Node => ({ children: [], parent: null })
const renderer = Vue.createRenderer<Node, Node>({
  createElement: node, createText: node, createComment: node,
  insert(child, parent) { child.parent = parent; parent.children.push(child) },
  remove(child) { if (child.parent) child.parent.children = child.parent.children.filter(item => item !== child) },
  setText() {}, setElementText() {}, patchProp() {},
  parentNode: child => child.parent, nextSibling: () => null,
})
const stub = Vue.defineComponent({ render: () => Vue.h('stub') })
export const settleView = async () => { await Promise.resolve(); await Vue.nextTick(); await Promise.resolve() }
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

/** 执行实际 setup/watch/操作函数；不启动网络、存储、真实 DOM 或 onMounted 采集。 */
export function mountBusinessScript<T extends object>(file: string, options: {
  props?: Record<string, unknown>; api?: Record<string, unknown>; app?: Record<string, unknown>
  dependencies?: Record<string, unknown>; confirm?: () => Promise<unknown>; prompt?: () => Promise<unknown>
} = {}) {
  const filename = resolve(import.meta.dirname, '../src/views', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: file })
  const { outputText } = ts.transpileModule(compiled.content, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const appState = Vue.reactive({ tenantId: 7, tenants: [], currentTenant: null, refreshStats: vi.fn(), ...options.app })
  const route = Vue.reactive({ query: {} as Record<string, string> })
  const messages = { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }
  const dependencies: Record<string, unknown> = {
    '@/config/resource-navigation': resourceNavigation,
    vue: { ...Vue, onMounted: () => undefined },
    dayjs: { default: dayjs },
    'element-plus': { ElMessage: messages, ElNotification: vi.fn(), ElMessageBox: { confirm: options.confirm ?? vi.fn(async () => 'confirm'), prompt: options.prompt ?? vi.fn(async () => ({ value: '' })) } },
    '@element-plus/icons-vue': new Proxy({}, { get: () => stub }),
    '@/api': options.api ?? {},
    '@/stores/app': { useAppStore: () => appState },
    '@/stores/user': { useUserStore: () => ({ can: () => true, canAny: () => true, user: { tenant_ids: null }, fetchMe: vi.fn(async () => undefined) }) },
    '@/composables/useLatestRequest': { useLatestRequest },
    '@/composables/useTable': { useTable },
    '@/composables/useResourceRouteScope': { useResourceRouteScope },
    '@/utils/dict': dictionary, '@/utils/format': format,
    '@/utils/clipboard': { copyText: vi.fn(async () => true) },
    './report-title': reportTitle,
    './cluster-options': podClusterOptions,
    './collect-scope': collectScope,
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: vi.fn(async () => undefined), push: vi.fn(async () => undefined) }) },
    ...options.dependencies,
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name in dependencies) return dependencies[name]
    if (name.endsWith('.vue')) return { default: stub }
    throw new Error(`Unmocked view dependency: ${name}`)
  }, exports)
  const component = Object.assign(exports.default as object, { render: () => Vue.h('view') }) as Vue.Component
  const app = renderer.createApp(component, options.props)
  app.mount(node())
  const internal = (app as unknown as { _instance: { setupState: T; props: Record<string, unknown> } })._instance
  return { state: internal.setupState, props: internal.props, appState, route, messages, unmount: () => app.unmount() }
}
