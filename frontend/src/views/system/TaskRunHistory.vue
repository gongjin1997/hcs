<script setup lang="ts">
import type { InspectionTask, TaskRun } from '@/api'
import type { TaskExecutionRun } from '@/types/task-executions'
import TenantTag from '@/components/TenantTag.vue'
import { fmtTime } from '@/utils/format'

defineProps<{
  task: InspectionTask
  describeRun: (code: string, run: TaskRun) => string
  status: (code: string) => { label: string; type?: 'success' | 'warning' | 'danger' | 'info' | 'primary' }
  projectLabel: (run: TaskRun) => string
}>()

const emit = defineEmits<{ openExecution: [id: string] }>()
const executionId = (run: TaskExecutionRun) => typeof run.execution_id === 'string' ? run.execution_id : null

function elapsed(run: TaskRun) {
  if (!run.started_at) return run.finished_at ? '未启动' : '尚未开始'
  if (!run.finished_at) return '尚未结束'
  const ms = Date.parse(run.finished_at) - Date.parse(run.started_at)
  return Number.isFinite(ms) && ms >= 0 ? `${Math.round(ms / 100) / 10} s` : '—'
}
</script>

<template>
  <div class="run-history">
    <p class="history-note text-3">展示 {{ task.runs.length }} 次历史运行，每个租户 / 项目最多 12 次。</p>
    <p v-if="task.code.startsWith('gaussdb_')" class="history-note text-3">累计详情耗时是各请求耗时之和，不是墙钟耗时；缺失统计显示“—”。</p>
    <el-empty v-if="!task.runs.length" description="暂无历史执行" :image-size="60" />
    <article v-for="run in task.runs" :key="run.id" class="history-run">
      <div class="run-heading"><TenantTag :id="run.tenant_id" :name="run.tenant_name" /><span>{{ projectLabel(run) }}</span><el-tag size="small" :type="status(run.status).type ?? 'info'">{{ status(run.status).label }}</el-tag></div>
      <p class="run-time text-3">{{ fmtTime(run.started_at) }} · {{ run.trigger_type === 'MANUAL' ? '手动' : '调度' }} · {{ elapsed(run) }}</p>
      <dl class="run-counters"><div><dt>{{ task.code === 'data_cleanup' ? '清理条数' : '目标对象' }}</dt><dd>{{ run.resource_total }}</dd></div><div><dt>{{ task.code === 'ledger_sync' ? '变更数' : '异常数' }}</dt><dd>{{ run.abnormal_total }}</dd></div><div><dt>API 次数</dt><dd>{{ run.api_calls }}</dd></div></dl>
      <p v-if="describeRun(task.code, run)" class="run-statistics">{{ describeRun(task.code, run) }}</p>
      <el-button v-if="executionId(run)" link type="primary" @click="emit('openExecution', executionId(run)!)">打开单次执行</el-button>
      <p v-if="run.error_summary" class="run-error" role="status">{{ run.error_summary }}</p>
    </article>
  </div>
</template>

<style scoped lang="scss">
.history-note { margin: 0 0 10px; font-size: 12px; line-height: 1.7; }
.history-run { padding: 14px 0; border-bottom: 1px solid var(--hi-border); }
.run-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.run-time, .run-statistics, .run-error { margin: 8px 0 0; font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.run-counters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 12px 0 0; dt { font-size: 12px; color: var(--hi-text-3); } dd { margin: 4px 0 0; font-weight: 600; } }
.run-error { color: var(--el-color-danger); }
</style>
