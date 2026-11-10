<script setup lang="ts">
import { computed } from 'vue'
import type { TaskExecution } from '@/types/task-executions'
import TenantTag from '@/components/TenantTag.vue'
import { fmtTime } from '@/utils/format'
import { SERVICES } from '@/utils/dict'
import { executionCount, executionPercentage, executionSeconds, executionStatus, isActiveExecution, isFinalExecution, isRetryableExecution } from './composables/taskExecutionPresentation'

const props = withDefaults(defineProps<{
  execution: TaskExecution
  canCancel: boolean
  canRetry: boolean
  pendingAction?: 'cancel' | 'retry' | null
  showOpen?: boolean
}>(), { showOpen: true })
const emit = defineEmits<{ open: []; cancel: []; retry: [] }>()
const serviceName = computed(() => Object.prototype.hasOwnProperty.call(SERVICES, props.execution.service) ? SERVICES[props.execution.service] : props.execution.service)
const status = computed(() => executionStatus(props.execution.status))
const percentage = computed(() => executionPercentage(props.execution))
const active = computed(() => isActiveExecution(props.execution.status))
const final = computed(() => isFinalExecution(props.execution.status))
const progressText = computed(() => {
  const e = props.execution
  if (e.current === null && e.total === null) return '后端未上报进度数量'
  return `${executionCount(e.current)} / ${executionCount(e.total)}${e.unit ? ` ${e.unit}` : ''}`
})
</script>

<template>
  <article class="execution-card" :aria-label="`${execution.task_name} · ${execution.project_label}`">
    <header class="execution-header"><h3>{{ execution.task_name }}</h3><el-tag :type="status.type" size="small">{{ status.label }} · {{ execution.status }}</el-tag></header>
    <div class="execution-target"><span class="text-3">{{ serviceName }}</span><TenantTag :id="execution.tenant_id" :name="execution.tenant_name" /><span>{{ execution.project_label }}</span><span class="text-3">{{ execution.trigger_type === 'MANUAL' ? '手动' : '调度' }}</span></div>
    <div class="execution-phase"><b>{{ execution.phase_label || execution.phase }}</b><span v-if="final" class="text-3">已结束</span><span v-else class="text-3">{{ progressText }}</span></div>
    <el-progress v-if="percentage !== null" :percentage="percentage" :stroke-width="6" :show-text="false" :aria-label="`后端阶段进度 ${progressText}`" />
    <p v-if="execution.message" class="execution-message text-3">{{ execution.message }}</p>
    <dl class="execution-metrics"><div><dt>目标对象</dt><dd>{{ executionCount(execution.resource_total) }}</dd></div><div><dt>API 次数</dt><dd>{{ executionCount(execution.api_calls) }}</dd></div><div><dt>巡检耗时</dt><dd>{{ executionSeconds(execution.elapsed_sec) }}</dd></div></dl>
    <p class="execution-heartbeat text-3">心跳 {{ execution.heartbeat_at ? fmtTime(execution.heartbeat_at) : '尚未上报' }}<span v-if="execution.status === 'QUEUED'"> · 入队 {{ fmtTime(execution.queued_at) }}</span></p>
    <p v-if="execution.error_summary" class="execution-error" role="status">{{ execution.error_summary }}</p>
    <p v-if="execution.cancel_requested && active" class="execution-cancel text-3" role="status">已请求停止本地巡检，等待后台确认。</p>
    <footer class="execution-actions">
      <el-button v-if="showOpen !== false" size="small" @click="emit('open')">执行详情</el-button>
      <el-button v-if="canCancel && active" size="small" type="danger" plain :disabled="execution.cancel_requested || !!pendingAction" :loading="pendingAction === 'cancel'" @click="emit('cancel')">仅停止本地巡检</el-button>
      <el-button v-if="canRetry && isRetryableExecution(execution.status)" size="small" :disabled="!!pendingAction" :loading="pendingAction === 'retry'" @click="emit('retry')">新建重试</el-button>
    </footer>
  </article>
</template>

<style scoped lang="scss">
.execution-card { min-width: 0; border: 1px solid var(--hi-border); border-radius: var(--hi-radius-sm); background: var(--hi-bg-card, var(--el-bg-color)); padding: 16px; }
.execution-header { display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 8px; h3 { margin: 0; font-size: 14px; overflow-wrap: anywhere; } }
.execution-target { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 10px; font-size: 12px; }
.execution-phase { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin: 16px 0 8px; font-size: 12px; }
.execution-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 16px 0 10px; dt { color: var(--hi-text-3); font-size: 12px; } dd { margin: 4px 0 0; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; } }
.execution-message, .execution-heartbeat, .execution-error, .execution-cancel { margin: 8px 0 0; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.execution-error { color: var(--el-color-danger); }
.execution-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; .el-button { margin-left: 0; } }
</style>
