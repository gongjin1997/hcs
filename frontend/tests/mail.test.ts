import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import type { Component } from 'vue'
import type { MailSettings, MailSettingsUpdate } from '../src/types/mail'

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
  setup(_props, { attrs, slots }) { return () => Vue.h(tag, attrs, slots.default?.()) },
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
function loadPanel(): Component {
  const filename = resolve(import.meta.dirname, '../src/views/notify/SmtpPanel.vue')
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: 'mail-panel-test', inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  const icons = ['CircleCheckFilled', 'CircleCloseFilled', 'Message', 'Promotion', 'Setting']
  const dependencies: Record<string, unknown> = {
    vue: Vue,
    dayjs: { default: () => ({ format: () => '12:00:00' }) },
    'element-plus': { ElMessage: messages },
    '@element-plus/icons-vue': Object.fromEntries(icons.map(key => [key, stub('svg')])),
    '@/api/mail': api,
    '@/stores/user': { useUserStore: () => ({ user: { id: 1, email: 'test@example.com' } }) },
    '@/components/RecipientSelect.vue': { default: stub('RecipientSelect') },
  }
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`)
    return dependencies[name]
  }, exports)
  return exports.default as Component
}
const Panel = loadPanel()
const dispose: (() => void)[] = []
async function settle() { await Vue.nextTick(); await Promise.resolve(); await Vue.nextTick() }
async function mount(canManage = true) {
  const root = node('root')
  const app = renderer.createApp(Panel, { contacts: [], canManage })
  for (const name of ['Alert', 'Row', 'Col', 'Form', 'FormItem', 'Input', 'InputNumber', 'Select', 'Option', 'Switch', 'Checkbox', 'Icon', 'Button']) {
    app.component(`El${name}`, stub(name))
  }
  app.directive('loading', {})
  app.mount(root)
  dispose.push(() => app.unmount())
  await settle()
  return root
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
beforeEach(() => { saved = seed(); vi.clearAllMocks() })
afterEach(() => { dispose.splice(0).forEach(fn => fn()) })

describe('runtime mail panel', () => {
  it('loads blank password fields and performs no test or send on mount or save', async () => {
    const root = await mount()
    const passwords = all(root).filter(el => el.tag === 'Input' && el.props.type === 'password')
    expect(passwords.map(el => el.props.modelValue)).toEqual(['', ''])
    expect(passwords.every(el => String(el.props.placeholder).includes('已设置'))).toBe(true)
    const server = all(root).find(el => el.tag === 'Input' && el.props.modelValue === 'smtp.example.com')!
    await change(server, 'changed.example.com')
    expect(button(root, '发送测试邮件').props.disabled).toBe(true)
    await click(root, '保存邮件设置')
    expect(api.saveMailSettings).toHaveBeenCalledWith(expect.objectContaining({
      smtp: expect.objectContaining({ host: 'changed.example.com', password: '', clear_password: false }),
    }))
    expect(api.testSmtpConnection).not.toHaveBeenCalled()
    expect(api.testImapConnection).not.toHaveBeenCalled()
    expect(api.sendMailTest).not.toHaveBeenCalled()
    expect(button(root, '发送测试邮件').props.disabled).toBe(false)
  })

  it('sends only through the explicit send button and uses separate connection tests', async () => {
    const root = await mount()
    expect(all(root).find(el => el.tag === 'RecipientSelect')?.props).toHaveProperty('global-only')
    await click(root, '测试 SMTP 连接（不发信）')
    await click(root, '测试 IMAP 连接与认证')
    expect(api.testSmtpConnection).toHaveBeenCalledTimes(1)
    expect(api.testImapConnection).toHaveBeenCalledTimes(1)
    expect(api.sendMailTest).not.toHaveBeenCalled()
    await click(root, '发送测试邮件')
    expect(api.sendMailTest).toHaveBeenCalledExactlyOnceWith([1])
    expect(textOf(root)).toContain('已发送到 test@example.com')
  })

  it('supports explicit clear and clears typed secrets after a successful save', async () => {
    const root = await mount()
    const passwords = all(root).filter(el => el.tag === 'Input' && el.props.type === 'password')
    await change(passwords[0], 'test-only-new-password')
    const checkbox = all(root).find(el => el.tag === 'Checkbox')!
    await change(checkbox, true)
    ;(checkbox.props.onChange as () => void)()
    await settle()
    await click(root, '保存邮件设置')
    expect(api.saveMailSettings).toHaveBeenCalledWith(expect.objectContaining({
      smtp: expect.objectContaining({ password: '', clear_password: true }),
    }))
    expect(saved.smtp.password_set).toBe(false)
    expect(all(root).filter(el => el.tag === 'Input' && el.props.type === 'password').map(el => el.props.modelValue)).toEqual(['', ''])
  })

  it.each(['permission', 'scope'] as const)('disables saving and all tests for missing %s', async kind => {
    if (kind === 'scope') saved.editable = false
    const root = await mount(kind !== 'permission')
    expect(all(root).some(el => el.tag === 'Button' && textOf(el) === '保存邮件设置')).toBe(false)
    expect(button(root, '测试 SMTP 连接（不发信）').props.disabled).toBe(true)
    expect(button(root, '测试 IMAP 连接与认证').props.disabled).toBe(true)
    expect(button(root, '发送测试邮件').props.disabled).toBe(true)
    expect(api.sendMailTest).not.toHaveBeenCalled()
  })
})
