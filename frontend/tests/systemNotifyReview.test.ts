import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import type { Component } from 'vue'
import type { MailSettings, MailSettingsUpdate } from '../src/types/mail'
import { useLatestRequest } from '../src/composables/useLatestRequest'
import * as podClusterOptions from '../src/views/pods/cluster-options'

// 执行真实 SFC，使用现有 Vue renderer 与进程内 API / 组件替代，不发网络请求。
interface Node {
  tag: string
  text: string
  props: Record<string, unknown>
  parent: Node | null
  children: Node[]
}
const node = (tag: string, text = ''): Node => ({ tag, text, props: {}, parent: null, children: [] })
function remove(child: Node) {
  if (child.parent) {
    const siblings = child.parent.children
    siblings.splice(siblings.indexOf(child), 1)
    child.parent = null
  }
}
const renderer = Vue.createRenderer<Node, Node>({
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
  insertStaticContent(content, parent, anchor) {
    const child = node('#static', content)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
    child.parent = parent
    return [child, child]
  },
  setText: (el, text) => { el.text = text },
  setElementText: (el, text) => { el.text = text; el.children = [] },
  parentNode: el => el.parent,
  nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _prev, value) => { el.props[key] = value },
})
const all = (root: Node): Node[] => [root, ...root.children.flatMap(all)]
const textOf = (root: Node): string => root.tag === '#comment' ? '' : root.text + root.children.map(textOf).join('')
const stub = (tag: string) => Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) { return () => Vue.h(tag, attrs, [slots.default?.(), slots.footer?.()]) },
})
const seed = (): MailSettings => ({
  smtp: { host: 'smtp.example.com', port: 25, security: 'NONE', auth: false, username: '', sender: 'sender@example.com', reply_to: '', password_set: true },
  imap: { enabled: true, host: 'imap.example.com', port: 993, security: 'SSL', username: 'imap-user', password_set: true },
  source: 'database', editable: true, updated_by: '测试操作人', updated_at: null,
})
let saved: MailSettings
const api = {
  getMailSettings: vi.fn(async () => structuredClone(saved)),
  saveMailSettings: vi.fn(async (body: MailSettingsUpdate) => {
    for (const protocol of ['smtp', 'imap'] as const) {
      const { password, clear_password, ...config } = body[protocol] ?? {}
      Object.assign(saved[protocol], config)
      if (clear_password) saved[protocol].password_set = false
      else if (password) saved[protocol].password_set = true
    }
    return structuredClone(saved)
  }),
  testSmtpConnection: vi.fn(async () => ({ protocol: 'SMTP', ok: true, elapsed_ms: 1, message: '未发送邮件' })),
  testImapConnection: vi.fn(async () => ({ protocol: 'IMAP', ok: true, elapsed_ms: 1, message: '未处理邮件' })),
  sendMailTest: vi.fn(async (_ids: number[]) => ({ sent_to: ['test@example.com'], elapsed_ms: 1 })),
}
const messages = { success: vi.fn(), warning: vi.fn() }
const confirmReload = vi.fn(async () => 'confirm')
const validateForm = vi.fn(async () => true)
const epComponents = new Set<string>()
const FormStub = Vue.defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots, expose }) {
    expose({ validate: () => validateForm(), clearValidate: vi.fn() })
    return () => Vue.h('Form', attrs, slots.default?.())
  },
})
function loadPanel(file = 'views/notify/SmtpPanel.vue', extraDependencies: Record<string, unknown> = {}): Component {
  const filename = resolve(import.meta.dirname, '../src', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  for (const match of descriptor.template?.content.matchAll(/<el-([a-z][a-z0-9-]*)/g) ?? []) {
    epComponents.add(match[1].split('-').map(part => part[0].toUpperCase() + part.slice(1)).join(''))
  }
  const compiled = compileScript(descriptor, { id: 'mail-panel-test', inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  const icons = ['CircleCheckFilled', 'CircleCloseFilled', 'Message', 'Promotion', 'Setting']
  const dependencies: Record<string, unknown> = {
    vue: Vue,
    dayjs: { default: () => ({ format: () => '12:00:00' }) },
    'element-plus': { ElMessage: messages, ElMessageBox: { confirm: confirmReload } },
    '@element-plus/icons-vue': Object.fromEntries(icons.map(key => [key, stub('svg')])),
    '@/api/mail': api,
    '@/composables/useLatestRequest': { useLatestRequest },
    './cluster-options': podClusterOptions,
    '@/stores/user': { useUserStore: () => ({ user: { id: 1, email: 'test@example.com' } }) },
    '@/components/RecipientSelect.vue': { default: stub('RecipientSelect') },
    './SmsGlobalPanel.vue': { default: stub('SmsGlobalPanel') },
    ...extraDependencies,
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', '__APP_VERSION__', outputText)((name: string) => {
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`)
    return dependencies[name]
  }, exports, 'test')
  return exports.default as Component
}
const Panel = loadPanel()
const dispose: (() => void)[] = []
async function settle() { await Vue.nextTick(); await Promise.resolve(); await Vue.nextTick() }
async function mount(canManage = true, component = Panel, props: Record<string, unknown> = {}) {
  const root = node('root')
  const app = renderer.createApp(component, { contacts: [], canManage, ...props })
  for (const name of epComponents) {
    app.component('El' + name, name === 'Form' ? FormStub : name === 'Table'
      ? Vue.defineComponent({ inheritAttrs: false, setup(_props, { attrs }) { return () => Vue.h('Table', attrs) } })
      : stub(name))
  }
  app.directive('loading', {})
  const exposed = app.mount(root) as unknown as { reload: () => Promise<void>; busy: boolean }
  dispose.push(() => app.unmount())
  await settle()
  return { root, exposed }
}
function button(root: Node, label: string) {
  const found = all(root).find(el => el.tag === 'Button' && textOf(el) === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}
async function click(root: Node, label: string) {
  const el = button(root, label)
  expect(el.props.disabled).not.toBe(true)
  await (el.props.onClick as () => void)()
  await settle()
}
async function change(el: Node, value: unknown) {
  ;(el.props['onUpdate:modelValue'] as (value: unknown) => void)(value)
  await settle()
}
beforeEach(() => { saved = seed(); systemRoute.query = {}; vi.clearAllMocks() })
afterEach(() => { dispose.splice(0).forEach(fn => fn()) })

describe('system and notification refresh recovery', () => {
  it('exposes reload and blocks overlapping mail requests', async () => {
    const { root, exposed } = await mount()
    let release!: (value: MailSettings) => void
    api.getMailSettings.mockImplementationOnce(() => new Promise<MailSettings>(resolve => { release = resolve }))
    const pending = exposed.reload()
    expect(exposed.busy).toBe(true)
    await exposed.reload()
    expect(api.getMailSettings).toHaveBeenCalledTimes(2)
    saved.smtp.host = 'reloaded.example.test'
    release(structuredClone(saved))
    await pending
    await settle()
    expect(exposed.busy).toBe(false)
    expect(all(root).some(el => el.props.modelValue === 'reloaded.example.test')).toBe(true)
  })

  it('shows a loading error and recovers through retry', async () => {
    api.getMailSettings.mockRejectedValueOnce(new Error('test-only failure'))
    const { root, exposed } = await mount()
    expect(exposed.busy).toBe(false)
    expect(all(root).some(el => el.tag === 'Alert' && el.props.type === 'error')).toBe(true)
    await click(root, '重试')
    expect(all(root).some(el => el.tag === 'Alert' && el.props.type === 'error')).toBe(false)
    expect(api.getMailSettings).toHaveBeenCalledTimes(2)
  })

  it('preserves a mail draft when reload is cancelled', async () => {
    const { root, exposed } = await mount()
    const server = all(root).find(el => el.tag === 'Input' && el.props.modelValue === 'smtp.example.com')!
    await change(server, 'unsaved.example.test')
    confirmReload.mockRejectedValueOnce('cancel')
    await exposed.reload()
    expect(api.getMailSettings).toHaveBeenCalledTimes(1)
    expect(server.props.modelValue).toBe('unsaved.example.test')
    expect(exposed.busy).toBe(false)
    await exposed.reload()
    await settle()
    expect(api.getMailSettings).toHaveBeenCalledTimes(2)
    expect(server.props.modelValue).toBe('smtp.example.com')
  })

  it('keeps a failed-save draft without sending mail', async () => {
    const { root, exposed } = await mount()
    const server = all(root).find(el => el.tag === 'Input' && el.props.modelValue === 'smtp.example.com')!
    await change(server, 'retry.example.test')
    api.saveMailSettings.mockRejectedValueOnce(new Error('test-only failure'))
    await click(root, '保存邮件设置')
    expect(server.props.modelValue).toBe('retry.example.test')
    expect(exposed.busy).toBe(false)
    expect(button(root, '发送测试邮件').props.disabled).toBe(true)
    expect(api.sendMailTest).not.toHaveBeenCalled()
  })

  it('disables edits and skips reload while a mail save is pending', async () => {
    const { root, exposed } = await mount()
    const server = all(root).find(el => el.tag === 'Input' && el.props.modelValue === 'smtp.example.com')!
    await change(server, 'pending.example.test')
    let release!: (value: MailSettings) => void
    api.saveMailSettings.mockImplementationOnce(() => new Promise<MailSettings>(resolve => { release = resolve }))
    const pending = (button(root, '保存邮件设置').props.onClick as () => Promise<void>)()
    await settle()
    expect(exposed.busy).toBe(true)
    expect(all(root).filter(el => el.tag === 'Form').every(el => el.props.disabled === true)).toBe(true)
    await exposed.reload()
    expect(api.getMailSettings).toHaveBeenCalledTimes(1)
    release(structuredClone(saved))
    await pending
    expect(exposed.busy).toBe(false)
  })
})

const iconNames = ['Connection', 'Document', 'Key', 'Monitor', 'Open', 'Setting', 'Timer', 'User',
  'Bottom', 'Plus', 'Right', 'Select', 'Top', 'Delete', 'Link', 'WarningFilled']
const iconModule = Object.fromEntries(iconNames.map(name => [name, stub('svg')]))
const systemRoute = Vue.reactive({ query: {} as Record<string, string>, hash: '' })
const navigateSystem = async (to: { query: Record<string, string> }) => { systemRoute.query = to.query }
const router = { replace: vi.fn(navigateSystem), push: vi.fn(navigateSystem) }
const user = { canAny: () => true, can: () => true, user: { email: 'test@example.com' } }
const common = {
  '@element-plus/icons-vue': iconModule,
  'vue-router': { useRoute: () => systemRoute, useRouter: () => router },
  '@/stores/user': { useUserStore: () => user },
  '@/components/PageHeader.vue': { default: stub('PageHeader') },
}
const tabNames = ['tenants', 'switches', 'tasks', 'platform', 'users', 'roles', 'settings', 'audit'] as const
const tabFiles = ['TenantPanel', 'CollectSwitchPanel', 'TaskPanel', 'PlatformPanel', 'UserPanel', 'RolePanel', 'SettingsPanel', 'AuditPanel']
const reloads = Object.fromEntries(tabNames.map(name => [name, vi.fn(async () => {})]))
const tabBusy = Object.fromEntries(tabNames.map(name => [name, Vue.shallowRef(false)]))
const panelModules = Object.fromEntries(tabFiles.map((name, index) => ['./' + name + '.vue', {
  default: Vue.defineComponent({
    setup(_props, { expose }) {
      expose({ reload: reloads[tabNames[index]], busy: tabBusy[tabNames[index]] })
      return () => Vue.h('Panel')
    },
  }),
}]))
const systemInfo = vi.fn(async () => ({ version: 'test', hcs_version: '8.5.1', mock: false }))
const System = loadPanel('views/system/SystemView.vue', { ...common, ...panelModules, '@/api': { systemInfo } })
function header(root: Node) { return all(root).find(el => el.tag === 'PageHeader')! }
function triggerRefresh(root: Node) { return (header(root).props.refresh as () => Promise<void>)() }

describe('active system tab refresh', () => {
  it('refreshes every active tab through its exposed reload', async () => {
    const { root } = await mount(true, System)
    const tabs = all(root).find(el => el.tag === 'Tabs')!
    for (const name of tabNames) {
      const before = Object.values(reloads).reduce((total, reload) => total + reload.mock.calls.length, 0)
      await change(tabs, name)
      await triggerRefresh(root)
      await settle()
      expect(reloads[name]).toHaveBeenCalledTimes(1)
      expect(Object.values(reloads).reduce((total, reload) => total + reload.mock.calls.length, 0)).toBe(before + 1)
      expect(header(root).props.refreshing).toBe(false)
    }
  })

  it('waits for the panel when the independent system-info request fails', async () => {
    const { root } = await mount(true, System)
    let release!: () => void
    reloads.tenants.mockImplementationOnce(() => new Promise<void>(resolve => { release = resolve }))
    systemInfo.mockRejectedValueOnce(new Error('test-only failure'))
    const pending = triggerRefresh(root)
    await settle()
    expect(header(root).props.refreshing).toBe(true)
    await triggerRefresh(root)
    expect(reloads.tenants).toHaveBeenCalledTimes(1)
    release()
    await pending
    await settle()
    expect(header(root).props.refreshing).toBe(false)
    await triggerRefresh(root)
    expect(reloads.tenants).toHaveBeenCalledTimes(2)
  })

  it('reflects a busy settings panel without reloading it', async () => {
    const { root } = await mount(true, System)
    const tabs = all(root).find(el => el.tag === 'Tabs')!
    await change(tabs, 'settings')
    tabBusy.settings.value = true
    await settle()
    expect(header(root).props.refreshing).toBe(true)
    await triggerRefresh(root)
    expect(reloads.settings).not.toHaveBeenCalled()
    tabBusy.settings.value = false
    await settle()
    expect(header(root).props.refreshing).toBe(false)
  })
})

const settingValue = () => ({
  retention: { label: '保留时间', fields: [{ key: 'report_days', label: '报表保留时间', ge: 1, le: 1000 }],
    value: { report_days: 30 }, editable: true, customized: false },
  platform_alarm: { label: '平台告警', fields: [], value: { enabled: true, task_fail_threshold: 3,
    auth_fail_alarm: true, notify_fail_alarm: true, heartbeat_timeout_min: 3, severity: 'P2', mail_to: [] },
    editable: true, customized: false },
  collect: { label: '采集配置', fields: [], value: { hcs_concurrency: 4, hcs_qps: { ecs: 5 } }, editable: true },
})
const getSettings = vi.fn(async () => settingValue())
const saveSetting = vi.fn(async () => ({ changed: ['report_days'] }))
const settingsDependencies = {
  '@/api': { getSettings, saveSetting },
  '@/utils/dict': { SEVERITY: { P1: { label: '紧急' }, P2: { label: '重要' }, P3: { label: '一般' } } },
  '@/utils/format': { fmtTime: () => '' },
}
const Collection = loadPanel('views/system/CollectionPanel.vue', settingsDependencies)
const Settings = loadPanel('views/system/SettingsPanel.vue', {
  ...settingsDependencies,
  '@element-plus/icons-vue': iconModule,
  './CollectionPanel.vue': { default: Collection },
  // 租户短信（原“租户接入”卡片上的短信配置）在系统设置里，需要“管理租户接入”权限
  './SmsTenantPanel.vue': { default: stub('SmsTenantPanel') },
  '@/stores/user': { useUserStore: () => ({ user: { id: 1, tenant_ids: null }, can: () => true }) },
})

describe('system setting drafts', () => {
  it('saving retention keeps an unsaved alarm threshold and collection QPS', async () => {
    const { root } = await mount(true, Settings)
    const threshold = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 3)!
    const qps = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 5)!
    const days = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 30)!
    await change(threshold, 7)
    await change(qps, 6)
    await change(days, 31)
    const saves = all(root).filter(el => el.tag === 'Button' && textOf(el) === '保存')
    await (saves[0].props.onClick as () => Promise<void>)()
    await settle()
    expect(saveSetting).toHaveBeenCalledWith('retention', { report_days: 31 })
    expect(threshold.props.modelValue).toBe(7)
    expect(qps.props.modelValue).toBe(6)
  })

  it('saving collection keeps the unsaved retention and alarm settings', async () => {
    const { root } = await mount(true, Settings)
    const threshold = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 3)!
    const days = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 30)!
    await change(threshold, 8)
    await change(days, 32)
    await click(root, '保存采集配置')
    await settle()
    expect(threshold.props.modelValue).toBe(8)
    expect(days.props.modelValue).toBe(32)
  })

  it('protects drafts on a cancelled top-bar reload', async () => {
    const { root, exposed } = await mount(true, Settings)
    const days = all(root).find(el => el.tag === 'InputNumber' && el.props.modelValue === 30)!
    await change(days, 31)
    confirmReload.mockRejectedValueOnce('cancel')
    await exposed.reload()
    expect(getSettings).toHaveBeenCalledTimes(1)
    expect(days.props.modelValue).toBe(31)
    expect(exposed.busy).toBe(false)
  })

  it('prevents native submission in all settings forms', async () => {
    const { root } = await mount(true, Settings)
    const forms = all(root).filter(el => el.tag === 'Form')
    expect(forms).toHaveLength(3)
    for (const form of forms) {
      const preventDefault = vi.fn()
      ;(form.props.onSubmit as (event: { preventDefault: () => void }) => void)({ preventDefault })
      expect(preventDefault).toHaveBeenCalledOnce()
    }
  })
})

const createGroup = vi.fn(async () => ({}))
const Group = loadPanel('views/notify/GroupDialog.vue', { '@/api': { createGroup, updateGroup: vi.fn() } })
describe('notification form submission', () => {
  it('handles validation rejection without submitting or rejecting the event handler', async () => {
    const { root } = await mount(true, Group, { visible: true, group: null, contacts: [] })
    validateForm.mockRejectedValueOnce(new Error('required field'))
    await click(root, '保存')
    expect(createGroup).not.toHaveBeenCalled()
  })

  it('does not submit an invalid form', async () => {
    const { root } = await mount(true, Group, { visible: true, group: null, contacts: [] })
    validateForm.mockResolvedValueOnce(false)
    await click(root, '保存')
    expect(createGroup).not.toHaveBeenCalled()
  })
})

interface PreviewAlarm { id: number; severity: string; resource_name: string; title: string }
const notifyApi = {
  listRoutes: vi.fn(async () => []), listGroups: vi.fn(async () => []), listContacts: vi.fn(async () => []),
  listNotifications: vi.fn(async () => []), listAlarms: vi.fn(async (): Promise<{ items: PreviewAlarm[] }> => ({ items: [] })),
  previewMail: vi.fn(), reorderRoutes: vi.fn(), updateRoute: vi.fn(), deleteRoute: vi.fn(), deleteGroup: vi.fn(),
}
const mailReload = vi.fn(async () => {})
const mailBusy = Vue.shallowRef(false)
const MailStub = Vue.defineComponent({
  setup(_props, { expose }) {
    expose({ reload: mailReload, busy: mailBusy })
    return () => Vue.h('MailPanel')
  },
})
const Notify = loadPanel('views/notify/NotifyView.vue', {
  ...common,
  'vue-router': { useRoute: () => ({ query: { tab: 'smtp' } }), useRouter: () => router },
  '@/api': notifyApi,
  '@/stores/app': { useAppStore: () => ({ tenants: [] }) },
  '@/components/SeverityTag.vue': { default: stub('SeverityTag') },
  '@/components/TenantTag.vue': { default: stub('TenantTag') },
  '@/utils/dict': { NOTIFY_KIND: {}, NOTIFY_STATUS: {}, RESOURCE_TYPES: {}, SERVICES: {} },
  '@/utils/format': { fmtTime: () => '-' },
  './ContactPanel.vue': { default: stub('Panel') },
  './GroupDialog.vue': { default: stub('Panel') },
  './RouteDialog.vue': { default: stub('Panel') },
  './SimulatePanel.vue': { default: stub('Panel') },
  './SmtpPanel.vue': { default: MailStub },
})

describe('notification top-bar refresh', () => {
  it('reflects mail-panel busy state and does not overlap a save or connection test', async () => {
    const { root } = await mount(true, Notify)
    mailBusy.value = true
    await settle()
    expect(header(root).props.refreshing).toBe(true)
    await triggerRefresh(root)
    expect(notifyApi.listRoutes).toHaveBeenCalledTimes(1)
    expect(mailReload).not.toHaveBeenCalled()
    mailBusy.value = false
    await settle()
    expect(header(root).props.refreshing).toBe(false)
  })

  it('waits for mail reload after a list request fails and blocks duplicate refreshes', async () => {
    const { root } = await mount(true, Notify)
    let release!: () => void
    mailReload.mockImplementationOnce(() => new Promise<void>(resolve => { release = resolve }))
    notifyApi.listRoutes.mockRejectedValueOnce(new Error('test-only list failure'))
    const pending = triggerRefresh(root)
    await settle()
    expect(header(root).props.refreshing).toBe(true)
    await triggerRefresh(root)
    expect(mailReload).toHaveBeenCalledTimes(1)
    expect(notifyApi.listRoutes).toHaveBeenCalledTimes(2)
    release()
    await pending
    await settle()
    expect(header(root).props.refreshing).toBe(false)
  })

  it('recovers from a failed first list load through the top-bar action', async () => {
    notifyApi.listRoutes.mockRejectedValueOnce(new Error('test-only first-load failure'))
    const { root } = await mount(true, Notify)
    await triggerRefresh(root)
    await settle()
    expect(notifyApi.listRoutes).toHaveBeenCalledTimes(2)
    expect(mailReload).toHaveBeenCalledOnce()
    expect(header(root).props.refreshing).toBe(false)
  })
})

describe('pending notification validation', () => {
  it('submits once when two save clicks complete validation together', async () => {
    const { root } = await mount(true, Group, { visible: true, group: null, contacts: [] })
    let release!: (value: boolean) => void
    const validation = new Promise<boolean>(resolve => { release = resolve })
    validateForm.mockImplementationOnce(() => validation).mockImplementationOnce(() => validation)
    const save = button(root, '保存').props.onClick as () => Promise<void>
    const first = save()
    const second = save()
    release(true)
    await Promise.all([first, second])
    expect(createGroup).toHaveBeenCalledOnce()
  })
})

const alarmOne = { id: 1, severity: 'P1', resource_name: 'test-resource-one', title: 'test alarm one' }
const alarmTwo = { id: 2, severity: 'P2', resource_name: 'test-resource-two', title: 'test alarm two' }
const preview = (subject: string) => ({ subject, html: '<p>test only</p>', text: 'test only', recipients: [], routes: [] })
async function showPreview(root: Node) {
  const tabs = all(root).find(el => el.tag === 'Tabs')!
  await change(tabs, 'preview')
  await (tabs.props.onTabChange as (name: string) => Promise<void>)('preview')
  await settle()
}
describe('notification preview refresh', () => {
  it('reloads a preview on the top-bar action after its first load', async () => {
    notifyApi.listAlarms.mockResolvedValue({ items: [alarmOne] })
    notifyApi.previewMail.mockResolvedValueOnce(preview('first preview')).mockResolvedValueOnce(preview('updated preview'))
    const { root } = await mount(true, Notify)
    await showPreview(root)
    expect(textOf(root)).toContain('first preview')
    await triggerRefresh(root)
    await settle()
    expect(notifyApi.listAlarms).toHaveBeenCalledTimes(2)
    expect(textOf(root)).toContain('updated preview')
    expect(textOf(root)).not.toContain('first preview')
  })

  it('clears the previous preview when the alarm list becomes empty', async () => {
    notifyApi.listAlarms.mockResolvedValueOnce({ items: [alarmOne] }).mockResolvedValueOnce({ items: [] })
    notifyApi.previewMail.mockResolvedValueOnce(preview('stale preview'))
    const { root } = await mount(true, Notify)
    await showPreview(root)
    expect(textOf(root)).toContain('stale preview')
    await triggerRefresh(root)
    await settle()
    expect(textOf(root)).not.toContain('stale preview')
    expect(all(root).filter(el => el.tag === 'iframe')).toHaveLength(0)
  })

  it('ignores a late preview response after another alarm is selected', async () => {
    notifyApi.listAlarms.mockResolvedValue({ items: [alarmOne, alarmTwo] })
    notifyApi.previewMail.mockResolvedValueOnce(preview('initial preview'))
    const { root } = await mount(true, Notify)
    await showPreview(root)
    const select = all(root).find(el => el.tag === 'Select' && el.props.modelValue === 1)!
    let release!: (value: ReturnType<typeof preview>) => void
    notifyApi.previewMail.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    await change(select, 2)
    const pending = (select.props.onChange as () => Promise<void>)()
    notifyApi.previewMail.mockResolvedValueOnce(preview('latest preview'))
    await change(select, 1)
    await (select.props.onChange as () => Promise<void>)()
    release(preview('late stale preview'))
    await pending
    await settle()
    expect(textOf(root)).toContain('latest preview')
    expect(textOf(root)).not.toContain('late stale preview')
  })

  it('clears the old preview when rendering fails and can retry', async () => {
    notifyApi.listAlarms.mockResolvedValue({ items: [alarmOne] })
    notifyApi.previewMail.mockResolvedValueOnce(preview('old preview')).mockRejectedValueOnce(new Error('test-only failure'))
    const { root } = await mount(true, Notify)
    await showPreview(root)
    await triggerRefresh(root)
    await settle()
    expect(textOf(root)).not.toContain('old preview')
    expect(all(root).filter(el => el.tag === 'iframe')).toHaveLength(0)
    notifyApi.previewMail.mockResolvedValueOnce(preview('recovered preview'))
    await triggerRefresh(root)
    await settle()
    expect(textOf(root)).toContain('recovered preview')
  })
})

describe('independent notification list requests', () => {
  it('keeps the top bar busy until the other list calls settle after one fails', async () => {
    const { root } = await mount(true, Notify)
    let release!: (value: never[]) => void
    notifyApi.listGroups.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    notifyApi.listRoutes.mockRejectedValueOnce(new Error('test-only failure'))
    const pending = triggerRefresh(root)
    await settle()
    expect(header(root).props.refreshing).toBe(true)
    release([])
    await pending
    await settle()
    expect(header(root).props.refreshing).toBe(false)
  })
})
