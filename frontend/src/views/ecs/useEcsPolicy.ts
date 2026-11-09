import { computed, onScopeDispose, reactive, shallowRef, watch } from 'vue'
import { useLatestRequest } from '../../composables/useLatestRequest'
import type { EcsInstance, EcsInstancesFetcher, EcsPolicyFetcher, EcsPolicyOut, EcsPolicySaver } from '../../types/ecs'
import { blankEcsPolicy, ecsPolicyPayload } from './ecs-policy'
import { errorText } from './ecs-utils'

export function useEcsPolicy(options: {
  tenantId: () => number | null; visible: () => boolean; canManage: () => boolean
  fetcher: EcsPolicyFetcher; saver: EcsPolicySaver; instancesFetcher: EcsInstancesFetcher
}) {
  const request = useLatestRequest(), saveRequest = useLatestRequest(), instancesRequest = useLatestRequest()
  const policy = reactive(blankEcsPolicy())
  const loaded = shallowRef<EcsPolicyOut | null>(null)
  const prometheusJson = shallowRef('null')
  const error = shallowRef(''), validationError = shallowRef(''), optionsError = shallowRef('')
  const instances = shallowRef<EcsInstance[]>([])
  const instancesTotal = shallowRef(0), instancePage = shallowRef(1), keyword = shallowRef('')
  const knownInstances = shallowRef(new Map<number, EcsInstance>())
  let controller: AbortController | null = null, saveController: AbortController | null = null, instancesController: AbortController | null = null
  const canSave = computed(() => options.canManage() && !!loaded.value && options.tenantId() === loaded.value.tenant_id && !request.loading.value && !saveRequest.loading.value)
  const instanceOptions = computed(() => {
    const label = (item: EcsInstance) => `${item.name || '未命名'} · ${item.ip || '无 IP'} · ${item.resource_id}`
    const rows = instances.value.map(item => ({ id: item.id, label: label(item) }))
    const selected = policy.selection.resource_ids.filter(id => !rows.some(row => row.id === id)).map(id => {
      const item = knownInstances.value.get(id)
      return { id, label: item ? label(item) : `已选实例 · 本地台账 #${id}` }
    })
    return [...rows, ...selected]
  })
  function apply(value: EcsPolicyOut) {
    loaded.value = { ...value, policy: { ...value.policy, aom: null } }
    Object.assign(policy, JSON.parse(JSON.stringify({ ...value.policy, aom: null })))
    prometheusJson.value = JSON.stringify(value.policy.prometheus, null, 2)
  }
  function cancel() { request.invalidate(); saveRequest.invalidate(); instancesRequest.invalidate(); controller?.abort(); saveController?.abort(); instancesController?.abort() }
  async function load() {
    cancel()
    loaded.value = null
    error.value = ''; validationError.value = ''; optionsError.value = ''
    instances.value = []; instancesTotal.value = 0; instancePage.value = 1; keyword.value = ''
    knownInstances.value = new Map()
    Object.assign(policy, blankEcsPolicy()); prometheusJson.value = 'null'
    const tenantId = options.tenantId()
    if (!options.visible() || !tenantId) return
    const local = new AbortController(); controller = local
    try { await request.run(() => options.fetcher(tenantId, { tenantId, signal: local.signal }), apply) }
    catch (cause) { if (!local.signal.aborted) error.value = errorText(cause, '策略读取失败') }
  }
  async function loadInstances(more = false) {
    instancesRequest.invalidate(); instancesController?.abort()
    const tenantId = options.tenantId()
    if (!options.visible() || !tenantId || !loaded.value) return
    const nextPage = more ? instancePage.value + 1 : 1
    const query = { keyword: keyword.value.trim() || undefined, page: nextPage, page_size: 100 }
    if (!more) instances.value = []
    const local = new AbortController(); instancesController = local; optionsError.value = ''
    try {
      await instancesRequest.run(() => options.instancesFetcher(query, { tenantId, signal: local.signal }), value => {
        const scoped = value.items.filter(item => item.tenant_id === tenantId)
        const merged = more ? [...instances.value, ...scoped] : scoped
        instances.value = [...new Map(merged.map(item => [item.id, item])).values()]
        knownInstances.value = new Map([...knownInstances.value, ...scoped.map(item => [item.id, item] as const)])
        instancesTotal.value = value.total; instancePage.value = nextPage
      })
    } catch (cause) { if (!local.signal.aborted) optionsError.value = errorText(cause, '实例选项加载失败') }
  }
  function searchInstances(value: string) { keyword.value = value; void loadInstances() }
  async function save() {
    if (!canSave.value || !options.tenantId()) return false
    validationError.value = ''; error.value = ''
    let body
    try { body = ecsPolicyPayload(policy, prometheusJson.value) }
    catch (cause) { validationError.value = errorText(cause, '请检查配置'); return false }
    const tenantId = options.tenantId()!
    const local = new AbortController(); saveController = local
    try { return await saveRequest.run(() => options.saver(tenantId, body, { tenantId, signal: local.signal }), apply) }
    catch (cause) { if (!local.signal.aborted) error.value = errorText(cause, '保存失败'); return false }
  }
  watch([options.tenantId, options.visible], () => { void load() }, { immediate: true, flush: 'sync' })
  onScopeDispose(cancel)
  return { policy, loaded, prometheusJson, error, validationError, optionsError, instances, instancesTotal, instanceOptions,
    keyword, loading: request.loading, saving: saveRequest.loading, instancesLoading: instancesRequest.loading, canSave, load, save, loadInstances, searchInstances }
}
