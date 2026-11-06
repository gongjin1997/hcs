import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'
import * as Vue from 'vue'

type Hooks = {
  mounted: (el: HTMLElement, binding: { value: string | string[] }) => void
  updated: (el: HTMLElement, binding: { value: string | string[] }) => void
  beforeUnmount: (el: HTMLElement) => void
}
function setup() {
  const permissions = Vue.ref<string[]>([])
  const source = readFileSync(resolve(import.meta.dirname, '../src/directives/admin.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const output: Record<string, unknown> = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name === 'vue') return Vue
    if (name === '@/stores/user') return { useUserStore: () => ({ canAny: (...values: string[]) => values.some(value => permissions.value.includes(value)) }) }
    throw new Error(name)
  }, output)
  return { permissions, directive: output.vPerm as Hooks, element: { style: { display: 'inline-flex' } } as HTMLElement }
}
describe('权限按钮实时更新', () => {
  it('授权与撤权立即更新显隐，保留Vue管理的DOM', () => {
    const { directive, element, permissions } = setup()
    directive.mounted(element, { value: 'ledger:edit' })
    expect(element.style.display).toBe('none')
    permissions.value = ['ledger:edit']
    expect(element.style.display).toBe('inline-flex')
    permissions.value = []
    expect(element.style.display).toBe('none')
    directive.beforeUnmount(element)
  })
  it('支持任一权限与动态指令参数，卸载后停止监听', () => {
    const { directive, element, permissions } = setup()
    permissions.value = ['ledger:export']
    directive.mounted(element, { value: ['ledger:edit', 'ledger:export'] })
    expect(element.style.display).toBe('inline-flex')
    directive.updated(element, { value: 'report:manage' })
    expect(element.style.display).toBe('none')
    directive.beforeUnmount(element)
    permissions.value = ['report:manage']
    expect(element.style.display).toBe('none')
  })
})
