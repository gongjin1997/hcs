<script setup lang="ts">
import { computed, onBeforeUnmount, shallowRef, watch } from 'vue'
import { getCollectScopes, type InspectionTask } from '@/api'
import type { CollectScopes } from '@/types'
import CollectScopeDialog from './CollectScopeDialog.vue'
import { scopeSummary } from './collect-scope'

const props = defineProps<{
  modelValue: boolean
  task: InspectionTask | null
  tenantId: number | null
  canManage: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>()
const scopes = shallowRef<CollectScopes | null>(null)
const loading = shallowRef(false)
const error = shallowRef('')
const selectedTenant = shallowRef(0)
const selectedType = shallowRef('')
const editorVisible = shallowRef(false)
const availableTypes = computed(() => scopes.value?.types.filter(type => props.task?.types.includes(type.code)) ?? [])
const resourceType = computed(() => availableTypes.value.find(type => type.code === selectedType.value) ?? null)
const tenantName = computed(() => scopes.value?.tenants.find(tenant => tenant.id === selectedTenant.value)?.name ?? '全局默认')
const canEdit = computed(() => props.canManage && (selectedTenant.value !== 0 || !!scopes.value?.global_editable))
const summary = computed(() => {
  const type = resourceType.value
  if (!type) return '暂无范围信息'
  if (selectedTenant.value === 0) return type.global.rules.length ? `${type.global.rules.length} 条全局规则` : '全局默认不限制资源'
  const cell = type.tenants[String(selectedTenant.value)]
  return cell ? scopeSummary(cell).text : '后端未返回此租户的范围配置'
})
let version = 0
let disposed = false

async function load() {
  const request = ++version
  editorVisible.value = false
  scopes.value = null
  error.value = ''
  if (!props.modelValue || !props.task) { loading.value = false; return }
  loading.value = true
  try {
    const result = await getCollectScopes()
    if (disposed || request !== version) return
    scopes.value = result
    selectedTenant.value = result.tenants.some(tenant => tenant.id === props.tenantId) ? props.tenantId!
      : result.global_editable ? 0 : result.tenants[0]?.id ?? 0
    selectedType.value = availableTypes.value[0]?.code ?? ''
  } catch {
    if (!disposed && request === version) error.value = '资源范围读取失败，请重试'
  } finally {
    if (!disposed && request === version) loading.value = false
  }
}
watch(() => [props.modelValue, props.task?.code, props.tenantId] as const, load, { immediate: true, flush: 'sync' })
watch([selectedTenant, selectedType], () => { editorVisible.value = false })
onBeforeUnmount(() => { disposed = true; ++version })
</script>

<template>
  <el-dialog :model-value="modelValue" :title="`${task?.name ?? '任务'} · 资源范围`" width="min(620px, calc(100vw - 24px))" append-to-body destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div v-loading="loading" class="task-scope">
      <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button link type="primary" @click="load">重试读取</el-button></el-alert>
      <template v-else-if="scopes">
        <el-form label-position="top">
          <el-form-item label="租户范围"><el-select v-model="selectedTenant" aria-label="资源范围租户"><el-option v-if="scopes.global_editable" :value="0" label="全局默认" /><el-option v-for="tenant in scopes.tenants" :key="tenant.id" :value="tenant.id" :label="tenant.name" /></el-select></el-form-item>
          <el-form-item label="资源类型"><el-select v-model="selectedType" aria-label="资源范围类型"><el-option v-for="type in availableTypes" :key="type.code" :value="type.code" :label="type.name" /></el-select></el-form-item>
        </el-form>
        <p class="scope-summary">{{ summary }}</p>
        <p class="scope-note text-3">仅影响本地巡检与告警。执行还受租户、项目和采集开关约束。</p>
        <el-button v-if="resourceType" type="primary" @click="editorVisible = true">{{ canEdit ? '编辑资源范围' : '查看资源范围' }}</el-button>
        <el-empty v-else description="此任务没有可配置的资源范围" :image-size="60" />
      </template>
    </div>
    <CollectScopeDialog v-if="scopes && resourceType" :key="`${selectedTenant}:${selectedType}`" v-model="editorVisible" :tenant-id="selectedTenant" :tenant-name="tenantName" :type="resourceType" :can-manage="canEdit" :limits="scopes.limits" @saved="emit('saved'); load()" />
  </el-dialog>
</template>

<style scoped lang="scss">
.task-scope { min-height: 100px; .el-select { width: 100%; } }
.scope-summary { font-size: 14px; font-weight: 600; }
.scope-note { font-size: 12px; line-height: 1.7; }
</style>
