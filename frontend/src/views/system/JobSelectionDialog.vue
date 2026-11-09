<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  discoverSelectionJobs, getJobSelection, getJobSelectionTargets, saveJobSelection,
  type JobDiscovery, type JobSelectionMode, type JobSelectionTarget, type JobSelectionType,
} from '@/api/job-selection'
import { useUserStore } from '@/stores/user'

const props = defineProps<{ modelValue: boolean; resourceType: JobSelectionType; tenantId?: number | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>()
const visible = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const user = useUserStore()
const canManage = computed(() => user.can('collect:manage'))
const targets = ref<JobSelectionTarget[]>([])
const tenantId = shallowRef<number | null>(null)
const projectRef = shallowRef<number | null>(null)
const workspace = shallowRef<string | null>(null)
const mode = shallowRef<JobSelectionMode>('inherit')
const selected = ref<string[]>([])
const rows = ref<JobDiscovery['items']>([])
const total = shallowRef(0)
const page = shallowRef(1)
const pageSize = 100
const keyword = shallowRef('')
const loading = shallowRef(false)
const listing = shallowRef(false)
const saving = shallowRef(false)
const ready = shallowRef(false)
const error = shallowRef('')
const listError = shallowRef('')
const configured = shallowRef(false)
const tenant = computed(() => targets.value.find(item => item.id === tenantId.value))
const projects = computed(() => tenant.value?.projects ?? [])
const project = computed(() => projects.value.find(item => item.id === projectRef.value))
const workspaces = computed(() => project.value?.workspaces ?? [])
const selectedSet = computed(() => new Set(selected.value))
const selectedPreview = computed(() => selected.value.slice(0, 50))
const title = computed(() => `${props.resourceType === 'dgc_realtime_job' ? 'DataArts 实时' : 'DataArts 批'} · 监控作业`)
let version = 0
let listingVersion = 0
let targetsVersion = 0
let disposed = false
let configAbort: AbortController | undefined
let listAbort: AbortController | undefined
let targetAbort: AbortController | undefined

function context() {
  if (disposed || !props.modelValue || tenantId.value == null || projectRef.value == null || workspace.value == null) return null
  return { tenant: tenantId.value, project: projectRef.value, workspace: workspace.value, type: props.resourceType, version }
}
function current(value: NonNullable<ReturnType<typeof context>>) {
  const now = context()
  return !!now && value.version === now.version && value.tenant === now.tenant && value.project === now.project
    && value.workspace === now.workspace && value.type === now.type
}
function invalidate() {
  ++version
  ++listingVersion
  configAbort?.abort()
  listAbort?.abort()
  ready.value = false
  loading.value = listing.value = saving.value = false
  selected.value = []
  mode.value = 'inherit'
  rows.value = []
  total.value = 0
  page.value = 1
  keyword.value = error.value = listError.value = ''
}

watch([() => props.modelValue, () => props.resourceType, () => props.tenantId], async ([open], _previous, cleanup) => {
  invalidate()
  const mine = ++targetsVersion
  targetAbort?.abort()
  const controller = new AbortController()
  targetAbort = controller
  cleanup(() => controller.abort())
  targets.value = []
  tenantId.value = null
  if (!open) return
  loading.value = true
  try {
    const result = await getJobSelectionTargets(controller.signal)
    if (disposed || mine !== targetsVersion || !props.modelValue) return
    targets.value = result
    const requested = props.tenantId
    tenantId.value = requested != null ? result.find(item => item.id === requested)?.id ?? null
      : result.length === 1 ? result[0].id : null
    if (requested != null && tenantId.value == null) error.value = '当前租户没有可配置的项目或不在授权范围内'
  } catch {
    if (!controller.signal.aborted && mine === targetsVersion) error.value = '无法加载获授权的租户和项目，请重试'
  } finally { if (mine === targetsVersion) loading.value = false }
}, { immediate: true, flush: 'sync' })

watch(tenantId, () => { projectRef.value = projects.value[0]?.id ?? null }, { flush: 'sync' })
watch(projectRef, () => { workspace.value = workspaces.value[0] ?? null }, { flush: 'sync' })
watch([tenantId, projectRef, workspace, () => props.resourceType, () => props.modelValue], async () => {
  invalidate()
  const request = context()
  if (!request) return
  const controller = new AbortController()
  configAbort = controller
  loading.value = true
  try {
    const result = await getJobSelection(request.tenant, request.type, request.project, request.workspace, controller.signal)
    if (!current(request)) return
    mode.value = result.mode
    selected.value = [...result.jobs]
    configured.value = result.configured
    ready.value = true
    if (result.invalid) error.value = '已保存的监控名单无效，当前不采集；请重新选择并保存'
  } catch {
    if (!controller.signal.aborted && current(request)) error.value = '读取监控名单失败；未改变范围，请重试'
  } finally { if (current(request)) loading.value = false }
}, { flush: 'sync' })

async function loadJobs(nextPage = 1) {
  const request = context()
  if (!request || !canManage.value || !ready.value || saving.value) return
  const mine = ++listingVersion
  listAbort?.abort()
  const controller = new AbortController()
  listAbort = controller
  listing.value = true
  listError.value = ''
  try {
    const result = await discoverSelectionJobs(request.tenant, request.type, {
      project_ref: request.project, workspace: request.workspace, page: nextPage, page_size: pageSize, keyword: keyword.value,
    }, controller.signal)
    if (!current(request) || mine !== listingVersion) return
    rows.value = result.items
    total.value = result.total
    page.value = result.page
  } catch {
    if (!controller.signal.aborted && current(request) && mine === listingVersion) {
      rows.value = []
      total.value = 0
      listError.value = '查询云端作业失败；已选名单保持不变，可重试'
    }
  } finally { if (current(request) && mine === listingVersion) listing.value = false }
}

function toggleJob(name: string, checked: boolean) {
  if (!canManage.value || saving.value) return
  mode.value = 'selected'
  selected.value = checked ? [...new Set([...selected.value, name])] : selected.value.filter(item => item !== name)
}

async function save() {
  const request = context()
  if (!request || !canManage.value || !ready.value || saving.value || mode.value === 'inherit') return
  const body = { project_ref: request.project, workspace: request.workspace, mode: mode.value,
    jobs: mode.value === 'selected' ? [...selected.value].sort() : [] }
  saving.value = true
  try {
    const result = await saveJobSelection(request.tenant, request.type, body)
    if (!current(request)) return
    ElMessage.success(`已保存监控范围${result.closed_alarms ? `，已行政关闭 ${result.closed_alarms} 条范围外告警` : ''}`)
    emit('saved')
    visible.value = false
  } finally { if (current(request)) saving.value = false }
}
onBeforeUnmount(() => { disposed = true; ++targetsVersion; invalidate(); targetAbort?.abort() })
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="min(800px, calc(100vw - 24px))" class="job-selection-dialog" append-to-body :close-on-click-modal="false">
    <div v-loading="loading" class="job-selection">
      <p class="text-3">仅设置本平台监控范围，不启停云端作业、不改变采集开关。最终范围为监控名单与通用采集范围的交集。</p>
      <div class="target-row">
        <el-select v-model="tenantId" aria-label="监控租户" placeholder="选择租户" :disabled="saving || tenantId != null && props.tenantId != null">
          <el-option v-for="item in targets" :key="item.id" :value="item.id" :label="item.name" />
        </el-select>
        <el-select v-model="projectRef" aria-label="监控项目" placeholder="选择项目" :disabled="saving || !tenant">
          <el-option v-for="item in projects" :key="item.id" :value="item.id" :label="`${item.name} · ${item.project_id}`" />
        </el-select>
        <el-select v-model="workspace" aria-label="监控工作空间" placeholder="选择工作空间 ID" :disabled="saving || !project">
          <el-option v-for="item in workspaces" :key="item" :value="item" :label="item || 'default（未传 workspace 请求头）'" />
        </el-select>
      </div>
      <el-alert v-if="error" type="warning" :title="error" :closable="false" />
      <template v-if="ready">
        <el-radio-group v-model="mode" class="job-modes" aria-label="监控作业模式" :disabled="!canManage || saving">
          <el-radio v-if="!configured" value="inherit" disabled>未设置，沿用通用范围</el-radio>
          <el-radio value="selected">仅监控指定作业</el-radio>
          <el-radio value="all">监控此工作空间全部作业</el-radio>
        </el-radio-group>
        <el-alert v-if="mode === 'selected' && !selected.length" type="warning" title="指定名单为空：保存后不查询、不监控任何作业，不会退回全量。" :closable="false" />
        <p v-else-if="mode === 'all'" class="text-3">保存后允许此工作空间内全部作业，仍受通用采集范围与采集开关限制。</p>
        <div v-if="canManage" class="search-row">
          <el-input v-model="keyword" aria-label="按名称查找云端作业" placeholder="输入作业名称查询" clearable :disabled="saving" @keyup.enter="loadJobs(1)" />
          <el-button :loading="listing" :disabled="saving" @click="loadJobs(1)">查询云端作业</el-button>
        </div>
        <el-alert v-if="listError" type="error" :title="listError" :closable="false" />
        <el-table v-if="rows.length || listing" v-loading="listing" :data="rows" row-key="name" max-height="270" size="small">
          <el-table-column label="监控" width="65"><template #default="{ row }">
            <el-checkbox :model-value="mode === 'all' || selectedSet.has(row.name)" :aria-label="`监控 ${row.name}`" :disabled="!canManage || saving || mode === 'all'"
                         @change="value => toggleJob(row.name, !!value)" />
          </template></el-table-column>
          <el-table-column prop="name" label="作业名称" min-width="260" />
          <el-table-column prop="job_type" label="处理模式" width="120" />
        </el-table>
        <el-pagination v-if="total > pageSize" :current-page="page" :page-size="pageSize" :total="total" layout="total, prev, pager, next" :disabled="listing || saving" @current-change="loadJobs" />
        <div v-if="mode === 'selected'" class="selection-summary">
          <b>已选 {{ selected.length }} 项（跨页保留）</b>
          <el-button v-if="canManage && selected.length" link type="danger" :disabled="saving" @click="selected = []">清空名单</el-button>
          <div class="selected-jobs"><el-tag v-for="name in selectedPreview" :key="name" :closable="canManage && !saving" @close="toggleJob(name, false)">{{ name }}</el-tag></div>
          <span v-if="selected.length > selectedPreview.length" class="text-3">另有 {{ selected.length - selectedPreview.length }} 项已选；可按名称查询后取消勾选，所有勾选均会保存。</span>
        </div>
        <p class="text-3">保存会同步标记范围外资源，并行政关闭其活动告警；未保存的勾选不生效。云端列表仅查询作业定义，不请求作业运行状态。</p>
      </template>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button v-if="canManage" type="primary" :loading="saving" :disabled="!ready || mode === 'inherit'" @click="save">保存监控范围</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
:global(.job-selection-dialog) { display: flex; flex-direction: column; max-height: calc(100dvh - 24px); margin: 12px auto; }
:global(.job-selection-dialog .el-dialog__header), :global(.job-selection-dialog .el-dialog__footer) { flex: none; }
:global(.job-selection-dialog .el-dialog__body) { flex: 1; min-height: 0; overflow: auto; }
.job-selection { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.target-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) minmax(0, 1.4fr); gap: 10px; }
.job-modes { display: flex; flex-wrap: wrap; gap: 8px 16px; }
.job-modes :deep(.el-radio) { margin-right: 0; max-width: 100%; }
.search-row { display: flex; gap: 10px; }
.selection-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.selected-jobs { display: flex; flex-wrap: wrap; gap: 6px; width: 100%; max-height: 150px; overflow-y: auto; }
@media (max-width: 600px) {
  .target-row { grid-template-columns: minmax(0, 1fr); }
  .job-modes :deep(.el-radio) { height: auto; min-height: 32px; white-space: normal; }
  .job-modes :deep(.el-radio__label) { white-space: normal; overflow-wrap: anywhere; }
}
</style>
