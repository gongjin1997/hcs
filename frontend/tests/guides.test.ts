import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, type EffectScope } from 'vue'
import { useGuideCatalog } from '../src/views/guides/useGuideCatalog'
import type { GuideField, ResourceGuide } from '../src/types/guides'
import { filterGuideFields, guideFieldGroups } from '../src/views/guides/guideFields'

const field = (patch: Partial<GuideField> = {}): GuideField => ({
  key: 'reboots_24h', label: '近 24 小时观测重启', unit: '次', meaning: '轮询看到的重启次数，不能代替完整审计',
  source: 'collectors/ecs.py + Prometheus', data_type: 'number', optional: true,
  availability: 'collected', check_items: ['reboot'], limitations: ['两次采集间可能漏掉重启'], ...patch,
})

const fixture: ResourceGuide = {
  resource_type: 'ecs_server', name: '云服务器 ECS', service: 'ecs', category: 'compute', ledger: true,
  summary: 'ECS 观测指南', collection_tasks: ['ecs_status'], metrics: [field()],
  facts: [field({ key: 'critical', label: '关键资源', data_type: 'boolean', check_items: [] })],
  attributes: [], ledger_fields: [], guidance: ['先核对状态'], best_practices: ['检查最满分区'], limitations: ['无数据不等于 0'],
}

describe('指南字段检索', () => {
  it('按原始 key、中文、单位、来源、检查项和限制检索，忽略大小写与首尾空格', () => {
    for (const query of ['REBOOTS', '重启', '次', 'PROMETHEUS', 'reboot', '漏掉', '  重启  ']) {
      expect(filterGuideFields([field()], query)).toHaveLength(1)
    }
    expect(filterGuideFields([field()], '重启 prometheus')).toHaveLength(1)
    expect(filterGuideFields([field()], 'OOM')).toHaveLength(0)
    expect(filterGuideFields([field()], '   ')).toHaveLength(1)
  })

  it('缺单位与未采集字段保留其含义，不填 0，也不把属性变成事实', () => {
    const legacy = field({ key: 'p90_minutes', unit: null, availability: 'not_collected', check_items: [] })
    const filtered = filterGuideFields([legacy], 'p90')
    expect(filtered[0].unit).toBeNull()
    expect(filtered[0].availability).toBe('not_collected')
    const groups = guideFieldGroups(fixture, 'reboots')
    expect(groups.map(group => group.key)).toEqual(['metrics', 'facts', 'attributes', 'ledger_fields'])
    expect(groups[0].fields).toHaveLength(1)
    expect(groups[1].fields).toHaveLength(0)
    expect(groups[1].total).toBe(1)
    expect(fixture.facts[0].key).toBe('critical')
  })

  it('实际页面可编译，使用统一刷新、URL query 类型与明确失败状态', () => {
    const filename = resolve(import.meta.dirname, '../src/views/guides/GuidesView.vue')
    const source = readFileSync(filename, 'utf8')
    const { descriptor, errors } = parse(source, { filename })
    expect(errors).toEqual([])
    expect(() => compileScript(descriptor, { id: 'guides' })).not.toThrow()
    expect(compileTemplate({ source: descriptor.template!.content, filename, id: 'guides' }).errors).toEqual([])
    expect(descriptor.template!.content).toContain('<PageHeader title="资源与指标指南" :refresh="load" :refreshing="loading" />')
    expect(descriptor.scriptSetup!.content).toContain('route.query.type')
    expect(descriptor.scriptSetup!.content).toContain('watch(selectedType')
    expect(descriptor.template!.content).toContain('guide.limitations')
    expect(descriptor.template!.content).toContain('row.source')
    expect(descriptor.template!.content).toContain('row.unit')
  })
})


const catalogScopes: EffectScope[] = []
afterEach(() => { catalogScopes.splice(0).forEach(scope => scope.stop()) })

function catalog() {
  const requests: { signal: AbortSignal; resolve: (data: ResourceGuide[]) => void; reject: (cause: unknown) => void }[] = []
  const scope = effectScope()
  catalogScopes.push(scope)
  const model = scope.run(() => useGuideCatalog(signal => new Promise((resolve, reject) => {
    requests.push({ signal, resolve, reject })
  })))!
  void model.load()
  return { model, requests, scope }
}

describe('指南加载与刷新', () => {
  it('保留完整元数据；加载失败与空目录分别处理，重试可恢复', async () => {
    const { model, requests } = catalog()
    expect(model.loading.value).toBe(true)
    requests[0].reject(new Error('连接失败'))
    await nextTick()
    expect(model.loadError.value).toBe('连接失败')
    expect(model.guides.value).toEqual([])
    expect(model.loading.value).toBe(false)
    const retry = model.load()
    expect(model.loadError.value).toBe('')
    requests[1].resolve([fixture])
    await retry
    expect(model.guides.value).toEqual([fixture])
    expect(model.guides.value[0].limitations).toEqual(['无数据不等于 0'])
    const empty = model.load()
    requests[2].resolve([])
    await empty
    expect(model.guides.value).toEqual([])
    expect(model.loadError.value).toBe('')
  })

  it('刷新失败时保留上次目录，不把失败当作无字段', async () => {
    const { model, requests } = catalog()
    requests[0].resolve([fixture])
    await nextTick()
    const refresh = model.load()
    requests[1].reject(new Error('刷新失败'))
    await refresh
    expect(model.guides.value).toEqual([fixture])
    expect(model.loadError.value).toBe('刷新失败')
    expect(model.loading.value).toBe(false)
  })

  it.each(['resolve', 'reject'] as const)('取消前一次请求，过期的 %s 不覆盖当前请求或状态', async outcome => {
    const { model, requests } = catalog()
    const refresh = model.load()
    expect(requests[0].signal.aborted).toBe(true)
    if (outcome === 'resolve') requests[0].resolve([fixture])
    else requests[0].reject(new Error('旧错误'))
    await nextTick()
    expect(model.guides.value).toEqual([])
    expect(model.loading.value).toBe(true)
    expect(model.loadError.value).toBe('')
    requests[1].resolve([{ ...fixture, name: '新目录' }])
    await refresh
    expect(model.guides.value[0].name).toBe('新目录')
  })

  it.each(['resolve', 'reject'] as const)('当前目录已加载后，旧请求 %s 仍不可覆盖它', async outcome => {
    const { model, requests } = catalog()
    const latest = model.load()
    requests[1].resolve([{ ...fixture, name: '当前目录' }])
    await latest
    if (outcome === 'resolve') requests[0].resolve([fixture])
    else requests[0].reject(new Error('迟到的错误'))
    await nextTick()
    expect(model.guides.value[0].name).toBe('当前目录')
    expect(model.loadError.value).toBe('')
    expect(model.loading.value).toBe(false)
  })

  it('离开页面取消加载，并忽略返回结果和后续刷新', async () => {
    const { model, requests, scope } = catalog()
    scope.stop()
    expect(requests[0].signal.aborted).toBe(true)
    requests[0].resolve([fixture])
    await nextTick()
    await model.load()
    expect(requests).toHaveLength(1)
    expect(model.guides.value).toEqual([])
    expect(model.loading.value).toBe(false)
  })

  it('可用页面上的中文标识检索缺失和计算字段', () => {
    expect(filterGuideFields([field({ availability: 'not_collected' })], '未采集')).toHaveLength(1)
    expect(filterGuideFields([field({ availability: 'derived' })], '平台计算')).toHaveLength(1)
    expect(filterGuideFields([field()], '可缺失')).toHaveLength(1)
  })
})
