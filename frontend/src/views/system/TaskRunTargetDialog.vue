<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import type { TaskDefinition, TaskExecutionTargets } from '@/types/task-executions'

const props = defineProps<{
  modelValue: boolean
  task: TaskDefinition | null
  targets: TaskExecutionTargets | null
  loading: boolean
  submitting: boolean
  error: string
  canRun: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  submit: [projectRefs: number[] | undefined]
  refresh: []
  existing: [executionId: string]
}>()
const selected = shallowRef<string[]>([])
const available = computed(() => props.targets?.targets ?? [])
const selectedTargets = computed(() => available.value.filter(target => target.executable && selected.value.includes(String(target.project_ref ?? 'global'))))
watch(() => props.targets, (next, previous) => {
  const executable = available.value.filter(target => target.executable).map(target => String(target.project_ref ?? 'global'))
  selected.value = !previous || next?.task_code !== previous.task_code ? executable : selected.value.filter(key => executable.includes(key))
}, { immediate: true })
watch(() => props.modelValue, visible => { if (!visible) selected.value = [] })
function submit() {
  if (!props.canRun || props.submitting || props.loading || !props.targets || !selectedTargets.value.length) return
  const refs = selectedTargets.value.flatMap(target => target.project_ref === null ? [] : [target.project_ref])
  emit('submit', refs.length ? refs : undefined)
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="`${task?.name ?? '巡检任务'} · 执行范围`" width="min(720px, calc(100vw - 24px))" append-to-body destroy-on-close :close-on-click-modal="!submitting" @update:model-value="emit('update:modelValue', $event)">
    <div v-loading="loading" class="run-targets"><p class="target-note text-3">每个租户 / 项目创建独立执行。提交后由后台处理，可关闭页面后回来查看。</p><el-alert v-if="error" :title="error" type="warning" :closable="false"><el-button link @click="emit('refresh')">刷新执行目标</el-button></el-alert>
      <el-checkbox-group v-model="selected" :disabled="!canRun || submitting" aria-label="本次巡检项目">
        <article v-for="target in available" :key="`${target.tenant_id}:${target.project_ref}`" class="run-target"><el-checkbox :value="String(target.project_ref ?? 'global')" :disabled="!target.executable"><span class="target-name">{{ target.tenant_name ?? '平台' }} · {{ target.project_label }}</span></el-checkbox><p v-if="target.reason" class="target-reason text-3">{{ target.reason }}</p><el-button v-if="target.active_execution_id" link type="primary" @click="emit('existing', target.active_execution_id)">打开已有活跃执行</el-button></article>
      </el-checkbox-group>
      <el-empty v-if="!loading && !available.length && !error" description="当前范围内没有可执行项目" :image-size="60" />
    </div>
    <template #footer><span class="selection-count text-3">已选择 {{ selectedTargets.length }} 个目标</span><el-button :disabled="submitting" @click="emit('update:modelValue', false)">关闭</el-button><el-button v-if="canRun" type="primary" :loading="submitting" :disabled="loading || !selectedTargets.length" @click="submit">加入后台队列</el-button></template>
  </el-dialog>
</template>

<style scoped lang="scss">
.run-targets { min-height: 120px; }
.target-note, .target-reason { font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.run-target { padding: 12px 0; border-bottom: 1px solid var(--hi-border); :deep(.el-checkbox) { height: auto; max-width: 100%; } :deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; } }
.target-reason { margin: 4px 0 0; }
.selection-count { display: inline-block; margin-right: 12px; font-size: 12px; }
</style>
