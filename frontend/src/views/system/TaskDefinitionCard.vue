<script setup lang="ts">
import type { InspectionTask } from '@/api'

const props = defineProps<{
  task: InspectionTask
  serviceName: string
  cadence: string
  scopeLabel: string
  resourceLabel: string
  nextRunLabel: string
  timeoutLabel?: string
  checks: string[]
  canEdit: boolean
  canRun: boolean
  submitting: boolean
  toggling: boolean
  active: boolean
  hasEcsPolicy: boolean
  hasJobSelection: boolean
}>()
const emit = defineEmits<{
  detail: []
  run: []
  configure: []
  scope: []
  ecs: []
  jobs: []
  selectTargets: []
  toggle: [enabled: boolean]
}>()

function toggle(value: string | number | boolean) {
  if (props.canEdit && !props.toggling) emit('toggle', Boolean(value))
}
</script>

<template>
  <article class="task-card" :aria-label="task.name">
    <header class="task-card-header">
      <div class="task-card-title">
        <el-tag size="small" effect="plain">{{ serviceName }}</el-tag>
        <h3>{{ task.name }}</h3>
        <span class="task-code mono text-3">{{ task.code }}</span>
      </div>
      <el-switch :model-value="task.enabled" :disabled="!canEdit" :loading="toggling" :aria-label="`${task.name}启用`" @change="toggle" />
    </header>
    <dl class="task-facts">
      <div><dt>周期</dt><dd>{{ cadence }}</dd></div>
      <div><dt>下次执行</dt><dd>{{ task.enabled ? nextRunLabel : '已停用' }}<el-tag v-if="task.enabled && task.next_run && task.next_run_source === 'estimate'" size="small" type="warning" effect="plain">预计</el-tag></dd></div>
      <div><dt>租户 / 项目</dt><dd>{{ scopeLabel }}</dd></div>
      <div><dt>资源范围</dt><dd>{{ resourceLabel }}</dd></div>
      <div v-if="timeoutLabel"><dt>整轮超时</dt><dd>{{ timeoutLabel }}</dd></div>
    </dl>
    <p class="task-checks"><span class="text-3">检查：</span>{{ checks.join('、') || '后端未提供检查项' }}</p>
    <div class="task-latest"><slot name="latest" /></div>
    <footer class="task-card-actions">
      <el-button v-if="canRun" type="primary" size="small" :loading="submitting" :disabled="!task.enabled || active" @click="emit('run')">{{ active ? '已有活跃执行' : '立即执行' }}</el-button>
      <el-button v-if="canRun" size="small" :disabled="!task.enabled || submitting" @click="emit('selectTargets')">按项目执行</el-button>
      <el-button size="small" @click="emit('detail')">任务详情</el-button>
      <el-button v-if="canEdit" size="small" @click="emit('configure')">{{ task.service === 'mrs' || task.code.includes('mrs') ? '执行参数' : '周期配置' }}</el-button>
      <el-button v-if="task.types.length" size="small" @click="emit('scope')">资源范围</el-button>
      <el-button v-if="hasEcsPolicy" size="small" @click="emit('ecs')">监控配置</el-button>
      <el-button v-if="hasJobSelection" size="small" @click="emit('jobs')">监控作业</el-button>
    </footer>
  </article>
</template>

<style scoped lang="scss">
.task-card { min-width: 0; padding: 18px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius-sm); background: var(--hi-bg-card, var(--el-bg-color)); }
.task-card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.task-card-title { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; h3 { margin: 0; font-size: 15px; overflow-wrap: anywhere; } }
.task-code { width: 100%; font-size: 11px; overflow-wrap: anywhere; }
.task-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 20px; margin: 18px 0 12px; div { min-width: 0; } dt { color: var(--hi-text-3); font-size: 12px; } dd { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 0; font-size: 13px; overflow-wrap: anywhere; } }
.task-checks { margin: 0; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.task-latest { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--hi-border); font-size: 12px; }
.task-card-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; .el-button { margin-left: 0; } }
@media (max-width: 600px) { .task-card { padding: 14px; } .task-facts { gap: 12px; } }
</style>
