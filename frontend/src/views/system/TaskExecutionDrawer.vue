<script setup lang="ts">
import { computed } from 'vue'
import type { TaskExecution } from '@/types/task-executions'
import { fmtTime } from '@/utils/format'
import TaskExecutionCard from './TaskExecutionCard.vue'

const props = defineProps<{
  modelValue: boolean
  execution: TaskExecution | null
  loading: boolean
  error: string
  canCancel: boolean
  canRetry: boolean
  pendingAction?: 'cancel' | 'retry' | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; refresh: []; cancel: []; retry: [] }>()
const queryModeLabels: Record<string, string> = {
  active_and_recent: '活动作业 + 近期提交', active_only: '仅活动作业',
  recent_only: '仅近期提交', all_history: '全历史',
}
function formatUtc(value: number) {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'medium' }).format(value)
}
const mrsOptions = computed(() => {
  const options = props.execution?.options?.mrs
  if (!options) return []
  const rows: { label: string; value: string }[] = [
    { label: '查询模式', value: queryModeLabels[options.query_mode] ?? options.query_mode },
    { label: '每页条数', value: `${options.page_size} 条` },
    { label: '分页预算', value: `${options.max_pages} 页` },
    { label: '返回条数预算', value: `${options.max_items} 条` },
    { label: '调用预算', value: `${options.max_calls} 次` },
    { label: '集群上限', value: `${options.max_clusters} 个` },
    { label: '分页方式', value: options.job_offset_mode === 'page' ? '页码' : '偏移量' },
    { label: 'elapsed_time 单位', value: options.elapsed_time_unit === 'milliseconds' ? '毫秒' : options.elapsed_time_unit === 'seconds' ? '秒' : '局点未确认' },
  ]
  if (options.submitted_window_hours != null) rows.splice(1, 0, { label: '近期窗口', value: `${options.submitted_window_hours} 小时` })
  else if (options.submitted_time_begin != null) rows.splice(1, 0, {
    label: '提交时间窗口',
    value: `${formatUtc(options.submitted_time_begin)} 至 ${options.submitted_time_end == null ? '本轮开始时间' : formatUtc(options.submitted_time_end)} UTC`,
  })
  if (props.execution?.options?.p90_min_samples != null) rows.push({ label: 'P90 最少样本数', value: `${props.execution.options.p90_min_samples}` })
  return rows
})
</script>

<template>
  <el-drawer :model-value="props.modelValue" title="单次巡检执行" size="min(760px, 100vw)" append-to-body destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div v-loading="props.loading && !props.execution" class="execution-detail">
      <el-alert v-if="props.error" :title="props.error" type="warning" :closable="false" show-icon><span>状态以最后一次后端响应为准。</span><el-button link @click="emit('refresh')">重新连接</el-button></el-alert>
      <template v-if="props.execution">
        <TaskExecutionCard :execution="props.execution" :can-cancel="props.canCancel" :can-retry="props.canRetry" :pending-action="props.pendingAction" :show-open="false" @cancel="emit('cancel')" @retry="emit('retry')" />
        <section class="detail-section" aria-label="执行标识与时间"><h3>执行信息</h3><dl class="detail-facts">
          <div><dt>执行 ID</dt><dd class="mono">{{ props.execution.execution_id }}</dd></div><div><dt>关联运行</dt><dd class="mono">#{{ props.execution.run_id }}</dd></div>
          <div><dt>请求批次</dt><dd class="mono">{{ props.execution.request_id }}</dd></div><div><dt>整轮超时</dt><dd>{{ props.execution.timeout_sec }} 秒</dd></div>
          <div><dt>入队时间</dt><dd>{{ fmtTime(props.execution.queued_at) }}</dd></div><div><dt>开始时间</dt><dd>{{ props.execution.started_at ? fmtTime(props.execution.started_at) : '尚未开始' }}</dd></div>
          <div><dt>结束时间</dt><dd>{{ props.execution.finished_at ? fmtTime(props.execution.finished_at) : '尚未结束' }}</dd></div><div><dt>后台截止时间</dt><dd>{{ props.execution.deadline_at ? fmtTime(props.execution.deadline_at) : '尚未设置' }}</dd></div>
          <div><dt>本次目标</dt><dd>{{ props.execution.tenant_name ? `${props.execution.tenant_name} · ` : '' }}{{ props.execution.project_label }}</dd></div>
        </dl><p class="detail-note text-3">停止操作只取消本地巡检，不会终止云端作业。</p></section>
        <section v-if="mrsOptions.length" class="detail-section" aria-label="本次参数"><h3>本次参数</h3><dl class="detail-facts">
          <div v-for="item in mrsOptions" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
        </dl></section>
        <section class="detail-section" aria-label="执行阶段记录"><h3>阶段记录</h3><el-timeline v-if="props.execution.history.length"><el-timeline-item v-for="(event, index) in props.execution.history" :key="index" :timestamp="event.at || event.timestamp ? fmtTime(event.at || event.timestamp!) : ''"><b>{{ event.phase_label || event.phase || '阶段更新' }}</b><p v-if="event.message" class="event-message">{{ event.message }}</p><span v-if="event.current != null || event.total != null" class="text-3">{{ event.current ?? '—' }} / {{ event.total ?? '—' }} {{ event.unit ?? '' }}</span></el-timeline-item></el-timeline><p v-else class="detail-note text-3">后端尚未上报阶段记录。</p></section>
      </template>
      <el-empty v-else-if="!props.loading && !props.error" description="没有可显示的执行详情" :image-size="70" />
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
