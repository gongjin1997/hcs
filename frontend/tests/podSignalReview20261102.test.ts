import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as Vue from 'vue'
import { describe, expect, it } from 'vitest'
import { fromNow } from '../src/utils/format'

async function render(file: string, props: Record<string, unknown>) {
  const filename = resolve(import.meta.dirname, '../src/views/pods', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const compiled = compileScript(descriptor, { id: file, inlineTemplate: true })
  const { outputText } = ts.transpileModule(compiled.content, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  const exports: { default?: Vue.Component } = {}
  new Function('require', 'exports', outputText)((name: string) => {
    if (name === 'vue') return Vue
    if (name === '@/utils/format') return { fromNow }
    throw new Error(`Unexpected dependency: ${name}`)
  }, exports)
  const app = Vue.createSSRApp(exports.default!, props)
  const slot = Vue.defineComponent({ setup: (_props, ctx) => () => Vue.h('span', ctx.slots.default?.()) })
  app.component('el-tag', slot)
  app.component('el-tooltip', slot)
  return renderToString(app)
}

describe('容器事件证据状态：实际模板渲染', () => {
  it.each([
    [{ ongoing: true, detail: {} }, '进行中'],
    [{ ongoing: true, detail: { signal_status: 'UNKNOWN' } }, '采样中断，状态未知'],
    [{ ongoing: false, detail: { closure_reason: 'POD_DELETED', recovery_confirmed: false } }, '目标已删除，非恢复'],
    [{ ongoing: false, detail: { closure_reason: 'POD_REPLACED', recovery_confirmed: false } }, 'Pod 已替换，非恢复'],
    [{ ongoing: false, detail: { closure_reason: 'EVENT_EXPIRED', recovery_confirmed: false } }, '事件已过期，恢复未知'],
    [{ ongoing: false, detail: { closure_reason: 'CONTAINER_TERMINATED', recovery_confirmed: false } }, '容器已终止，非恢复'],
    [{ ongoing: false, detail: { recovery_confirmed: true } }, '已恢复'],
  ])('显示 %s', async (event, expected) => {
    const html = await render('PodSignalBadge.vue', { event })
    expect(html).toContain(expected)
    if ('recovery_confirmed' in event.detail && event.detail.recovery_confirmed === false) {
      expect(html).not.toContain('>已恢复<')
    }
  })
  it('普通历史事件不臆造恢复标识', async () => {
    expect(await render('PodSignalBadge.vue', { event: { ongoing: false, detail: {} } })).not.toContain('已恢复')
  })
  it.each([
    [{ at: '2026-11-02T02:00:00Z', pods: 0, hit: 0, max: null, errors: [] }, '未匹配到目标，状态未知'],
    [{ at: '2026-11-02T02:00:00Z', pods: 2, hit: 0, max: null, errors: ['HTTP 403'] }, '采样不完整，状态未知'],
  ])('探针未知时不显示零个超阈值作为正常结果', async (result, expected) => {
    const html = await render('PodProbeResult.vue', { result, enabled: true })
    expect(html).toContain(expected)
    expect(html).not.toContain('已确认超阈值')
  })
  it('有效的零值与查询失败有不同表现', async () => {
    const html = await render('PodProbeResult.vue', {
      result: { at: '2026-11-02T02:00:00Z', pods: 2, hit: 0, max: 0, errors: [] }, enabled: true,
    })
    expect(html).toContain('已确认超阈值')
    expect(html).not.toContain('状态未知')
  })
})
