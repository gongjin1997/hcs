<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getEcsPolicy, listEcsInstances, saveEcsPolicy } from '@/api/ecs'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { ECS_PHASES, ECS_POLICY_METRICS, ecsPhaseReason } from './ecs-policy'
import { intervalText } from './ecs-utils'
import { useEcsPolicy } from './useEcsPolicy'
import ManageOneConfigDialog from './ManageOneConfigDialog.vue'
import type { EcsPolicyMetricKey } from '@/types/ecs'

const props = defineProps<{ modelValue: boolean; tenantId: number | null; tenantName?: string }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; saved: [] }>()
const app = useAppStore(), user = useUserStore()
const selectedTenant = shallowRef<number | null>(props.tenantId)
const scopedTenant = computed(() => props.tenantId ?? selectedTenant.value)
const editable = computed(() => user.can('collect:manage') && scopedTenant.value != null && app.tenants.some(tenant => tenant.id === scopedTenant.value))
watch(() => [props.modelValue, props.tenantId] as const, () => { selectedTenant.value = props.tenantId }, { flush: 'sync' })
const { policy, loaded, prometheusJson, error, validationError, optionsError, instances, instancesTotal, instanceOptions,
  loading, saving, instancesLoading, canSave, load, save, loadInstances, searchInstances } = useEcsPolicy({
  tenantId: () => scopedTenant.value, visible: () => props.modelValue, canManage: () => editable.value,
  fetcher: getEcsPolicy, saver: saveEcsPolicy, instancesFetcher: listEcsInstances,
})
const manageOneVisible = shallowRef(false)
const canReadManageOne = computed(() => user.canAny('ledger:view', 'task:view', 'tenant:view') && scopedTenant.value != null
  && (user.user?.tenant_ids == null || user.user.tenant_ids.includes(scopedTenant.value)))
watch(() => [loaded.value?.tenant_id, policy.selection.mode] as const, ([tenant, mode]) => {
  if (tenant && mode === 'selected') void loadInstances()
})
async function onSave() {
  if (await save()) { ElMessage.success('采集策略已保存'); emit('saved') }
}
// JSON 与可视字段共用同一份草稿；修改单位不重建/丢弃其他映射。
const mappingDraft = computed(() => {
  try {
    const value: unknown = JSON.parse(prometheusJson.value || 'null')
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
  } catch { return null }
})
type MappingField = 'queries' | 'units' | 'timestamp_queries'
function mappingValue(field: MappingField, key: EcsPolicyMetricKey): string {
  const values = mappingDraft.value?.[field]
  const value = values && typeof values === 'object' && !Array.isArray(values) ? (values as Record<string, unknown>)[key] : undefined
  return typeof value === 'string' ? value : ''
}
function updateMapping(field: MappingField, key: EcsPolicyMetricKey, value: string) {
  if (!mappingDraft.value) return
  const old = mappingDraft.value[field]
  const values = old && typeof old === 'object' && !Array.isArray(old) ? { ...old } as Record<string, unknown> : {}
  if (value) values[key] = value
  else delete values[key]
  prometheusJson.value = JSON.stringify({ ...mappingDraft.value, [field]: values }, null, 2)
}
function configureMapping() { prometheusJson.value = JSON.stringify({ queries: {}, instance_label: 'instance' }, null, 2) }
</script>

<template>
  <el-dialog :model-value="modelValue" :title="`${tenantName || '租户'} · ECS 采集策略`" width="min(860px, calc(100vw - 24px))" top="5vh" append-to-body
    :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @update:model-value="emit('update:modelValue', $event)">
    <div class="ecs-policy" v-loading="loading">
      <el-form-item v-if="tenantId == null" label="租户"><el-select v-model="selectedTenant" :disabled="saving" placeholder="请选择租户" aria-label="策略租户"><el-option v-for="tenant in app.tenants" :key="tenant.id" :value="tenant.id" :label="tenant.name" /></el-select></el-form-item>
      <div v-if="scopedTenant" class="table-heading">
        <b>采集阶段 <el-tag v-if="!editable" type="info" size="small">只读</el-tag></b>
        <div class="table-actions"><router-link v-if="user.can('task:view')" :to="{ path: '/system', query: { tab: 'tasks' } }" @click="emit('update:modelValue', false)">全局任务</router-link></div>
      </div>
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      <el-alert v-if="validationError" :title="validationError" type="warning" show-icon :closable="false" />
      <template v-if="loaded">
        <el-alert :type="loaded.master_enabled ? 'info' : 'warning'" :closable="false" show-icon
          :title="loaded.master_enabled ? 'ECS 采集总开关已开启' : 'ECS 采集总开关已关闭，各阶段当前不执行'">
          <router-link v-if="user.canAny('task:view', 'collect:manage')" :to="{ path: '/system', query: { tab: 'switches' } }" @click="emit('update:modelValue', false)">查看采集总开关</router-link>
        </el-alert>
        <el-form :disabled="!editable || loading || saving" label-width="130px" @submit.prevent="onSave">
          <section class="scope-section" aria-labelledby="ecs-scope-title">
            <div class="scope-heading">
              <div><h3 id="ecs-scope-title">实例范围</h3><p class="text-3">先确定本租户哪些 ECS 参与指标采集和巡检。</p></div>
              <el-tag v-if="policy.selection.mode === 'selected'" type="primary" effect="plain">指定 {{ policy.selection.resource_ids.length }} 台</el-tag>
              <el-tag v-else type="info" effect="plain">{{ policy.selection.mode === 'critical' ? '仅核心实例' : '全部实例' }}</el-tag>
            </div>
            <el-form-item label="监控实例">
              <el-radio-group v-model="policy.selection.mode" aria-label="实例范围">
                <el-radio-button value="all">全部实例</el-radio-button>
                <el-radio-button value="critical">仅核心实例</el-radio-button>
                <el-radio-button value="selected">指定实例</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="policy.selection.mode === 'selected'" label="指定实例">
              <el-select v-model="policy.selection.resource_ids" multiple filterable remote reserve-keyword :remote-method="searchInstances" :loading="instancesLoading"
                collapse-tags collapse-tags-tooltip :max-collapse-tags="3" placeholder="搜索名称、IP 或原始资源 ID" aria-label="指定 ECS 实例">
                <el-option v-for="item in instanceOptions" :key="item.id" :value="item.id" :label="item.label" />
                <template #footer><div class="select-footer"><span class="text-3">已选 {{ policy.selection.resource_ids.length }} 台</span><el-button v-if="instances.length < instancesTotal" link :loading="instancesLoading" @click="loadInstances(true)">加载更多</el-button></div></template>
              </el-select>
              <el-alert v-if="!policy.selection.resource_ids.length" title="未选择实例：保存后指标采集和巡检均不处理任何 ECS。" type="warning" :closable="false" />
              <div v-if="optionsError" class="text-danger">{{ optionsError }} <el-button link @click="loadInstances()">重试</el-button></div>
            </el-form-item>
            <div class="scope-note text-3">实例范围只控制指标采集和巡检。台账同步、项目配额保持独立；标记为“未纳入巡检”的实例仍会排除。需要按业务、环境或负责人筛选时，到“系统管理 → 采集开关”设置采集范围，两者取交集。</div>
          </section>
          <p class="text-3 phase-caption">执行状态按已保存策略显示。</p>
          <div class="phase-grid">
            <div v-for="phase in ECS_PHASES" :key="phase.key" class="phase-row">
              <label :for="`ecs-phase-${phase.key}`">{{ phase.label }}</label>
              <el-switch :id="`ecs-phase-${phase.key}`" v-model="policy[phase.key].enabled" :aria-label="`${phase.label}开关`" />
              <el-input-number v-model="policy[phase.key].interval_sec" :min="60" :max="2592000" :precision="0" :step="60" controls-position="right" :aria-label="`${phase.label}间隔秒`" />
              <span class="text-3">秒 · {{ intervalText(policy[phase.key].interval_sec) }}</span>
              <el-tag size="small" :type="loaded.phase_status[phase.key]?.effective_enabled ? 'success' : 'info'">{{ loaded.phase_status[phase.key]?.effective_enabled ? '生效' : '未执行' }}</el-tag>
              <span v-if="!loaded.phase_status[phase.key]?.effective_enabled" class="text-3 phase-reason">{{ ecsPhaseReason(loaded.phase_status[phase.key]) }}</span>
              <span v-if="loaded.phase_status[phase.key]?.cron" class="text-3 phase-schedule">当前沿用全局定时：<code>{{ loaded.phase_status[phase.key]?.cron }}</code><span>；保存后按本租户周期执行</span></span>
            </div>
          </div>
          <el-alert v-if="editable && loaded.policy.inspection.enabled && !policy.inspection.enabled" title="保存后将关闭该租户的 ECS 巡检及活动告警，已有采样保留。" type="warning" :closable="false" />
          <el-form-item label="采集指标"><el-checkbox-group v-model="policy.metric_keys"><el-checkbox v-for="metric in ECS_POLICY_METRICS" :key="metric.key" :value="metric.key">{{ metric.label }}</el-checkbox></el-checkbox-group></el-form-item>
          <el-form-item label="结果过期"><el-input-number v-model="policy.stale_after_sec" :min="60" :max="2592000" :precision="0" :step="60" controls-position="right" aria-label="样本过期秒" /><span class="text-3 inline-hint">秒；按返回时间判断，PromQL 默认是求值时间</span></el-form-item>
        </el-form>
        <el-divider content-position="left">指标来源映射</el-divider>
          <div class="table-actions manageone-entry"><span class="text-3">ManageOne 原生使用率</span><el-button v-if="canReadManageOne" size="small" @click="manageOneVisible = true">配置 ManageOne</el-button></div>
        <el-form :disabled="!editable || loading || saving" label-width="130px" @submit.prevent="onSave">
          <el-alert v-if="!mappingDraft" title="未填写自定义查询时使用内置 Prometheus 查询；未配置数据源时显示“无数据”。" type="info" :closable="false" />
          <div class="mapping-help text-3">AOM 已停用，旧映射不再使用；保存策略时清除旧映射。</div>
          <el-button v-if="prometheusJson.trim() === 'null'" @click="configureMapping">配置 Prometheus 映射</el-button>
          <el-form-item label="Prometheus JSON"><el-input v-model="prometheusJson" type="textarea" :rows="6" spellcheck="false" aria-label="Prometheus 映射 JSON" placeholder="queries、units、timestamp_queries、label_filters、instance_label；未配置用 null" /></el-form-item>
          <div class="mapping-help text-3">label_filters 是标签名到精确值的映射，不自动推断租户/项目。服务地址在租户接入中配置。</div>
          <template v-if="mappingDraft">
            <div v-for="item in ECS_POLICY_METRICS" :key="item.key" class="mapping-metric">
              <el-form-item :label="`${item.label}查询`"><el-input :model-value="mappingValue('queries', item.key)" :aria-label="`${item.label}查询`" placeholder="留空使用内置查询" @update:model-value="updateMapping('queries', item.key, $event)" /></el-form-item>
              <el-form-item :label="`${item.label}单位`"><el-select :model-value="mappingValue('units', item.key)" :aria-label="`${item.label}单位`" clearable placeholder="自定义查询必须选择单位" @update:model-value="updateMapping('units', item.key, $event)"><el-option :value="item.key === 'boot_time' ? 's' : '%'" :label="item.key === 'boot_time' ? '秒（s）' : '百分比（%）'" /></el-select></el-form-item>
              <el-form-item :label="`${item.label}源时间`"><el-input :model-value="mappingValue('timestamp_queries', item.key)" :aria-label="`${item.label}源时间查询`" placeholder="可选：返回源时间秒值，标签须与结果完全匹配" @update:model-value="updateMapping('timestamp_queries', item.key, $event)" /></el-form-item>
            </div>
          </template>
          <el-alert title="未配置源时间查询时，时间仅表示 PromQL 求值时间，不能证明物理采样新鲜度。" type="warning" :closable="false" />
          <div class="mapping-help text-3">源时间表达式须取原始序列时间；对聚合结果套 timestamp 仍可能只是求值时间。源时间缺失或失败时不回退；使用率单位为 %，开机时间为 s，不猜测或自动换算。</div>
        </el-form>
        <router-link v-if="user.can('tenant:view')" :to="{ path: '/system', query: { tab: 'tenants', tenant_id: scopedTenant }, hash: `#tenant-${scopedTenant}` }" @click="emit('update:modelValue', false)">配置租户服务地址</router-link>
      </template>
      <el-empty v-else-if="!loading && !error" :description="scopedTenant ? '尚未读取策略' : '请选择租户'" :image-size="60" />
    </div>
    <template #footer><div class="table-actions policy-footer"><el-button :disabled="saving" @click="emit('update:modelValue', false)">关闭</el-button><el-button v-if="scopedTenant" :disabled="saving" :loading="loading" @click="load">重新读取</el-button><el-button v-if="editable" type="primary" :loading="saving" :disabled="!canSave" @click="onSave">保存策略</el-button></div></template>
  </el-dialog>
  <ManageOneConfigDialog v-model="manageOneVisible" :tenant-id="scopedTenant" :tenant-name="tenantName || (scopedTenant ? app.tenants.find(tenant => tenant.id === scopedTenant)?.name : undefined)" @saved="emit('saved')" />
</template>

<style scoped lang="scss">
.ecs-policy { display: flex; flex-direction: column; gap: 14px; max-height: calc(85vh - 180px); max-height: calc(85dvh - 180px); overflow-y: auto; padding-right: 4px; }
.ecs-policy > :deep(.el-alert) { flex-shrink: 0; }
.policy-footer { justify-content: flex-end; }
.table-heading, .select-footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; align-items: center; }
.scope-section { border: 1px solid var(--hi-border-light); border-left: 3px solid var(--el-color-primary); padding: 14px 14px 2px; margin-bottom: 10px; }
.scope-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.scope-heading h3 { margin: 0; font-size: 15px; font-weight: 600; }
.scope-heading p { margin: 4px 0 0; }
.phase-grid { display: flex; flex-direction: column; gap: 12px; margin: 16px 0 24px; }
.phase-row { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.phase-row label { width: 90px; flex: none; font-weight: 500; }
.phase-row .el-input-number { width: 150px; }
.phase-reason, .phase-schedule { flex-basis: 100%; padding-left: 102px; box-sizing: border-box; line-height: 1.6; }
.phase-schedule code { font-size: inherit; overflow-wrap: anywhere; }
.phase-caption { margin: 12px 0 0; }
.phase-row .text-3, .phase-caption, .scope-note, .mapping-help { font-size: 12px; }
.mapping-help { line-height: 1.7; margin: -4px 0 16px 130px; overflow-wrap: anywhere; }
.scope-note { line-height: 1.7; margin-bottom: 14px; }
.scope-section :deep(.el-alert) { margin-bottom: 10px; }
.mapping-metric { border-top: 1px solid var(--hi-border-light); padding-top: 16px; }
.manageone-entry { justify-content: flex-start; align-items: center; gap: 10px; margin: 0 0 12px 130px; }
.inline-hint { margin-left: 10px; }
@media (max-width: 650px) {
  .mapping-help { margin-left: 0; }
  .phase-row { gap: 8px; }
  .phase-row label { width: 100%; }
  .phase-reason, .phase-schedule { padding-left: 0; }
  .ecs-policy :deep(.el-form-item) { flex-direction: column; align-items: stretch; }
  .ecs-policy :deep(.el-form-item__label) { width: auto !important; justify-content: flex-start; }
  .ecs-policy :deep(.el-form-item__content) { margin-left: 0 !important; min-width: 0; }
  .inline-hint { margin-left: 0; }
  .manageone-entry { margin-left: 0; }
  .scope-section { padding-right: 10px; padding-left: 10px; }
}
</style>
