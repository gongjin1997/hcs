<script setup lang="ts">
import { computed } from 'vue'
import type { TaskExecution } from '@/types/task-executions'
import TaskExecutionCard from './TaskExecutionCard.vue'

const props = defineProps<{
  items: TaskExecution[]
  mode: 'active' | 'recent'
  queueStatus: 'QUEUED' | 'RUNNING'
  total: number
  page: number
  pageSize: number
  loading: boolean
  error: string
  canCancel: boolean
  canRetry: boolean
  pendingActions: Record<string, 'cancel' | 'retry' | undefined>
}>()
const emit = defineEmits<{
  'update:mode': [value: 'active' | 'recent']
  'update:queueStatus': [value: 'QUEUED' | 'RUNNING']
  'update:page': [value: number]
  open: [execution: TaskExecution]
  cancel: [execution: TaskExecution]
  retry: [execution: TaskExecution]
  refresh: []
}>()
const groups = computed(() => {
  const grouped = new Map<string, TaskExecution[]>()
  for (const execution of props.items) {
    const key = execution.request_id || execution.execution_id
    const items = grouped.get(key)
    if (items) items.push(execution)
    else grouped.set(key, [execution])
  }
  return [...grouped].map(([requestId, items]) => ({ requestId, items }))
})
</script>

<template>
  <section class="execution-list" aria-label="巡检执行工作区">
    <div class="execution-toolbar"><el-radio-group :model-value="mode" aria-label="执行视图" @update:model-value="emit('update:mode', $event as 'active' | 'recent')"><el-radio-button value="active">运行队列</el-radio-button><el-radio-button value="recent">最近执行</el-radio-button></el-radio-group><span class="text-3">共 {{ total }} 条</span><el-button :loading="loading" @click="emit('refresh')">刷新状态</el-button></div>
    <el-radio-group v-if="mode === 'active'" :model-value="queueStatus" aria-label="队列状态" class="queue-status" @update:model-value="emit('update:queueStatus', $event as 'QUEUED' | 'RUNNING')"><el-radio-button value="QUEUED">排队中 · QUEUED</el-radio-button><el-radio-button value="RUNNING">巡检中 · RUNNING</el-radio-button></el-radio-group>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon class="feed-error">当前显示上次后端状态；网络中断不会改变执行结果。</el-alert>
    <div v-loading="loading && !items.length" class="execution-groups">
      <el-empty v-if="!loading && !items.length" :description="error ? '执行状态暂不可用，请重试读取' : mode === 'active' ? queueStatus === 'QUEUED' ? '当前范围内没有排队中的巡检' : '当前范围内没有运行中的巡检' : '当前范围内暂无执行记录'" :image-size="72" />
      <section v-for="group in groups" :key="group.requestId" class="execution-group" aria-label="一次请求的执行批次"><div class="group-heading"><span>请求批次</span><code>{{ group.requestId }}</code><span class="text-3">本页 {{ group.items.length }} 条执行</span></div><div class="execution-grid"><TaskExecutionCard v-for="execution in group.items" :key="execution.execution_id" :execution="execution" :can-cancel="canCancel" :can-retry="canRetry" :pending-action="pendingActions[execution.execution_id]" @open="emit('open', execution)" @cancel="emit('cancel', execution)" @retry="emit('retry', execution)" /></div></section>
    </div>
    <el-pagination v-if="total > pageSize" layout="prev, pager, next" :total="total" :page-size="pageSize" :current-page="page" @update:current-page="emit('update:page', $event)" />
  </section>
</template>

<style scoped lang="scss">
.execution-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 16px; font-size: 12px; > .el-button { margin-left: auto; } }
.queue-status { margin-bottom: 14px; }
.feed-error { margin-bottom: 14px; }
.execution-groups { min-height: 120px; }
.execution-group { margin-bottom: 20px; }
.group-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 12px; code { color: var(--hi-text-3); overflow-wrap: anywhere; } }
.execution-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 12px; }
.el-pagination { justify-content: flex-end; }
</style>
