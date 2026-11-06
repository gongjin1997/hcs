import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import type { Component, InjectionKey, ShallowRef } from 'vue'
import * as VueRouter from 'vue-router'
import * as resourceNavigation from '../src/config/resource-navigation'
import * as workspaceNavigation from '../src/config/workspace-navigation'

// Use Vue's real renderer and Teleport in the existing Node test environment.
// The host models the element tree; responsive geometry is covered by browser acceptance.
interface HostNode {
  tag: string
  text: string
  props: Record<string, unknown>
  style: Record<string, string>
  parent: HostNode | null
  children: HostNode[]
}
const node = (tag: string, text = ''): HostNode => Vue.markRaw({ tag, text, props: {}, style: {}, parent: null, children: [] })
function remove(child: HostNode) {
  if (child.parent) {
    const siblings = child.parent.children
    const index = siblings.indexOf(child)
    if (index >= 0) siblings.splice(index, 1)
    child.parent = null
  }
}
const renderer = Vue.createRenderer<HostNode, HostNode>({
  insert(child, parent, anchor) {
    remove(child)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
    child.parent = parent
  },
  remove,
  createElement: tag => node(tag),
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text },
  setElementText: (el, text) => { el.text = text; el.children = [] },
  parentNode: el => el.parent,
  nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _prev, value) => { el.props[key] = value },
})
function findAll(root: HostNode, predicate: (el: HostNode) => boolean): HostNode[] {
  return [...(predicate(root) ? [root] : []), ...root.children.flatMap(child => findAll(child, predicate))]
}
const withClass = (name: string) => (el: HostNode) => String(el.props.class ?? '').split(/\s+/).includes(name)
const textOf = (root: HostNode): string => root.tag === '#comment' ? '' : root.text + root.children.map(textOf).join('')
const click = (el: HostNode) => (el.props.onClick as () => void)()
const disposers: (() => void)[] = []
afterEach(() => { disposers.splice(0).reverse().forEach(dispose => dispose()); vi.unstubAllGlobals() })
const TestTransition = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    return () => Vue.h(Vue.BaseTransition, {
      ...attrs,
      // Like a CSS leave transition, complete after the current render has finished.
      onLeave: (_el: Element, done: () => void) => queueMicrotask(done),
    }, slots)
  },
})
async function settle() { await Vue.nextTick(); await Vue.nextTick() }

// Compile the actual SFCs without changing the shared Vite/Vitest config or adding dependencies.
function loadSfc(file: string, dependencies: Record<string, unknown> = {}) {
  const filename = resolve(import.meta.dirname, '../src', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: file, inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  const exports: Record<string, unknown> = {}
  const require = (name: string) => {
    if (name === 'vue') return { ...Vue, Transition: TestTransition }
    if (name in dependencies) return dependencies[name]
    throw new Error(`Unexpected SFC dependency: ${name}`)
  }
  new Function('require', 'exports', '__APP_VERSION__', outputText)(require, exports, 'test')
  return exports
}
const headerModule = loadSfc('components/PageHeader.vue', { '@element-plus/icons-vue': { Refresh: passthrough('svg') } })
const PageHeader = headerModule.default as Component
const headerKey = headerModule.pageHeaderTargetKey as InjectionKey<Readonly<ShallowRef<HTMLElement | null>>>
const headerRef = (target: ShallowRef<HostNode | null>) => target as unknown as ShallowRef<HTMLElement | null>
const Tooltip = Vue.defineComponent({
  props: ['content'],
  setup(props, { slots }) { return () => Vue.h('span', { 'data-description': props.content }, slots.default?.()) },
})

function mount(component: Component, components: Record<string, Component> = {}) {
  const root = node('root')
  const app = renderer.createApp(component)
  app.component('ElTooltip', Tooltip)
  app.component('ElIcon', passthrough('i'))
  app.component('ElButton', passthrough('button'))
  Object.entries(components).forEach(([name, child]) => app.component(name, child))
  app.mount(root)
  disposers.push(() => app.unmount())
  return root
}

function headerFixture() {
  const target = Vue.shallowRef<HostNode | null>(null)
  const desc = Vue.ref('数据更新于 10:00')
  const refreshing = Vue.ref(false)
  const clicks = Vue.ref(0)
  const HeaderPage = Vue.defineComponent({
    setup() {
      return () => Vue.h('article', [
        Vue.h(PageHeader, { title: '运行总览', desc: desc.value, refreshing: refreshing.value, refresh: () => clicks.value++ }, {
          default: () => Vue.h('button', '导出'),
        }),
        Vue.h('div', { class: 'page-content' }, '内容'),
      ])
    },
  })
  const root = mount(Vue.defineComponent({
    setup() {
      Vue.provide(headerKey, headerRef(target))
      return () => Vue.h('section', [Vue.h('header', { ref: target }), Vue.h('main', [Vue.h(HeaderPage)])])
    },
  }))
  return { root, target, desc, refreshing, clicks }
}

describe('unified page header', () => {
  it('does not leave an inline fallback while the layout target is unavailable', async () => {
    const target = Vue.shallowRef<HostNode | null>(null)
    const root = mount(Vue.defineComponent({
      setup() {
        Vue.provide(headerKey, headerRef(target))
        return () => Vue.h('main', [Vue.h(PageHeader, { title: '等待挂载', refresh: vi.fn() })])
      },
    }))
    await Vue.nextTick()
    expect(findAll(root, withClass('page-refresh'))).toHaveLength(0)
    const header = node('header')
    target.value = header
    await Vue.nextTick()
    expect(findAll(header, withClass('page-refresh'))[0].props['aria-label']).toBe('刷新当前页面')
    target.value = null
    await Vue.nextTick()
    expect(header.children).toHaveLength(0)
  })

  it('waits for the mounted layout target and leaves no header element in main', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const { root } = headerFixture()
      await Vue.nextTick()
      const main = findAll(root, el => el.tag === 'main')[0]
      const header = findAll(root, el => el.tag === 'header')[0]
      expect(findAll(header, withClass('page-refresh'))).toHaveLength(1)
      expect(findAll(header, el => ['h1', 'h2', 'p'].includes(el.tag))).toHaveLength(0)
      expect(findAll(main, withClass('page-refresh'))).toHaveLength(0)
      const article = findAll(main, el => el.tag === 'article')[0]
      expect(article.children.filter(el => !el.tag.startsWith('#')).map(el => el.props.class)).toEqual(['page-content'])
      expect(warn).not.toHaveBeenCalled()
    } finally { warn.mockRestore() }
  })

  it('keeps only the refresh action and honours loading state', async () => {
    const { root, desc, refreshing, clicks } = headerFixture()
    await Vue.nextTick()
    const header = findAll(root, el => el.tag === 'header')[0]
    const button = () => findAll(header, withClass('page-refresh'))[0]
    await click(button())
    await Vue.nextTick()
    expect(clicks.value).toBe(1)
    refreshing.value = true
    desc.value = '数据更新于 10:30'
    await Vue.nextTick()
    await click(button())
    expect(clicks.value).toBe(1)
    expect(button().props.loading).toBe(true)
    expect(findAll(header, withClass('extra'))).toHaveLength(0)
    expect(findAll(header, withClass('page-description'))).toHaveLength(0)
    expect(textOf(header)).not.toContain('导出')
    expect(textOf(header)).not.toContain('数据更新于')
    refreshing.value = false
    await Vue.nextTick()
    await click(button())
    expect(clicks.value).toBe(2)
  })

  it('prevents overlapping refreshes and recovers after failure', async () => {
    let reject!: (cause: Error) => void
    const refresh = vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail }))
    const target = Vue.shallowRef<HostNode | null>(null)
    const root = mount(Vue.defineComponent({
      setup() {
        Vue.provide(headerKey, headerRef(target))
        return () => Vue.h('section', [Vue.h('header', { ref: target }), Vue.h('main', [
          Vue.h(PageHeader, { title: '列表', refresh }),
        ])])
      },
    }))
    await Vue.nextTick()
    const button = findAll(root, withClass('page-refresh'))[0]
    const first = click(button)
    click(button)
    await Vue.nextTick()
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(button.props.loading).toBe(true)
    reject(new Error('offline'))
    await first
    await Vue.nextTick()
    expect(button.props.loading).toBe(false)
    refresh.mockImplementationOnce(() => Promise.resolve())
    await click(button)
    expect(refresh).toHaveBeenCalledTimes(2)
  })

})

function passthrough(tag: string): Component {
  return Vue.defineComponent({
    setup(_props, { slots }) { return () => Vue.h(tag, slots.default?.()) },
  })
}
async function layoutFixture() {
  vi.stubGlobal('window', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const app = Vue.reactive({
    collapsed: false, tenantSeq: 0, tenants: [], stats: null, streamConnected: false,
    refreshStats: vi.fn(), toggle: vi.fn(),
  })
  const user = { user: { permissions: [], username: 'operator' }, can: () => false, displayName: '运维', roleName: '只读' }
  const paths = ['/dashboard', '/ledger', '/job-reports', ...['dgc', 'flink', 'realtime', 'batch', 'mrs'].map(p => `/job-reports/${p}`)]
  const action = vi.fn()
  const HeaderPage = Vue.defineComponent({
    setup() {
      const owner = `${router.currentRoute.value.path}#${app.tenantSeq}`
      return () => Vue.h('article', [
        Vue.h(PageHeader, { title: owner, refresh: () => action(owner) }),
        Vue.h('div', '业务内容'),
      ])
    },
  })
  const EmptyPage = Vue.defineComponent({ setup: () => () => Vue.h('article', '无页头页面') })
  const router = VueRouter.createRouter({
    history: VueRouter.createMemoryHistory(),
    routes: [
      ...paths.map(path => ({ path, component: HeaderPage, meta: { title: path } })),
      { path: '/403', component: EmptyPage },
      { path: '/:pathMatch(.*)*', component: HeaderPage },
    ],
  })
  await router.push('/dashboard')
  await router.isReady()
  const { default: BasicLayout } = loadSfc('layouts/BasicLayout.vue', {
    '@/config/resource-navigation': resourceNavigation,
    '@/config/workspace-navigation': workspaceNavigation,
    '@element-plus/icons-vue': Object.fromEntries(['ArrowDown', 'Bell', 'Expand', 'Fold', 'Lock', 'Monitor', 'OfficeBuilding', 'Search', 'SwitchButton', 'User'].map(name => [name, passthrough('svg')])),
    'element-plus': { ElMessage: { success: vi.fn() }, ElNotification: vi.fn() },
    'vue-router': VueRouter,
    '@/api': { listResources: vi.fn() },
    '@/components/PageHeader.vue': headerModule,
    '@/components/WorkspaceNav.vue': { default: passthrough('nav') },
    '@/components/PasswordDialog.vue': { default: passthrough('dialog') },
    '@/composables/useAlarmStream': { useAlarmStream: () => ({ connected: Vue.ref(false), reopen: vi.fn() }) },
    '@/config/menu': { GROUP_OF: {}, visibleMenu: () => [{ title: '监控', items: paths.map(path => ({ path, title: path, icon: passthrough('svg') })) }] },
    '@/stores/app': { useAppStore: () => app },
    '@/stores/user': { useUserStore: () => user },
    '@/utils/dict': { AUTH_TYPE: {}, SEVERITY: {}, typeName: () => '' },
  })
  const components: Record<string, Component> = Object.fromEntries([
    ['ElContainer', 'section'], ['ElAside', 'aside'], ['ElHeader', 'header'], ['ElMain', 'main'],
    ['ElScrollbar', 'div'], ['ElTooltip', 'div'], ['ElIcon', 'i'], ['ElButton', 'button'],
    ['ElBreadcrumb', 'nav'], ['ElBreadcrumbItem', 'span'], ['ElDropdown', 'div'],
    ['ElAutocomplete', 'input'], ['ElDropdownItem', 'div'], ['ElDropdownMenu', 'div'], ['ElBadge', 'span'],
  ].map(([name, tag]) => [name, passthrough(tag)]))
  components.ElTooltip = Tooltip
  const LayoutRoot = Vue.defineComponent({
    setup() { return () => Vue.h(BasicLayout as Component) },
  })
  const root = node('root')
  const mounted = renderer.createApp(LayoutRoot)
  mounted.use(router)
  Object.entries(components).forEach(([name, component]) => mounted.component(name, component))
  mounted.mount(root)
  disposers.push(() => mounted.unmount())
  return { root, app, router, action }
}

describe('page action placement', () => {
  it('keeps all page refresh registrations separate from business action slots', () => {
    const views = ['dashboard/DashboardView.vue', 'alarms/AlarmListView.vue', 'ledger/LedgerView.vue',
      'pods/PodEventsView.vue', 'reports/ReportView.vue', 'silences/SilenceView.vue', 'rules/RuleListView.vue',
      'notify/NotifyView.vue', 'system/SystemView.vue', 'job-reports/JobReportPanel.vue']
    for (const view of views) {
      const filename = resolve(import.meta.dirname, '../src/views', view)
      const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
      const headers = descriptor.template?.content.match(/<PageHeader\b[^>]*>/g) ?? []
      expect(headers, view).toHaveLength(1)
      expect(headers[0], view).toContain(':refresh=')
      expect(headers[0], view).toMatch(/\/\s*>$/)
    }
  })
})

describe('layout header lifecycle and navigation', () => {
  it('refreshes the current route and tenant from the right toolbar', async () => {
    const { root, app, router, action } = await layoutFixture()
    await settle()
    const header = findAll(root, el => el.tag === 'header')[0]
    const right = findAll(header, withClass('right'))[0]
    const actions = () => findAll(header, withClass('page-refresh'))
    expect(findAll(header, el => el.props.role === 'heading').map(textOf)).toEqual(['/dashboard'])
    expect(actions()).toHaveLength(1)
    expect(findAll(right, withClass('page-refresh'))).toHaveLength(1)
    expect(findAll(header, withClass('page-header-host'))).toHaveLength(0)
    await click(actions()[0])
    expect(action).toHaveBeenLastCalledWith('/dashboard#0')
    await router.push('/ledger')
    await settle()
    expect(actions()).toHaveLength(1)
    await click(actions()[0])
    expect(action).toHaveBeenLastCalledWith('/ledger#0')
    app.tenantSeq++
    await settle()
    await click(actions()[0])
    expect(action).toHaveBeenLastCalledWith('/ledger#0')
    await router.push('/403')
    await settle()
    expect(actions()).toHaveLength(0)
    await router.push('/dashboard')
    await settle()
    expect(actions()).toHaveLength(1)
    await click(actions()[0])
    expect(action).toHaveBeenLastCalledWith('/dashboard#1')
    const main = findAll(root, el => el.tag === 'main')[0]
    expect(findAll(main, withClass('page-refresh'))).toHaveLength(0)
  })

  it('preserves refresh on query changes and replaces it when the tenant changes', async () => {
    const { root, app, router, action } = await layoutFixture()
    await settle()
    const header = findAll(root, el => el.tag === 'header')[0]
    const actions = () => findAll(header, withClass('page-refresh'))
    const initial = actions()[0]
    await router.push({ path: '/dashboard', query: { tab: 'overview' } })
    await settle()
    expect(actions()[0]).toBe(initial)
    app.tenantSeq++
    app.tenantSeq++
    await router.push('/ledger')
    await settle()
    expect(actions()).toHaveLength(1)
    await click(actions()[0])
    expect(action).toHaveBeenLastCalledWith('/ledger#2')
  })

  it.each(['dgc', 'flink', 'realtime', 'batch', 'mrs'])('selects the %s menu for nested report routes', async service => {
    const { root, router } = await layoutFixture()
    await router.push(`/job-reports/${service}`)
    await settle()
    expect(findAll(root, el => el.tag === 'a' && withClass('active')(el)).map(el => el.props.href)).toEqual([`/job-reports/${service}`])
    await router.push(`/job-reports/${service}/jobs/run-123`)
    await settle()
    expect(findAll(root, el => el.tag === 'a' && withClass('active')(el)).map(el => el.props.href)).toEqual([`/job-reports/${service}`])
    await router.push(`/job-reports/${service}-other`)
    await settle()
    expect(findAll(root, el => el.tag === 'a' && withClass('active')(el)).map(el => el.props.href)).toEqual(['/job-reports'])
  })
})
