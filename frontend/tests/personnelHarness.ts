import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as resourceNavigation from '../src/config/resource-navigation'
import * as Vue from 'vue'
import { vi } from 'vitest'
import * as format from '../src/utils/format'
import * as dictionary from '../src/utils/dict'
import { useUserImport } from '../src/views/system/useUserImport'

interface Node { tag: string; text: string; props: Record<string, unknown>; parent: Node | null; children: Node[] }
const node = (tag: string, text = ''): Node => ({ tag, text, props: {}, parent: null, children: [] })
function remove(child: Node) {
  if (child.parent) child.parent.children = child.parent.children.filter(item => item !== child)
  child.parent = null
}
function insert(child: Node, parent: Node, anchor?: Node | null) {
  remove(child)
  const index = anchor ? parent.children.indexOf(anchor) : -1
  parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  child.parent = parent
}
const renderer = Vue.createRenderer<Node, Node>({
  createElement: node, createText: text => node('#text', text), createComment: text => node('#comment', text),
  insert, remove, parentNode: child => child.parent,
  nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] ?? null,
  setText: (child, text) => { child.text = text },
  setElementText: (child, text) => { child.text = text; child.children = [] },
  patchProp: (child, key, _old, value) => { child.props[key] = value },
  insertStaticContent(content, parent, anchor) { const child = node('#static', content); insert(child, parent, anchor); return [child, child] },
})
const stub = (tag: string) => Vue.defineComponent({
  inheritAttrs: false,
  props: { disabled: Boolean },
  setup(props, { attrs, slots }) { return () => Vue.h(tag, { ...attrs, disabled: props.disabled }, [slots.default?.(), slots.footer?.()]) },
})
const Form = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots, expose }) {
    expose({ validate: async () => true, clearValidate: () => undefined })
    return () => Vue.h('Form', attrs, slots.default?.())
  },
})
const Dialog = Vue.defineComponent({
  inheritAttrs: false,
  props: { modelValue: Boolean },
  setup(props, { attrs, slots }) { return () => props.modelValue ? Vue.h('Dialog', attrs, [slots.default?.(), slots.footer?.()]) : null },
})
const rowsKey: Vue.InjectionKey<Vue.ComputedRef<unknown[]>> = Symbol('rows')
const Table = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    Vue.provide(rowsKey, Vue.computed(() => (attrs.data ?? []) as unknown[]))
    return () => Vue.h('Table', attrs, slots.default?.())
  },
})
const TableColumn = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    const rows = Vue.inject(rowsKey)!
    return () => Vue.h('TableColumn', attrs, rows.value.map((row, index) => slots.default ? slots.default({ row, $index: index })
      : String(row && typeof row === 'object' && typeof attrs.prop === 'string' ? (row as Record<string, unknown>)[attrs.prop] ?? '' : '')))
  },
})

export const allNodes = (root: Node): Node[] => [root, ...root.children.flatMap(allNodes)]
export const textOf = (root: Node): string => root.tag === '#comment' ? '' : root.text + root.children.map(textOf).join('')
export async function settlePersonnel() { await new Promise<void>(resolve => setImmediate(resolve)); await Vue.nextTick() }
export function findNode(root: Node, tag: string, predicate: (item: Node) => boolean = () => true) {
  const found = allNodes(root).find(item => item.tag === tag && predicate(item))
  if (!found) throw new Error(`Missing ${tag}`)
  return found
}
export function formField(root: Node, label: string) {
  return findNode(root, 'FormItem', item => item.props.label === label)
}
export async function setField(root: Node, label: string, value: unknown) {
  const control = allNodes(formField(root, label)).find(item => typeof item.props['onUpdate:modelValue'] === 'function')!
  if (control.props.disabled) throw new Error(`Disabled ${label}`)
  ;(control.props['onUpdate:modelValue'] as (value: unknown) => void)(value)
  await settlePersonnel()
}
export async function clickButton(root: Node, label: string) {
  const button = findNode(root, 'Button', item => textOf(item).trim() === label)
  if (button.props.disabled) throw new Error(`Disabled ${label}`)
  await (button.props.onClick as () => unknown)()
  await settlePersonnel()
}

/** 编译实际模板，以内存 renderer 操作表单 / 按钮；所有接口为显式替身，不访问真实环境。 */
export function mountPersonnel(file: string, options: {
  api?: Record<string, unknown>; props?: Record<string, unknown>; dependencies?: Record<string, unknown>
  user?: { id?: number; tenant_ids: number[] | null }; can?: (permission: string) => boolean
} = {}) {
  const filename = resolve(import.meta.dirname, '../src/views', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: file, inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const replace = vi.fn(async () => undefined), push = vi.fn(async () => undefined)
  const messages = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }
  const confirm = vi.fn(async () => 'confirm')
  const dependencies: Record<string, unknown> = {
    '@/config/resource-navigation': resourceNavigation,
    vue: Vue, '@/api': options.api ?? {}, '@/utils/format': format, '@/utils/dict': dictionary,
    '@element-plus/icons-vue': new Proxy({}, { get: () => stub('Icon') }),
    'element-plus': { ElMessage: messages, ElMessageBox: { confirm, prompt: vi.fn(async () => ({ value: 'new-password' })) } },
    '@/stores/app': { useAppStore: () => ({ tenants: [] }) },
    '@/stores/user': { useUserStore: () => ({ user: options.user ?? { id: 1, tenant_ids: null }, can: options.can ?? (() => true), canAny: () => true }) },
    'vue-router': { useRoute: () => ({ query: {} }), useRouter: () => ({ replace, push }) },
    './useUserImport': { useUserImport },
    ...options.dependencies,
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name in dependencies) return dependencies[name]
    if (name.endsWith('.vue')) return { default: stub('Component') }
    throw new Error(`Unmocked dependency ${name}`)
  }, exports)
  const root = node('root')
  const props = Vue.reactive({ ...options.props })
  const Parent = Vue.defineComponent({ setup: () => () => Vue.h(exports.default as Vue.Component, props) })
  const app = renderer.createApp(Parent)
  const specials: Record<string, Vue.Component> = { Form, Dialog, Table, TableColumn }
  const componentNames = new Set(Array.from(descriptor.template?.content.matchAll(/<el-([a-z][a-z0-9-]*)/g) ?? [], match => match[1]))
  for (const tag of componentNames) {
    const name = tag.split('-').map(part => part[0].toUpperCase() + part.slice(1)).join('')
    app.component('El' + name, specials[name] ?? stub(name))
  }
  app.directive('loading', {})
  app.mount(root)
  return { root, messages, push, replace, confirm, updateProps: (values: Record<string, unknown>) => Object.assign(props, values), unmount: () => app.unmount() }
}
