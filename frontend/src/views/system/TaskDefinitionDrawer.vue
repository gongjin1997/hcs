<script setup lang="ts">
import type { InspectionTask } from '@/api'

defineProps<{
  modelValue: boolean
  task: InspectionTask | null
  serviceName: string
  cadence: string
  scopeLabel: string
  checks: string[]
  steps: string[]
  canEdit: boolean
  canConfigureCce?: boolean
  timeoutLabel?: string
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  configure: []
  scope: []
}>()
</script>

<template>
  <el-drawer :model-value="modelValue" :title="task?.name ?? '任务详情'" size="min(720px, 100vw)" append-to-body destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div v-if="task" class="task-definition">
      <div class="definition-heading"><el-tag effect="plain">{{ serviceName }}</el-tag><span class="mono text-3">{{ task.code }}</span><el-tag :type="task.enabled ? 'success' : 'info'">{{ task.enabled ? '已启用' : '已停用' }}</el-tag></div>
      <section class="definition-section" aria-label="任务配置">
        <h3 class="section-title">配置与范围</h3>
        <dl class="definition-facts"><div><dt>执行周期</dt><dd>{{ cadence }}</dd></div><div><dt>租户 / 项目</dt><dd>{{ scopeLabel }}</dd></div><div v-if="timeoutLabel"><dt>整轮超时</dt><dd>{{ timeoutLabel }}</dd></div></dl>
        <div class="definition-actions"><el-button v-if="canEdit" size="small" @click="emit('configure')">执行参数 / 周期</el-button><el-button v-if="task.types.length" size="small" @click="emit('scope')">查看 / 配置资源范围</el-button></div>
      </section>
      <section class="definition-section" aria-label="任务定义">
        <h3 class="section-title">检查项</h3>
        <div class="check-tags"><el-tag v-for="check in checks" :key="check" size="small" type="info">{{ check }}</el-tag><span v-if="!checks.length" class="text-3">后端未提供检查项</span></div>
        <details class="definition-steps"><summary>执行步骤说明<span v-if="steps.length">（{{ steps.length }} 步）</span></summary><ol v-if="steps.length"><li v-for="(step, index) in steps" :key="index">{{ step }}</li></ol><p v-else class="text-3">后端未提供步骤说明</p></details>
        <p v-if="['ecs_status', 'ecs_metrics', 'ecs_quota'].includes(task.code)" class="definition-note text-3">租户 ECS 策略中的频率、范围与指标优先于全局任务周期。</p>
        <p v-if="task.note" class="definition-note text-3">{{ task.note }}</p>
        <p v-if="task.code === 'cce_cluster_health' && canConfigureCce" class="definition-note"><router-link :to="{ path: '/pod-events', query: { tab: 'clusters' } }">容器事件 · 集群与节点采集配置</router-link></p>
      </section>
      <section class="definition-section" aria-label="历史执行"><h3 class="section-title">历史执行</h3><slot name="history" /></section>
    </div>
  </el-drawer>
</template>

<style scoped lang="scss">
.task-definition { min-width: 0; overflow-wrap: anywhere; }
.definition-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.definition-section { margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--hi-border); }
.section-title { margin: 0 0 12px; font-size: 14px; }
.definition-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 0 0 14px; dt { color: var(--hi-text-3); font-size: 12px; } dd { margin: 4px 0 0; font-size: 13px; } }
.definition-actions, .check-tags { display: flex; flex-wrap: wrap; gap: 8px; .el-button { margin-left: 0; } }
.definition-steps { margin-top: 14px; font-size: 13px; line-height: 1.8; summary { color: var(--el-color-primary); cursor: pointer; } ol { padding-left: 20px; } }
.definition-note { font-size: 12px; line-height: 1.7; }
@media (max-width: 600px) { .definition-facts { grid-template-columns: 1fr; } }
</style>
