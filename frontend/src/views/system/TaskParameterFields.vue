<script setup lang="ts">
import { computed } from 'vue'
import type { TaskParameterSchema, TaskParameterValue } from '@/types/task-executions'
import { taskParameterUpdate } from './composables/taskParameters'

const props = defineProps<{
  modelValue: Record<string, TaskParameterValue>
  schema: TaskParameterSchema
  disabled: boolean
  exclude?: string[]
}>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, TaskParameterValue>] }>()
const fields = computed(() => Object.entries(props.schema.properties ?? {}).filter(([key]) => {
  if (props.exclude?.includes(key)) return false
  return !(['active_only', 'all_history'].includes(String(props.modelValue.query_mode)) && ['submitted_window_hours', 'submitted_time_begin', 'submitted_time_end'].includes(key))
}))
const modes = new Map([['active_and_recent', '活动作业 + 近期提交'], ['active_only', '仅活动作业'], ['recent_only', '仅近期提交'], ['all_history', '全历史（显式选择）']])
const enumLabel = (value: TaskParameterValue) => value === null ? '不限制' : modes.get(String(value)) ?? String(value)
function update(key: string, value: TaskParameterValue | undefined) {
  if (props.disabled) return
  emit('update:modelValue', taskParameterUpdate(props.modelValue, key, value, props.schema.properties ?? {}))
}
</script>

<template>
  <div class="task-parameter-fields">
    <el-form-item v-for="[key, spec] in fields" :key="key" :label="spec.title ?? key" class="parameter-field">
      <el-select v-if="spec.enum" :model-value="modelValue[key] ?? ''" :disabled="disabled" :aria-label="spec.title ?? key" @update:model-value="update(key, $event === '' && spec.enum?.includes(null) ? null : $event)"><el-option v-for="option in spec.enum" :key="String(option)" :value="option ?? ''" :label="enumLabel(option)" /></el-select>
      <el-input-number v-else-if="spec.type === 'number' || spec.type === 'integer'" :model-value="typeof modelValue[key] === 'number' ? modelValue[key] : undefined" :min="spec.minimum" :max="spec.maximum" :precision="spec.type === 'integer' ? 0 : undefined" :disabled="disabled" controls-position="right" :aria-label="spec.title ?? key" @update:model-value="update(key, $event)" />
      <el-input v-else-if="spec.type === 'string' || Array.isArray(spec.type) && spec.type.includes('string')" :model-value="typeof modelValue[key] === 'string' ? modelValue[key] : ''" :disabled="disabled" :aria-label="spec.title ?? key" clearable @update:model-value="update(key, $event === '' && Array.isArray(spec.type) && spec.type.includes('null') ? null : $event)" />
      <span v-else class="text-3">此参数类型暂不支持编辑</span>
      <p v-if="spec.description" class="parameter-hint text-3">{{ spec.description }}</p>
    </el-form-item>
  </div>
</template>

<style scoped lang="scss">
.task-parameter-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; :deep(.el-form-item__label) { white-space: normal; line-height: 1.6; height: auto; } :deep(.el-form-item__content) { display: block; min-width: 0; } .el-select, .el-input-number { width: 100%; } }
.parameter-field { min-width: 0; }
.parameter-hint { margin: 6px 0 0; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
@media (max-width: 600px) { .task-parameter-fields { grid-template-columns: 1fr; } }
</style>
