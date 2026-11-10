<script setup lang="ts">
import type { TaskExecution } from '@/types/task-executions'
import { fmtTime } from '@/utils/format'
import TaskExecutionCard from './TaskExecutionCard.vue'

defineProps<{
  modelValue: boolean
  execution: TaskExecution | null
  loading: boolean
  error: string
  canCancel: boolean
  canRetry: boolean
  pendingAction?: 'cancel' | 'retry' | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; refresh: []; cancel: []; retry: [] }>()
</script>

<template>
  <el-drawer :model-value="modelValue" title="单次巡检执行" size="min(760px, 100vw)" append-to-body destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div v-loading="loading && !execution" class="execution-detail">
      <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon><span>状态以最后一次后端响应为准。</span><el-button link @click="emit('refresh')">重新连接</el-button></el-alert>
      <template v-if="execution">
        <TaskExecutionCard :execution="execution" :can-cancel="canCancel" :can-retry="canRetry" :pending-action="pendingAction" :show-open="false" @cancel="emit('cancel')" @retry="emit('retry')" />
        <section class="detail-section" aria-label="执行标识与时间"><h3>执行信息</h3><dl class="detail-facts">
          <div><dt>执行 ID</dt><dd class="mono">{{ execution.execution_id }}</dd></div><div><dt>关联运行</dt><dd class="mono">#{{ execution.run_id }}</dd></div>
          <div><dt>请求批次</dt><dd class="mono">{{ execution.request_id }}</dd></div><div><dt>整轮超时</dt><dd>{{ execution.timeout_sec }} 秒</dd></div>
          <div><dt>入队时间</dt><dd>{{ fmtTime(execution.queued_at) }}</dd></div><div><dt>开始时间</dt><dd>{{ execution.started_at ? fmtTime(execution.started_at) : '尚未开始' }}</dd></div>
          <div><dt>结束时间</dt><dd>{{ execution.finished_at ? fmtTime(execution.finished_at) : '尚未结束' }}</dd></div><div><dt>后台截止时间</dt><dd>{{ execution.deadline_at ? fmtTime(execution.deadline_at) : '尚未设置' }}</dd></div>
        <div><dt>资源范围快照</dt><dd>后端未提供；当前规则可在任务的资源范围入口查看。</dd></div></dl><p class="detail-note text-3">停止操作只取消本地巡检，不会终止云端作业。超时、中断和最终结果均由后台判定。</p></section>
        <section class="detail-section" aria-label="执行阶段记录"><h3>阶段记录</h3><el-timeline v-if="execution.history.length"><el-timeline-item v-for="(event, index) in execution.history" :key="index" :timestamp="event.at || event.timestamp ? fmtTime(event.at || event.timestamp!) : ''"><b>{{ event.phase_label || event.phase || '阶段更新' }}</b><p v-if="event.message" class="event-message">{{ event.message }}</p><span v-if="event.current != null || event.total != null" class="text-3">{{ event.current ?? '—' }} / {{ event.total ?? '—' }} {{ event.unit ?? '' }}</span></el-timeline-item></el-timeline><p v-else class="detail-note text-3">后端尚未上报阶段记录。</p></section>
      </template>
      <el-empty v-else-if="!loading && !error" description="没有可显示的执行详情" :image-size="70" />
    </div>
  </el-drawer>
</template>

<style scoped lang="scss">
.execution-detail { min-width: 0; .el-alert { margin-bottom: 14px; } }
.detail-section { margin-top: 22px; h3 { font-size: 14px; margin: 0 0 14px; } }
.detail-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 0; div { min-width: 0; } dt { font-size: 12px; color: var(--hi-text-3); } dd { margin: 5px 0 0; font-size: 12px; overflow-wrap: anywhere; } }
.detail-note, .event-message { font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
@media (max-width: 600px) { .detail-facts { grid-template-columns: 1fr; } }
</style>
