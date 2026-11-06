<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { exportEcsHistory, getEcsHistory } from '@/api/ecs'
import EChart from '@/components/EChart.vue'
import { useUserStore } from '@/stores/user'
import { resourceAlarmsLocation, resourceLedgerLocation, resourceViewerScopeKey } from '@/config/resource-navigation'
import { fmtTime } from '@/utils/format'
import { METRICS_SOURCE, metricsSourceLabel, rawStatusType, statusText } from '@/utils/dict'
import type { EcsInstance, EcsMetricKey } from '@/types/ecs'
import { ECS_METRICS, ECS_PRESETS, ECS_STATE_LABEL, ecsLatestReadings, ecsTimestampLabel, intervalText, percentText } from './ecs-utils'
import { ecsTrendChart } from './ecs-chart'
import { useEcsTrend } from './useEcsTrend'

const props = defineProps<{ modelValue: boolean; instanceId: number | null; tenantId: number | null; instance?: EcsInstance | null }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; policy: [tenantId: number] }>()
const user = useUserStore()
const metric = shallowRef<EcsMetricKey>('cpu_util')
const detailTab = shallowRef('trend')
const { preset, history, appliedQuery, error, exportError, loading, exporting, canExport, refresh, exportExcel } = useEcsTrend({
  tenantId: () => props.tenantId, instanceId: () => props.instanceId, visible: () => props.modelValue,
  metric: () => metric.value, canExport: () => user.can('ledger:export'), fetcher: getEcsHistory, exporter: exportEcsHistory,
  canView: () => !!user.user && user.can('ledger:view'), scopeKey: () => resourceViewerScopeKey(user),
})
const statistic = shallowRef<'average' | 'maximum'>('average')
const chart = computed(() => history.value ? ecsTrendChart(history.value, metric.value, statistic.value) : {})
const hasData = computed(() => history.value?.series.some(series => series.metric === metric.value && series.points.some(point => point.sample_count > 0 && typeof point[statistic.value] === 'number' && Number.isFinite(point[statistic.value]))) ?? false)
const sources = computed(() => [...new Set(history.value?.series.filter(series => series.metric === metric.value).map(series => metricsSourceLabel(series.source)) ?? [])].join(' / '))
const instance = computed(() => history.value?.instance ?? (props.instance?.id === props.instanceId && props.instance?.tenant_id === props.tenantId ? props.instance : null))
const target = computed(() => instance.value && { ...instance.value, resource_type: 'ecs_server' })
const ledgerLink = computed(() => resourceLedgerLocation(target.value, user))
const alarmsLink = computed(() => resourceAlarmsLocation(target.value, user))
const readings = computed(() => instance.value ? ecsLatestReadings(instance.value) : [])
const metricLabel = computed(() => {
  const labels = [...new Set(history.value?.series.filter(series => series.metric === metric.value).map(series => series.label) ?? [])]
  return labels.join(' / ') || ECS_METRICS.find(item => item.key === metric.value)!.label
})
const detailPage = shallowRef(1)
watch(history, () => { detailPage.value = 1 })
watch(() => [props.modelValue, props.instanceId, props.tenantId] as const, () => { detailTab.value = 'trend' })
const detailRows = computed(() => history.value?.series.filter(series => series.metric === metric.value)
  .flatMap(series => series.points.map(point => ({ ...point, source: series.source, timestamp_semantics: series.timestamp_semantics })))
  .sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.source.localeCompare(b.source)) ?? [])
const visibleRows = computed(() => detailRows.value.slice((detailPage.value - 1) * 20, detailPage.value * 20))
// 来源代码显示为中文名称（例如 NONE → 仅服务原生）；旧 AOM 不能显示为当前来源
const sourceText = computed(() => {
  const code = instance.value?.metrics_source
  return !code || code === 'AOM' ? '未配置数据源' : METRICS_SOURCE[code]?.label ?? code
})
const canPolicy = computed(() => user.canAny('ledger:view', 'task:view') && instance.value?.tenant_id != null)
function openPolicy() { if (canPolicy.value && instance.value?.tenant_id != null) emit('policy', instance.value.tenant_id) }
async function onExport() { if (await exportExcel()) ElMessage.success('Excel 已导出') }
</script>

<template>
  <el-drawer :model-value="modelValue" size="min(1080px, 100vw)" title="ECS 实例监控" destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div class="trend-content">
      <header class="instance-summary">
        <div class="instance-title"><h2>{{ instance?.name ?? `实例 #${instanceId}` }}</h2><el-tag v-if="instance" :type="rawStatusType(instance.status_raw)" size="small">{{ statusText('ecs_server', instance.status_raw) }}</el-tag><el-tag v-if="instance?.critical" type="danger" size="small" effect="plain">核心</el-tag></div>
        <div v-if="instance" class="text-3 instance-scope">{{ instance.tenant_name }} · {{ instance.project_name ?? '未关联项目' }} · <span class="mono">{{ instance.ip ?? '无 IP' }}</span></div>
        <div class="text-3 mono resource-id">{{ instance?.resource_id }}</div>
        <div v-if="ledgerLink || alarmsLink" class="table-actions"><router-link v-if="ledgerLink" :to="ledgerLink">查看台账</router-link><router-link v-if="alarmsLink" :to="alarmsLink">相关告警</router-link></div>
      </header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon><el-button link type="primary" @click="refresh">重试</el-button></el-alert>
      <el-tabs v-model="detailTab" class="detail-tabs">
        <el-tab-pane label="趋势" name="trend">
          <div class="table-heading"><h3>指标趋势</h3><div class="table-actions"><el-button :loading="loading" @click="refresh">查询</el-button><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="onExport">导出 Excel</el-button></div></div>
          <div class="trend-filters">
            <el-radio-group v-model="preset" aria-label="时间范围"><el-radio-button v-for="range in ECS_PRESETS" :key="range.value" :value="range.value">{{ range.label }}</el-radio-button></el-radio-group>
            <el-select v-model="metric" aria-label="趋势指标" class="metric-select"><el-option v-for="item in ECS_METRICS" :key="item.key" :value="item.key" :label="item.label" /></el-select>
            <el-radio-group v-model="statistic" aria-label="统计方式"><el-radio-button value="average">均值</el-radio-button><el-radio-button value="maximum">峰值</el-radio-button></el-radio-group>
          </div>
          <p v-if="appliedQuery" class="text-3 range-caption">{{ fmtTime(appliedQuery.start_at, 'YYYY-MM-DD HH:mm:ss') }} 至 {{ fmtTime(appliedQuery.end_at, 'YYYY-MM-DD HH:mm:ss') }} · {{ intervalText(appliedQuery.interval_sec) }}聚合 · 本地时间</p>
          <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" show-icon />
          <el-alert v-for="warning in history?.warnings ?? []" :key="warning" :title="warning" type="warning" :closable="false" show-icon />
          <el-skeleton v-if="loading" :rows="8" animated />
          <EChart v-else-if="hasData && detailTab === 'trend'" :option="chart" height="300px" />
          <el-empty v-else-if="!hasData" :description="error ? '趋势加载失败，可重试' : '所选指标在此时段暂无样本'" :image-size="60" />
          <div class="trend-notes text-3"><span v-if="sources">历史来源：{{ sources }}</span><span>缺失样本保留断点；导出当前指标、时段及聚合粒度，含全部统计值。</span><span v-if="history">生成于 {{ fmtTime(history.generated_at) }} · 历史保留 {{ history.retention_days }} 天</span></div>
        </el-tab-pane>
        <el-tab-pane label="最近结果" name="samples" lazy>
          <div class="table-heading"><h3>最新结果</h3><span class="text-3 section-caption">当前快照，与历史查询时段无关；求值结果不等于物理采样</span></div>
          <el-table v-loading="loading && !instance" :data="readings" size="small">
            <el-table-column label="指标" prop="label" min-width="120" />
            <el-table-column label="最近结果值" width="110"><template #default="{ row }"><b>{{ percentText(row.value) }}</b></template></el-table-column>
            <el-table-column label="新鲜度" min-width="110"><template #default="{ row }"><el-tag size="small" :type="row.state === 'FRESH' ? 'success' : row.state === 'ERROR' ? 'danger' : row.state === 'STALE' ? 'warning' : 'info'">{{ ECS_STATE_LABEL[row.state as keyof typeof ECS_STATE_LABEL] }}</el-tag></template></el-table-column>
            <el-table-column label="时间语义" min-width="210"><template #default="{ row }">{{ ecsTimestampLabel(row.timestamp_semantics) }}</template></el-table-column>
            <el-table-column label="返回时间（本地）" min-width="180"><template #default="{ row }">{{ fmtTime(row.observed_at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
            <el-table-column label="入库时间（本地）" min-width="180"><template #default="{ row }">{{ fmtTime(row.collected_at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
            <el-table-column label="当前来源" min-width="130"><template #default="{ row }">{{ row.source ? metricsSourceLabel(row.source) : '无来源' }}</template></el-table-column>
          </el-table>
          <div class="table-heading sample-heading"><h3>{{ metricLabel }} · 聚合明细</h3><div class="table-actions"><el-select v-model="metric" aria-label="采样明细指标" class="metric-select"><el-option v-for="item in ECS_METRICS" :key="item.key" :value="item.key" :label="item.label" /></el-select><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="onExport">导出 Excel</el-button></div></div>
          <p class="text-3 section-caption">与趋势条件一致，最近窗口优先；缺失值为 —。</p>
          <el-table v-loading="loading" :data="visibleRows" size="small" stripe max-height="340">
            <el-table-column label="结果窗口（本地）" min-width="180"><template #default="{ row }">{{ fmtTime(row.at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
            <el-table-column label="来源" min-width="210"><template #default="{ row }">{{ metricsSourceLabel(row.source) }}</template></el-table-column>
            <el-table-column label="时间语义" min-width="210"><template #default="{ row }">{{ ecsTimestampLabel(row.timestamp_semantics) }}</template></el-table-column>
            <el-table-column label="均值" width="90"><template #default="{ row }">{{ percentText(row.sample_count > 0 ? row.average : null) }}</template></el-table-column>
            <el-table-column label="峰值" width="90"><template #default="{ row }">{{ percentText(row.sample_count > 0 ? row.maximum : null) }}</template></el-table-column>
            <el-table-column label="最低值" width="90"><template #default="{ row }">{{ percentText(row.sample_count > 0 ? row.minimum : null) }}</template></el-table-column>
            <el-table-column label="样本数" prop="sample_count" width="85" />
            <template #empty><el-empty description="当前指标与时段暂无聚合明细" :image-size="48" /></template>
          </el-table>
          <div class="samples-footer"><span v-if="appliedQuery" class="text-3 section-caption">{{ intervalText(appliedQuery.interval_sec) }}聚合 · {{ fmtTime(appliedQuery.start_at) }} 至 {{ fmtTime(appliedQuery.end_at) }}</span><el-pagination v-model:current-page="detailPage" :page-size="20" :total="detailRows.length" :pager-count="5" layout="total, prev, pager, next" /></div>
        </el-tab-pane>
        <el-tab-pane label="采集信息" name="collection" lazy>
          <div class="table-heading"><h3>当前采集与巡检</h3><div class="table-actions"><el-button v-if="canPolicy" @click="openPolicy">采集策略</el-button></div></div>
          <template v-if="instance">
            <dl class="collection-grid">
              <div><dt>台账纳管</dt><dd>{{ instance.monitored ? '已纳入' : '未纳入' }}</dd></div>
              <div><dt>策略范围</dt><dd>{{ instance.included ? '已纳入策略' : '未纳入策略' }}</dd></div>
              <div><dt>指标采集</dt><dd>{{ instance.metrics_enabled ? `已开启 · ${intervalText(instance.metrics_interval_sec)}` : '已关闭' }}</dd></div>
              <div><dt>巡检</dt><dd>{{ instance.inspection_enabled ? `已开启 · ${intervalText(instance.inspection_interval_sec)}` : '已关闭' }}</dd></div>
              <div><dt>当前指标来源</dt><dd>{{ sourceText }}</dd></div>
              <div><dt>结果过期判定</dt><dd>{{ intervalText(instance.stale_after_sec) }} · 按返回时间；PromQL 默认求值时间</dd></div>
              <div v-if="instance.metrics_source === 'MANAGEONE'"><dt>采集说明</dt><dd>云硬盘使用率不代表分区使用率；开机时间暂不支持。</dd></div>
            </dl>
            <el-alert v-if="instance.diagnostic?.message" :title="instance.diagnostic.message" type="info" :closable="false" show-icon />
            <p v-if="instance.diagnostic?.at" class="text-3 section-caption">诊断时间 {{ fmtTime(instance.diagnostic.at, 'YYYY-MM-DD HH:mm:ss') }}</p>
          </template>
          <el-empty v-else-if="!loading" description="尚无实例采集信息" :image-size="48" />
          <p class="text-3 collection-note">采集策略按租户配置；项目实际配额请在 ECS 监控的“项目配额”页签查看。</p>
        </el-tab-pane>
      </el-tabs>
    </div>
  </el-drawer>
</template>

<style scoped lang="scss">
.trend-content { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.instance-title { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.instance-title h2 { font-size: 18px; line-height: 1.5; margin: 0; overflow-wrap: anywhere; }
.instance-scope, .resource-id { margin-top: 4px; font-size: 12px; overflow-wrap: anywhere; }
.table-heading { margin: 4px 0 14px; }
.table-heading h3 { margin: 0; font-size: 14px; font-weight: 600; }
.trend-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 12px; }
.metric-select { width: 150px; }
.range-caption { margin: 12px 0; line-height: 1.7; font-size: 12px; overflow-wrap: anywhere; }
.trend-notes { display: flex; flex-direction: column; gap: 6px; font-size: 12px; line-height: 1.6; margin-top: 12px; overflow-wrap: anywhere; }
.detail-tabs { min-width: 0; }
.detail-tabs :deep(.el-alert) { margin-bottom: 10px; }
.sample-heading { margin-top: 24px; }
.section-caption, .collection-note { font-size: 12px; line-height: 1.7; }
.samples-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.samples-footer .el-pagination { max-width: 100%; overflow-x: auto; }
.collection-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0 0 16px; border-top: 1px solid var(--hi-border-light); }
.collection-grid > div { display: grid; grid-template-columns: 110px minmax(0, 1fr); padding: 14px 8px 14px 0; gap: 8px; border-bottom: 1px solid var(--hi-border-light); font-size: 13px; }
.collection-grid dt { color: var(--hi-text-3); }
.collection-grid dd { margin: 0; overflow-wrap: anywhere; }
.collection-note { margin: 16px 0 0; }
@media (max-width: 767px) {
  .trend-filters { gap: 10px 8px; }
  .trend-filters :deep(.el-radio-button__inner) { padding: 8px 10px; }
  .collection-grid { grid-template-columns: minmax(0, 1fr); }
  .collection-grid > div { grid-template-columns: 100px minmax(0, 1fr); }
}
</style>
