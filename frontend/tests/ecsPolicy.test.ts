import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef, type EffectScope } from 'vue'
import { ecsPolicyPayload, blankEcsPolicy, ecsPhaseReason } from '../src/views/ecs/ecs-policy'
import { useEcsPolicy } from '../src/views/ecs/useEcsPolicy'
import type { EcsInstancesOut, EcsPolicyOut } from '../src/types/ecs'
import { ecsInstance, ecsPolicy } from './ecsFixtures'
import { deferred } from './businessViewHarness'
const scopes: EffectScope[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
const settle = async () => { await new Promise<void>(resolve => setImmediate(resolve)); await nextTick() }
function setup(canManage = true) {
  const tenant = shallowRef<number | null>(7), visible = shallowRef(true), permission = shallowRef(canManage)
  const reads: { result: ReturnType<typeof deferred<EcsPolicyOut>>; signal: AbortSignal }[] = []
  const saves: { result: ReturnType<typeof deferred<EcsPolicyOut>>; signal: AbortSignal; tenantId: number | null }[] = []
  const lookups: { result: ReturnType<typeof deferred<EcsInstancesOut>>; signal: AbortSignal; tenantId: number | null }[] = []
  const saver = vi.fn((_tenant, _body, context) => { const result = deferred<EcsPolicyOut>(); saves.push({ result, ...context }); return result.promise })
  const fetcher = vi.fn((_tenant, context) => { const result = deferred<EcsPolicyOut>(); reads.push({ result, ...context }); return result.promise })
  const scope = effectScope(); scopes.push(scope)
  const model = scope.run(() => useEcsPolicy({ tenantId: () => tenant.value, visible: () => visible.value, canManage: () => permission.value, fetcher, saver,
    instancesFetcher: (_query, context) => { const result = deferred<EcsInstancesOut>(); lookups.push({ result, ...context }); return result.promise } }))!
  return { model, tenant, visible, permission, reads, saves, lookups, fetcher, saver }
}

describe('ECS 配置校验', () => {
  it('默认四阶段独立，保留一天/五分钟/两分钟/一小时和全部选择', () => {
    const policy = blankEcsPolicy()
    expect([policy.inventory.interval_sec, policy.metrics.interval_sec, policy.inspection.interval_sec, policy.quota.interval_sec]).toEqual([86400, 300, 120, 3600])
    const payload = ecsPolicyPayload({ ...policy, metrics: { enabled: false, interval_sec: 600 } }, 'null')
    expect(payload.inventory).toEqual(policy.inventory)
    expect(payload.inspection).toEqual(policy.inspection)
    expect(payload.quota).toEqual(policy.quota)
    expect(payload.metrics.enabled).toBe(false)
    expect(payload.selection).toEqual({ mode: 'all', resource_ids: [] })
  })
  it.each([0, 59, 2592001, 300.5, Number.NaN])('拒绝错误间隔 %s，不截断或替换为默认值', interval => {
    const policy = blankEcsPolicy(); policy.metrics.interval_sec = interval
    expect(() => ecsPolicyPayload(policy, 'null')).toThrow('间隔')
  })
  it.each([{ ids: [1, 1] }, { ids: [0] }, { ids: [-1] }, { ids: [1.5] }, { ids: [Number.NaN] }])('拒绝非稳定或重复的本地资源 ID $ids', ({ ids }) => {
    const policy = blankEcsPolicy(); policy.selection = { mode: 'selected', resource_ids: ids }
    expect(() => ecsPolicyPayload(policy, 'null')).toThrow('本地资源 ID')
  })
  it('只保留 Prometheus 校验，所有提交均带 aom:null', () => {
    expect(() => ecsPolicyPayload(blankEcsPolicy(), '{bad json')).toThrow('Prometheus 映射须为有效 JSON')
    const prom = { queries: { mem_util: 'measured_memory_ratio * 100' }, instance_label: 'vm_id', units: { mem_util: '%' } }
    expect(ecsPolicyPayload(blankEcsPolicy(), JSON.stringify(prom))).toMatchObject({ aom: null, prometheus: prom })
    expect(() => ecsPolicyPayload(blankEcsPolicy(), '{"queries":{"invented":"field"}}')).toThrow('Prometheus 映射无效')
    expect(() => ecsPolicyPayload(blankEcsPolicy(), '{"queries":{"cpu_util":"https://not-a-query"}}')).toThrow('Prometheus 映射无效')
  })
  it('运行时读到旧映射也不解析或回传，原对象不受修改', () => {
    const policy = { ...blankEcsPolicy(), aom: { namespace: 'old.namespace', metrics: { cpu_util: 'old' } } }
    const body = ecsPolicyPayload(policy as unknown as EcsPolicyOut['policy'], 'null')
    expect(body.aom).toBeNull()
    expect(policy.aom.namespace).toBe('old.namespace')
  })
})

describe('ECS 租户策略保存', () => {
  it('只读角色能读取但不能保存；读取失败不能以默认策略覆盖服务器', async () => {
    const { model, reads, saver } = setup(false)
    expect(await model.save()).toBe(false)
    reads[0].result.resolve(ecsPolicy()); await settle()
    expect(model.loaded.value?.tenant_id).toBe(7)
    expect(model.canSave.value).toBe(false); expect(await model.save()).toBe(false)
    expect(saver).not.toHaveBeenCalled()
    const failed = setup()
    failed.reads[0].result.reject(new Error('读取失败')); await settle()
    expect(failed.model.error.value).toBe('读取失败')
    expect(await failed.model.save()).toBe(false)
    expect(failed.saver).not.toHaveBeenCalled()
  })
  it('本地校验拦截无效保存，合法保存不改变 master gate', async () => {
    const { model, reads, saves, saver } = setup()
    reads[0].result.resolve(ecsPolicy(7, false)); await settle()
    model.policy.metrics.interval_sec = 10
    expect(await model.save()).toBe(false); expect(saver).not.toHaveBeenCalled()
    expect(model.validationError.value).toContain('间隔')
    model.policy.metrics.interval_sec = 300
    model.policy.selection = { mode: 'selected', resource_ids: [11] }
    model.policy.inventory.enabled = false
    const saving = model.save()
    expect(saver).toHaveBeenCalledTimes(1)
    expect(saver.mock.calls[0][0]).toBe(7)
    expect(saver.mock.calls[0][1]).toMatchObject({ selection: { mode: 'selected', resource_ids: [11] }, inventory: { enabled: false }, inspection: { enabled: true } })
    expect(saver.mock.calls[0][1]).not.toHaveProperty('master_enabled')
    const response = ecsPolicy(7, false); response.policy = JSON.parse(JSON.stringify(model.policy))
    saves[0].result.resolve(response); expect(await saving).toBe(true)
    expect(model.loaded.value?.master_enabled).toBe(false)
  })
  it('切换租户清空旧选择、取消读取及保存，迟到结果不能恢复旧策略', async () => {
    const { model, tenant, reads, saves } = setup()
    reads[0].result.resolve(ecsPolicy()); await settle()
    model.policy.selection = { mode: 'selected', resource_ids: [11] }
    const saving = model.save()
    tenant.value = 8
    expect(saves[0].signal.aborted).toBe(true)
    expect(model.policy.selection.resource_ids).toEqual([])
    expect(model.canSave.value).toBe(false)
    reads[1].result.resolve(ecsPolicy(8)); await settle()
    saves[0].result.resolve(ecsPolicy(7)); expect(await saving).toBe(false)
    expect(model.loaded.value?.tenant_id).toBe(8)
  })
  it('实例选项按租户过滤，远端搜索迟到响应被拒绝且保留稳定本地 ID', async () => {
    const { model, reads, lookups, tenant } = setup()
    reads[0].result.resolve(ecsPolicy()); await settle()
    model.policy.selection.resource_ids = [99]
    const old = model.loadInstances()
    model.searchInstances('new')
    expect(lookups[0].signal.aborted).toBe(true)
    lookups[1].result.resolve({ items: [ecsInstance(11, 7), ecsInstance(12, 8)], total: 2, page: 1, page_size: 100 }); await settle()
    lookups[0].result.resolve({ items: [ecsInstance(10, 7)], total: 1, page: 1, page_size: 100 }); await old
    expect(model.instanceOptions.value.map(item => item.id)).toEqual([11, 99])
    tenant.value = 8
    expect(model.instanceOptions.value).toEqual([])
    expect(model.policy.selection.resource_ids).toEqual([])
  })
  it('实例标签包含名称、IP、原始资源 ID，搜索后保留已选实例的可识别标签', async () => {
    const { model, reads, lookups } = setup()
    reads[0].result.resolve(ecsPolicy()); await settle()
    model.searchInstances('vm-11')
    lookups[0].result.resolve({ items: [ecsInstance(11, 7)], total: 1, page: 1, page_size: 100 }); await settle()
    expect(model.instanceOptions.value).toEqual([{ id: 11, label: '服务器 11 · 192.0.2.1 · vm-11' }])
    model.policy.selection.resource_ids = [11]
    model.searchInstances('other')
    lookups[1].result.resolve({ items: [], total: 0, page: 1, page_size: 100 }); await settle()
    expect(model.instanceOptions.value).toEqual([{ id: 11, label: '服务器 11 · 192.0.2.1 · vm-11' }])
  })
  it.each(['all', 'critical', 'selected'] as const)('打开策略时保留已有 %s 范围，不自动更改', async mode => {
    const { model, reads } = setup()
    const response = ecsPolicy()
    response.policy.selection = { mode, resource_ids: mode === 'selected' ? [11] : [] }
    reads[0].result.resolve(response); await settle()
    expect(model.policy.selection).toEqual(response.policy.selection)
  })
  it('重复点击保存仅发一次请求，关闭后旧失败不回填', async () => {
    const { model, reads, saves, saver, visible } = setup()
    reads[0].result.resolve(ecsPolicy()); await settle()
    const saving = model.save()
    expect(await model.save()).toBe(false)
    expect(saver).toHaveBeenCalledTimes(1)
    visible.value = false
    saves[0].result.reject(new Error('旧租户保存失败')); expect(await saving).toBe(false)
    expect(model.error.value).toBe('')
  })
})


describe('ECS 执行状态原因', () => {
  it('从服务端 phase_status 展示所有停用原因和真实全局任务编码', () => {
    const phase = { configured_enabled: false, master_enabled: false, task_code: 'ecs_metrics', task_enabled: false, supported: false, effective_enabled: false }
    expect(ecsPhaseReason(phase)).toBe('采集总开关关闭；全局任务关闭（ecs_metrics）；本阶段关闭；任务暂不支持')
    expect(ecsPhaseReason({ ...phase, master_enabled: true, configured_enabled: true, supported: true })).toBe('全局任务关闭（ecs_metrics）')
    expect(ecsPhaseReason({ ...phase, master_enabled: true, configured_enabled: true, supported: true, task_enabled: true, effective_enabled: true })).toBe('已生效')
    expect(ecsPhaseReason(undefined)).toBe('服务端尚未返回执行状态')
    expect(ecsPhaseReason({ ...phase, master_enabled: true, configured_enabled: true, supported: true, task_enabled: true, reason: 'invalid_schedule', cron: 'invalid' })).toBe('定时配置无效')
  })
})
