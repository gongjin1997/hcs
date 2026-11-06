<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { ArrowLeft, Download } from '@element-plus/icons-vue'
import { resourceAlarmsLocation, resourceLedgerLocation } from '@/config/resource-navigation'
import { useUserStore } from '@/stores/user'
import { fmtTime } from '@/utils/format'
import { rawStatusType, statusText } from '@/utils/dict'
import type { GaussComponent, GaussDisk } from '@/types/gaussdb'
import type { GaussPanelModel } from './useGaussPanel'
import {
  COMPONENT_STATUS_TEXT, FRESHNESS_TEXT, GAUSS_PANELS, componentHealth, componentStatusType, diskLevel, diskText, inspectionTime,
  isNativeTimeKey, nativeColumnLabel, nativeText, nativeTimeTitle, quantityText,
} from './gauss-utils'
import GaussDiskPanel from './GaussDiskPanel.vue'
import GaussDiskHistoryPanel from './GaussDiskHistoryPanel.vue'
import GaussNativeStatus from './GaussNativeStatus.vue'
import GaussPerformancePanel from './GaussPerformancePanel.vue'
import GaussQueryPanel from './GaussQueryPanel.vue'

const props = defineProps<{ model: GaussPanelModel; canRun: boolean }>()
defineEmits<{ back: []; panel: [value: string | number] }>()
const user = useUserStore()
const { selected, panel, overview, error, exportError, loading, loadingKind, exporting, canExport, detail, detailTitle, canExportSlowDetail } = props.model
const diskHistory = shallowRef<InstanceType<typeof GaussDiskHistoryPanel> | null>(null)
const sqlDetailVisible = computed({ get: () => panel.value === 'slow-sql' && (!!detail.value || loadingKind.value === 'detail'), set: value => { if (!value) props.model.closeDetail() } })
const target = computed(() => selected.value && { ...selected.value, resource_type: 'gaussdb_instance' })
const ledgerLink = computed(() => resourceLedgerLocation(target.value, user))
const alarmsLink = computed(() => resourceAlarmsLocation(target.value, user))
const health = computed(() => componentHealth(selected.value?.components))
const collectedAt = computed(() => selected.value?.disk_observed_at ?? selected.value?.components?.observed_at ?? null)
const collectedFreshness = computed(() => selected.value?.disk_observed_at ? selected.value.disk_freshness : selected.value?.components?.freshness)
// 概览先展示最近一次巡检采集的磁盘与组件（本地）；点击“查询实例数据”后换成实时结果
const localDisk = computed<GaussDisk | null>(() => {
  const instance = selected.value
  if (!instance?.disk_state) return null
  return { status: instance.disk_state, used_gb: instance.disk_used_gb ?? null, total_gb: instance.disk_total_gb ?? null,
    usage_percent: instance.disk_usage ?? null, observed_at: instance.disk_observed_at ?? null, collected_at: instance.disk_observed_at ?? null,
    time_basis: instance.disk_observed_at ? 'RESPONSE_RECEIVED' : null,
    warning: instance.disk_freshness === 'STALE' ? '最近一次巡检采集已过期，请检查 GaussDB 巡检任务'
      : instance.disk_state === 'UNAVAILABLE' ? '实例当前状态不查询磁盘用量' : instance.disk_state === 'ERROR' ? '最近一次磁盘用量查询失败' : null }
})
const components = computed<GaussComponent[]>(() => overview.value ? overview.value.components : selected.value?.component_items ?? [])
const componentKey = (row: GaussComponent) => `${row.node_id}/${row.component_id}`
const componentRowClass = ({ row }: { row: GaussComponent }) => (row.status === 'Down' ? 'component-down' : '')
/** 顶栏刷新：概览重读存储趋势、已实时查询过的再查一次；其他标签只重跑已经查过的结果。 */
async function refresh() {
  if (panel.value === 'overview') await Promise.all([props.canRun && overview.value ? props.model.queryOverview() : undefined, diskHistory.value?.refresh()])
  else if (props.canRun && props.model.applied.value) await props.model.queryPanel()
}
defineExpose({ refresh })
</script>

<template>
  <div v-if="selected" class="gauss-workspace">
    <div class="instance-nav"><el-button text :icon="ArrowLeft" @click="$emit('back')">实例列表</el-button><div class="table-actions"><router-link v-if="ledgerLink" :to="ledgerLink">查看台账</router-link><router-link v-if="alarmsLink" :to="alarmsLink">相关告警</router-link></div></div>
    <section class="hi-card workspace-head">
      <div class="identity-line">
        <h2>{{ selected.name }}</h2>
        <el-tag :type="rawStatusType(selected.status_raw)" effect="plain" :title="selected.status_raw || undefined">{{ statusText('gaussdb_instance', selected.status_raw) }}</el-tag>
        <span v-if="selected.status_since" class="text-3 status-since">自 {{ fmtTime(selected.status_since, 'MM-DD HH:mm') }}</span>
        <el-tag v-if="selected.scope_excluded" size="small" type="info" effect="plain" title="被采集范围规则排除，不再采集">范围外</el-tag>
      </div>
      <div class="identity-meta"><span>{{ selected.tenant_name }}</span><span>{{ selected.project_name || '未关联项目' }}</span><span>{{ selected.region || '区域未提供' }}</span><span v-if="selected.deploy_type">{{ selected.deploy_type }}</span><span v-if="selected.version">{{ selected.version }}</span><span class="mono instance-id">{{ selected.resource_id }}</span></div>
      <dl class="facts" aria-label="最近一次巡检">
        <div><dt>磁盘使用</dt><dd><el-tag size="small" :type="diskLevel(selected.disk_usage)" effect="plain">{{ diskText(selected) }}</el-tag><span v-if="selected.disk_total_gb != null" class="text-3">{{ quantityText(selected.disk_used_gb ?? null, 'GB') }} / {{ quantityText(selected.disk_total_gb, 'GB') }}</span></dd></div>
        <div><dt>组件</dt><dd><el-tag size="small" :type="health.type" effect="plain">{{ health.text }}</el-tag></dd></div>
        <div><dt>活动告警</dt><dd><strong>{{ selected.active_alarms ?? 0 }}</strong><router-link v-if="alarmsLink && selected.active_alarms" :to="alarmsLink" class="fact-link">查看</router-link></dd></div>
        <div><dt>最近采集</dt><dd><span>{{ collectedAt ? fmtTime(collectedAt, 'MM-DD HH:mm:ss') : '—' }}</span><span v-if="collectedAt && collectedFreshness" class="text-3">{{ FRESHNESS_TEXT[collectedFreshness] }}</span></dd></div>
      </dl>
      <el-tabs :model-value="panel" class="workspace-tabs" @update:model-value="value => $emit('panel', value)"><el-tab-pane v-for="item in GAUSS_PANELS" :key="item.key" :name="item.key" :label="item.label" /></el-tabs>
    </section>
    <section class="hi-card workspace-content">
      <div v-if="!canRun" class="read-only-note" role="status">当前账号可查看实例和已采集的磁盘历史。在线查询需管理员授予执行巡检权限。</div>
      <div v-if="error || exportError" class="query-error"><el-alert :title="error || exportError" type="error" :closable="false" show-icon /></div>
      <div v-if="loading" class="request-progress" role="status"><span>{{ loadingKind === 'detail' ? '正在读取明细' : '正在查询' }}</span><el-button link @click="model.cancelQuery">取消查询</el-button></div>
      <template v-if="panel === 'overview'">
        <header class="table-heading">
          <h3>实例概况</h3>
          <div class="table-actions"><el-button v-if="canRun && !overview && !loading" @click="model.queryOverview">查询实例数据</el-button><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="model.exportExcel">导出 Excel</el-button><el-button v-if="exporting" @click="model.cancelExport">取消导出</el-button></div>
        </header>
        <p class="source-note text-3">{{ overview ? `磁盘与组件为实时查询结果（${fmtTime(overview.queried_at, 'MM-DD HH:mm:ss')}）` : '磁盘与组件为最近一次巡检采集的结果；“查询实例数据”会向 GaussDB 实时查询。' }}</p>
        <dl class="configuration">
          <div><dt>配置 CPU</dt><dd>{{ quantityText(selected.vcpus, 'vCPU') }}</dd></div>
          <div><dt>配置内存</dt><dd>{{ quantityText(selected.memory_size, selected.memory_unit) }}</dd></div>
          <div><dt>配置存储</dt><dd>{{ quantityText(selected.storage_size, selected.storage_unit) }}</dd></div>
          <div><dt>台账更新</dt><dd class="snapshot-time">{{ fmtTime(selected.snapshot_at, 'MM-DD HH:mm:ss') }}</dd></div>
        </dl>
        <h3 class="section-title">存储空间</h3>
        <el-skeleton v-if="loadingKind === 'overview'" :rows="3" animated />
        <GaussDiskPanel v-else-if="overview" :overview="overview" />
        <GaussDiskPanel v-else-if="localDisk" :disk="localDisk" />
        <el-empty v-else description="暂无存储采集，等待下一轮 GaussDB 巡检" :image-size="48" />
        <div class="node-heading"><h3>节点与组件 <span class="text-3">{{ components.length }}</span></h3><el-button v-if="canRun" link type="primary" @click="$emit('panel', 'sessions')">查看会话</el-button></div>
        <el-skeleton v-if="loadingKind === 'overview'" :rows="4" animated />
        <template v-else-if="overview || components.length">
          <GaussNativeStatus v-if="overview" :status="overview.components_status" :warning="overview.components_warning" :queried-at="overview.queried_at" />
          <p v-else class="text-3 component-source">最近一次巡检采集 {{ fmtTime(selected.components?.observed_at, 'MM-DD HH:mm:ss') }} · {{ health.text }}</p>
          <el-table :data="components" :row-key="componentKey" :row-class-name="componentRowClass">
            <el-table-column label="节点" min-width="180"><template #default="{ row }">{{ row.node_name || row.node_id }}<div v-if="row.node_name" class="text-3 component-id">{{ row.node_id }}</div></template></el-table-column>
            <el-table-column prop="component_type" label="组件类型" width="100" />
            <el-table-column prop="component_id" label="组件 ID" min-width="180" />
            <el-table-column label="角色" width="110"><template #default="{ row }">{{ row.role || '—' }}</template></el-table-column>
            <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag size="small" :type="componentStatusType(row.status)" effect="plain" :title="row.status || undefined">{{ COMPONENT_STATUS_TEXT[row.status ?? ''] ?? row.status ?? '—' }}</el-tag></template></el-table-column>
            <el-table-column label="可用区 / 分组" min-width="160"><template #default="{ row }">{{ row.availability_zone_id || '—' }}<div v-if="row.distributed_id" class="text-3 component-id">{{ row.distributed_id }}</div></template></el-table-column>
            <template #empty><el-empty description="未返回节点组件" :image-size="48" /></template>
          </el-table>
        </template>
        <el-empty v-else description="暂无在线节点数据" :image-size="48" />
        <GaussDiskHistoryPanel ref="diskHistory" :instance="selected" />
      </template>
      <GaussPerformancePanel v-else-if="panel === 'performance'" :model="model" :can-run="canRun" />
      <GaussQueryPanel v-else :model="model" :can-run="canRun" />
    </section>
    <el-drawer v-model="sqlDetailVisible" :title="detailTitle || 'SQL 明细'" size="min(860px, 100vw)" destroy-on-close>
      <div class="table-heading"><h3>执行明细</h3><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExportSlowDetail" :loading="exporting" @click="model.exportSlowDetail">导出明细 Excel</el-button></div>
      <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" />
      <el-skeleton v-if="loadingKind === 'detail'" :rows="7" animated />
      <template v-else-if="detail"><GaussNativeStatus :status="detail.status" :warning="detail.warning" :queried-at="detail.queried_at" /><el-empty v-if="!detail.items.length" description="该 SQL 暂无执行明细" :image-size="48" /><dl v-for="(row, index) in detail.items" :key="index" class="sql-detail"><template v-for="(value, key) in row" :key="key"><dt>{{ nativeColumnLabel(String(key), detail.units, [value]) }}</dt><dd :title="isNativeTimeKey(String(key)) ? nativeTimeTitle(value) : undefined">{{ isNativeTimeKey(String(key)) ? inspectionTime(value) : nativeText(value) }}</dd></template></dl></template>
    </el-drawer>
  </div>
</template>

<style scoped>
.gauss-workspace { display: flex; flex-direction: column; gap: 16px; min-width: 0; }.instance-nav { display: flex; align-items: center; justify-content: space-between; }.instance-nav > .el-button:first-child { margin-left: -12px; }
.workspace-head { padding-bottom: 0; }.identity-line { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }h2 { font-size: 21px; margin: 0; overflow-wrap: anywhere; }h3 { font-size: 15px; margin: 0; }
.status-since { font-size: 12px; }
.identity-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 12px; color: var(--hi-text-2); font-size: 12px; }.instance-id { color: var(--hi-text-3); overflow-wrap: anywhere; }
.facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 18px 0 0; padding: 14px 0; border-top: 1px solid var(--hi-border-light); }
.facts dt { color: var(--hi-text-3); font-size: 12px; }.facts dd { margin: 8px 0 0; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 13px; }
.facts strong { font-size: 18px; }.fact-link { font-size: 12px; }
.workspace-tabs { margin-top: 6px; }.workspace-tabs :deep(.el-tabs__header) { margin: 0; }.workspace-tabs :deep(.el-tabs__nav-wrap::after) { display: none; }.workspace-tabs :deep(.el-tabs__item) { height: 44px; }
.workspace-content { min-width: 0; }.table-heading { min-height: 32px; margin-bottom: 8px; }.source-note { font-size: 12px; margin: 0 0 18px; }
.configuration { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 24px; margin: 0 0 24px; padding: 8px 0 20px; border-bottom: 1px solid var(--hi-border); }.configuration dt { color: var(--hi-text-3); font-size: 12px; }.configuration dd { margin: 8px 0 0; font-size: 18px; font-weight: 600; overflow-wrap: anywhere; }.configuration .snapshot-time { font-size: 14px; padding-top: 4px; }
.section-title { margin-bottom: 10px; }
.node-heading { display: flex; align-items: center; justify-content: space-between; margin: 24px 0 12px; }.node-heading h3 span { margin-left: 8px; font-weight: 400; }.component-id { font-size: 11px; margin-top: 4px; }
.component-source { font-size: 12px; margin: 0 0 10px; }
.workspace-content :deep(.component-down td) { background: var(--el-color-danger-light-9) !important; }
.read-only-note { font-size: 13px; color: var(--hi-text-2); padding: 10px 12px; background: var(--hi-bg-soft); margin-bottom: 20px; }.request-progress { display: flex; align-items: center; gap: 14px; font-size: 13px; color: var(--hi-text-3); margin-bottom: 14px; }.query-error { margin-bottom: 14px; }
.sql-detail { display: grid; grid-template-columns: 165px minmax(0, 1fr); gap: 14px 18px; font-size: 13px; }.sql-detail + .sql-detail { padding-top: 20px; border-top: 1px solid var(--hi-border); }.sql-detail dt { color: var(--hi-text-3); }.sql-detail dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--hi-font-mono); }
@media (max-width: 900px) { .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { .configuration { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }.configuration dd { font-size: 16px; }.identity-meta { gap: 8px 12px; }.instance-id { flex-basis: 100%; }.sql-detail { grid-template-columns: minmax(0, 1fr); gap: 5px; }.sql-detail dd { margin-bottom: 12px; } }
</style>
