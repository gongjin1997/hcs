<script setup lang="ts">
import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { gaussApi, gaussDirectoryApi } from '@/api/gaussdb'
import PageHeader from '@/components/PageHeader.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { monitoringListQuery, readMonitoringContext, resourceViewerScopeKey } from '@/config/resource-navigation'
import type { GaussInstance, GaussPanel, GaussProjectOption } from '@/types/gaussdb'
import { errorText, GAUSS_PANELS } from './gauss-utils'
import { useGaussPanel } from './useGaussPanel'
import GaussInstanceList from './GaussInstanceList.vue'
import GaussWorkspace from './GaussWorkspace.vue'

const app = useAppStore(), user = useUserStore(), route = useRoute(), router = useRouter()
const scopeKey = computed(() => resourceViewerScopeKey(user))
const canRun = computed(() => user.can('ledger:view', 'task:run'))
const model = useGaussPanel({ tenantId: () => app.tenantId, canView: () => !!user.user && user.can('ledger:view'),
  canRun: () => canRun.value, canExport: () => user.can('ledger:export'), scopeKey: () => scopeKey.value, api: gaussApi })
const { selected, listLoading, loading, listError } = model
const projects = shallowRef<GaussProjectOption[]>([])
const projectsError = shallowRef('')
const opening = shallowRef(false)
const workspace = shallowRef<InstanceType<typeof GaussWorkspace> | null>(null)
let projectController: AbortController | null = null
let navigationGeneration = 0
const tabParam = (value: unknown): GaussPanel => GAUSS_PANELS.some(item => item.key === value) ? value as GaussPanel : 'overview'

async function loadProjects() {
  projectController?.abort()
  const controller = new AbortController(), tenantId = app.tenantId
  projectController = controller; projects.value = []; projectsError.value = ''
  if (!user.user || !user.can('ledger:view')) return
  try {
    const result = await gaussDirectoryApi.projects({ tenantId, signal: controller.signal })
    if (!controller.signal.aborted && app.tenantId === tenantId) projects.value = result
  } catch (cause) { if (!controller.signal.aborted) projectsError.value = errorText(cause, '项目列表加载失败') }
}
// 打开实例、切换标签都只读本地记录；慢 SQL、会话、巡检报告和实时磁盘 / 组件在对应标签里点击查询后才请求 HCS。
function open(row: GaussInstance) {
  return router.push({ query: { ...route.query, instance_id: String(row.id), tenant_id: String(row.tenant_id), tab: 'overview' } })
}
function listQuery() {
  return monitoringListQuery(route.query)
}
function back() { return router.push({ query: listQuery() }) }
function changePanel(value: string | number) { return router.replace({ query: { ...route.query, tab: String(value) } }) }
watch(() => [route.query.instance_id, route.query.tenant_id, route.query.tab, app.tenantId, scopeKey.value] as const, async ([rawId, rawTenant, tab, tenantId, identity], previous) => {
  const generation = ++navigationGeneration
  if (previous && (previous[3] !== tenantId || previous[4] !== identity)) {
    model.clearSelection(); opening.value = false
    if (rawId != null || rawTenant != null || tab != null) await router.replace({ query: listQuery() })
    return
  }
  const { context, error } = readMonitoringContext(route.query, app.tenantId, user, 'ledger:view')
  if (!context) { model.clearSelection(); opening.value = false; if (error) listError.value = error; return }
  const { instanceId: id, tenantId: tenant } = context
  if (selected.value?.id !== id || selected.value.tenant_id !== tenant) {
    opening.value = true
    const ok = await model.openInstance(id, gaussDirectoryApi.instance, false, tenant)
    if (generation !== navigationGeneration) return
    opening.value = false
    if (!ok) return
    if (selected.value?.tenant_id !== tenant) {
      model.clearSelection(); listError.value = '实例链接的租户信息不匹配'; return
    }
  }
  model.setPanel(tabParam(tab))
}, { immediate: true })
watch(() => [app.tenantId, scopeKey.value], () => { void loadProjects() }, { immediate: true, flush: 'sync' })
/** 顶栏刷新：重读本地实例记录，再由工作区刷新本标签已有的结果（概览的存储趋势；已查询过的在线结果重新查询）。 */
async function refresh() {
  if (opening.value) return
  if (selected.value) {
    const ok = await model.openInstance(selected.value.id, gaussDirectoryApi.instance, true, selected.value.tenant_id ?? undefined)
    if (ok) await workspace.value?.refresh()
  } else await Promise.all([model.loadInstances(), loadProjects()])
}
onScopeDispose(() => { navigationGeneration++; projectController?.abort() })
</script>

<template>
  <div class="gauss-page">
    <PageHeader title="GaussDB 监控" :refresh="refresh" :refreshing="listLoading || loading || opening" />
    <section v-if="opening" class="hi-card" aria-label="加载实例"><el-skeleton :rows="7" animated /></section>
    <GaussWorkspace v-else-if="selected" ref="workspace" :model="model" :can-run="canRun" @back="back" @panel="changePanel" />
    <GaussInstanceList v-else :model="model" :projects="projects" :projects-error="projectsError" @open="open" @retry-projects="loadProjects" />
  </div>
</template>

<style scoped>
.gauss-page { min-width: 0; }
</style>
