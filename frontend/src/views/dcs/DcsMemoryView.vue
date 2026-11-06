<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Download, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import EChart from '@/components/EChart.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { monitoringListQuery, readMonitoringContext, resourceAlarmsLocation, resourceLedgerLocation, resourceViewerScopeKey } from '@/config/resource-navigation'
import { exportDcsMetricsHistory, getDcsInstance, getDcsMetricsHistory, listDcsInstances } from '@/api/dcs'
import type { DcsChartMetric, DcsIntervalMinutes, DcsRangePreset } from '@/types/dcs'
import { fmtTime } from '@/utils/format'
import { DCS_SOURCE_LABELS, INTERVAL_MINUTES, MAX_INSTANCES, metricText } from './dcs-utils'
import { memoryChart } from './memory-chart'
import { useDcsMemory } from './useDcsMemory'
import DcsCurrentStatistics from './DcsCurrentStatistics.vue'

const props = defineProps<{ projectRef?: number | null }>()
const app = useAppStore()
const user = useUserStore(), route = useRoute(), router = useRouter()
const scopeKey = computed(() => resourceViewerScopeKey(user))
const link = computed(() => readMonitoringContext(route.query, app.tenantId, user, 'report:view'))
const hasLink = () => route.query.instance_id != null || route.query.tenant_id != null
const model = useDcsMemory({ tenantId: () => app.tenantId, projectRef: () => props.projectRef ?? null,
  canView: () => !!user.user && user.can('report:view'), scopeKey: () => scopeKey.value, autoSelect: () => !hasLink(),
  instancesFetcher: listDcsInstances, historyFetcher: getDcsMetricsHistory, exporter: exportDcsMetricsHistory })
const { instanceOptions, instancesTotal, instanceKeyword, instancesLoading, instancesError, hasMoreInstances,
  selectedIds, intervalMinutes, rangePreset, customRange, history, appliedQuery, historyLoading,
  historyError, validationError, cancelled, exporting, exportError, canExport, tablePage, tableSize, tableRows, totalRows,
  searchInstances, loadMoreInstances, setSelectedIds, setIntervalMinutes, setRangePreset, setCustomDate,
  queryHistory, refresh, cancelHistory, exportExcel, cancelExport, setTablePage, setTableSize } = model
const metric = shallowRef<DcsChartMetric>('memory_usage')
const metricDefinition = computed(() => history.value?.metrics?.find(item => item.key === metric.value))
// 没有选择实例时显示当前范围内的全部实例（概览），选择后只看所选实例
const currentInstances = computed(() => history.value?.series.map(item => item.instance)
  ?? (selectedIds.value.length ? instanceOptions.value.filter(item => selectedIds.value.includes(item.id)) : instanceOptions.value))
const presets: { value: DcsRangePreset; label: string }[] = [
  { value: '1d', label: '最近 1 天' }, { value: '2d', label: '最近 2 天' },
  { value: '7d', label: '最近 7 天' }, { value: 'custom', label: '自定义' },
]
const sourceLabel = DCS_SOURCE_LABELS
const chartHasData = computed(() => history.value?.series.some(series => series.points.some(point =>
  typeof point[metric.value] === 'number' && Number.isFinite(point[metric.value]))) ?? false)
const chart = computed(() => history.value ? memoryChart(history.value, metric.value) : {})
const busy = computed(() => instancesLoading.value || historyLoading.value || model.opening.value)
const selectedTarget = computed(() => selectedIds.value.length === 1 ? instanceOptions.value.find(item => item.id === selectedIds.value[0]) : null)
const ledgerLink = computed(() => resourceLedgerLocation(selectedTarget.value && { ...selectedTarget.value, resource_type: 'dcs_instance' }, user))
const alarmsLink = computed(() => resourceAlarmsLocation(selectedTarget.value && { ...selectedTarget.value, resource_type: 'dcs_instance' }, user))
watch(() => [route.query.instance_id, route.query.tenant_id, app.tenantId, scopeKey.value] as const, async (_values, previous) => {
  if (previous && (previous[2] !== app.tenantId || previous[3] !== scopeKey.value)) {
    model.clearSelection()
    if (hasLink()) await router.replace({ query: monitoringListQuery(route.query) })
    return
  }
  const { context, error } = link.value
  if (!context) {
    model.cancelInstanceOpen()
    if (error || selectedIds.value.length === 1) model.clearSelection()
    return
  }
  if (selectedTarget.value?.id === context.instanceId && selectedTarget.value.tenant_id === context.tenantId) return
  await model.openInstance(context.instanceId, context.tenantId, getDcsInstance)
}, { immediate: true, flush: 'sync' })

async function selectInstances(ids: number[]) {
  const pending = setSelectedIds(ids)
  const target = ids.length === 1 ? instanceOptions.value.find(item => item.id === ids[0]) : null
  const query = target?.tenant_id ? { ...monitoringListQuery(route.query), instance_id: String(target.id), tenant_id: String(target.tenant_id) }
    : monitoringListQuery(route.query)
  await router.replace({ query })
  await pending
}
const emptyText = computed(() => {
  if (instancesLoading.value && !selectedIds.value.length) return '正在加载实例'
  if (instancesError.value && !selectedIds.value.length) return '实例加载失败，请重试'
  if (!selectedIds.value.length) return !instanceKeyword.value && instancesTotal.value === 0 ? '当前范围暂无 DCS 实例' : '请选择实例'
  if (validationError.value) return '请调整查询条件'
  if (historyError.value) return '指标查询失败，请重试'
  if (cancelled.value) return '查询已取消'
  if (!history.value) return '请选择实例和时间查询'
  if (!history.value.series.length) return '所选时段未返回实例指标'
  return '所选时段暂无有效数据'
})

async function onExport() {
  if (await exportExcel()) ElMessage.success('Excel 已导出')
}
</script>

<template>
  <div class="dcs-memory-page">
    <PageHeader title="DCS 监控" :refresh="refresh" :refreshing="busy" />
    <el-alert v-if="link.error || model.openingError.value" :title="link.error || model.openingError.value" type="error" :closable="false" show-icon />
    <div v-if="ledgerLink || alarmsLink" class="table-actions"><router-link v-if="ledgerLink" :to="ledgerLink">查看台账</router-link><router-link v-if="alarmsLink" :to="alarmsLink">相关告警</router-link></div>
    <section class="hi-card filters" aria-label="DCS 指标筛选">
      <div class="hi-card__title table-heading"><span>DCS 实例<span class="sub">{{ app.tenantLabel }}</span></span>
      <div class="table-actions instance-filter">
        <label for="dcs-instances">实例</label>
        <el-select id="dcs-instances" :model-value="selectedIds" multiple filterable remote reserve-keyword clearable
          collapse-tags collapse-tags-tooltip :max-collapse-tags="3" :multiple-limit="MAX_INSTANCES"
          :loading="instancesLoading" :remote-method="searchInstances" :suffix-icon="Search" placeholder="搜索名称或资源 ID，最多 20 个"
          aria-label="选择 DCS 实例" no-data-text="暂无实例" no-match-text="没有匹配实例" @update:model-value="selectInstances">
          <el-option v-for="item in instanceOptions" :key="item.id" :value="item.id"
            :label="`${item.name} · ${item.tenant_name}${item.project_name ? ` / ${item.project_name}` : ''} · #${item.id}`">
            <span>{{ item.name }}</span><span class="option-scope">{{ item.tenant_name }} · {{ item.project_name ?? item.region ?? item.resource_id }} · #{{ item.id }}</span>
          </el-option>
          <template #footer>
            <div class="instance-footer">
              <span class="text-3">已选 {{ selectedIds.length }}/20 · 匹配 {{ instancesTotal }} 个</span>
              <el-button v-if="hasMoreInstances" link type="primary" :loading="instancesLoading" @click="loadMoreInstances">加载更多</el-button>
              <span v-if="instanceKeyword && !instancesTotal && !instancesLoading" class="text-3">没有匹配实例</span>
            </div>
          </template>
        </el-select>
        <el-button type="primary" :disabled="!selectedIds.length" :loading="historyLoading" @click="queryHistory">查询</el-button>
        <el-button :icon="Download" :disabled="!canExport" :loading="exporting" @click="onExport">导出 Excel</el-button>
        <el-button v-if="historyLoading" @click="cancelHistory">取消查询</el-button>
        <el-button v-if="exporting" @click="cancelExport">取消导出</el-button>
      </div></div>
      <div class="time-filters">
        <el-radio-group :model-value="rangePreset" aria-label="时间范围" @update:model-value="value => setRangePreset(value as DcsRangePreset)">
          <el-radio-button v-for="preset in presets" :key="preset.value" :value="preset.value">{{ preset.label }}</el-radio-button>
        </el-radio-group>
        <div class="interval-filter"><span>聚合</span><el-select :model-value="intervalMinutes" aria-label="聚合粒度" @update:model-value="value => setIntervalMinutes(value as DcsIntervalMinutes)">
          <el-option v-for="interval in INTERVAL_MINUTES" :key="interval" :value="interval" :label="`${interval} 分钟`" />
        </el-select></div>
      </div>
      <div v-if="rangePreset === 'custom'" class="custom-range">
        <el-date-picker :model-value="customRange[0]" type="datetime" placeholder="开始时间" aria-label="开始时间" format="YYYY-MM-DD HH:mm:ss" @update:model-value="value => setCustomDate(0, value)" />
        <span>至</span>
        <el-date-picker :model-value="customRange[1]" type="datetime" placeholder="结束时间" aria-label="结束时间" format="YYYY-MM-DD HH:mm:ss" @update:model-value="value => setCustomDate(1, value)" />
        <span class="text-3">本地时间，最长 31 天；含起点、不含终点</span>
      </div>
      <el-alert v-if="instancesError" :title="instancesError" type="error" show-icon :closable="false">
        <el-button link type="primary" :loading="instancesLoading" @click="model.loadInstances()">重试实例列表</el-button>
      </el-alert>
      <el-alert v-if="validationError" :title="validationError" type="warning" show-icon :closable="false" />
    </section>

    <DcsCurrentStatistics :instances="currentInstances" :loading="busy" :selected-ids="selectedIds" @select="id => selectInstances([id])" />
    <el-alert v-if="historyError" :title="historyError" type="error" show-icon :closable="false">
      <el-button link type="primary" :loading="historyLoading" @click="queryHistory">重试查询</el-button>
    </el-alert>
    <section class="hi-card trend-section">
      <div class="hi-card__title table-heading">
        <span>{{ metricDefinition?.label ?? '内存使用率' }} <el-tag v-if="history?.demo" size="small" type="warning" effect="plain">演示数据</el-tag></span>
        <el-select v-model="metric" aria-label="趋势指标" class="metric-select">
          <el-option v-for="item in history?.metrics ?? []" :key="item.key" :value="item.key"
            :label="`${item.label}（${item.unit}，${item.aggregation === 'mean' ? '均值' : '末值'}）`" />
          <el-option value="memory_max" label="内存使用率（%，峰值）" />
        </el-select>
      </div>
      <div v-if="appliedQuery" class="range-caption text-3">{{ fmtTime(appliedQuery.start_at, 'YYYY-MM-DD HH:mm:ss') }} 至 {{ fmtTime(appliedQuery.end_at, 'YYYY-MM-DD HH:mm:ss') }} · {{ appliedQuery.interval_minutes }} 分钟 · 本地时间 · 含起点、不含终点</div>
      <el-skeleton v-if="historyLoading" :rows="7" animated />
      <EChart v-else-if="history && chartHasData" :option="chart" height="340px" />
      <el-empty v-else :description="emptyText" :image-size="80" />
      <div v-if="history" class="chart-note text-3">本地获取记录；缺失时段留空 · V1 CPU% 尚未启用</div>
    </section>

    <section v-if="history?.series.length" class="hi-card summary-section">
      <div class="hi-card__title">实例概况</div>
      <el-table :data="history.series" row-key="instance.id" size="small" stripe>
        <el-table-column label="实例" min-width="205"><template #default="{ row }">
          <strong>{{ row.instance.name }}</strong><div class="text-3">{{ row.instance.tenant_name }} · {{ row.instance.project_name ?? row.instance.resource_id }}</div>
        </template></el-table-column>
        <el-table-column :label="`均值 ${metricDefinition?.unit ?? '%'}`" width="120"><template #default="{ row }">{{ metricText(row.summaries?.[metric]?.average) }}</template></el-table-column>
        <el-table-column :label="`峰值 ${metricDefinition?.unit ?? '%'}`" width="120"><template #default="{ row }">{{ metricText(metric === 'memory_max' ? row.summary.peak_usage : row.summaries?.[metric]?.peak) }}</template></el-table-column>
        <el-table-column :label="`最近值 ${metricDefinition?.unit ?? '%'}`" width="120"><template #default="{ row }">{{ metricText(row.summaries?.[metric]?.latest) }}</template></el-table-column>
        <el-table-column label="最近获取时间" min-width="180"><template #default="{ row }">{{ fmtTime(row.summaries?.[metric]?.latest_at ?? row.summary.latest_at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
        <el-table-column label="有效点 / 总点数" width="155"><template #default="{ row }">{{ row.valid_points }} / {{ row.total_points }}</template></el-table-column>
        <el-table-column label="来源与限制" min-width="290"><template #default="{ row }">
          <el-tag size="small" type="info" effect="plain">{{ sourceLabel[row.source as keyof typeof sourceLabel] }}</el-tag>
          <span v-if="row.status !== 'AVAILABLE'" class="series-state text-3">{{ row.status === 'PARTIAL' ? '部分缺失' : '无数据' }}</span>
          <div v-if="row.warning" class="source-warning">{{ row.warning }}</div>
        </template></el-table-column>
      </el-table>
    </section>

    <section class="hi-card metrics-section">
      <div class="hi-card__title table-heading">
        <span>指标明细<span class="sub">{{ totalRows }} 个时段</span></span>

      </div>
      <el-alert v-if="exportError" :title="exportError" type="error" show-icon :closable="false" />
      <el-table v-loading="historyLoading" :data="tableRows" row-key="key" size="small" stripe>
        <el-table-column label="实例" min-width="180"><template #default="{ row }">{{ row.instance.name }}<div class="text-3">#{{ row.instance.id }} · {{ row.instance.tenant_name }}</div></template></el-table-column>
        <el-table-column label="时段（本地）" min-width="180"><template #default="{ row }">{{ fmtTime(row.at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
        <el-table-column label="均值 %" width="100"><template #default="{ row }">{{ metricText(row.memory_usage) }}</template></el-table-column>
        <el-table-column label="峰值 %" width="100"><template #default="{ row }">{{ metricText(row.memory_max) }}</template></el-table-column>
        <el-table-column label="已用内存 MB" min-width="120"><template #default="{ row }">{{ metricText(row.used_memory_mb) }}</template></el-table-column>
        <el-table-column label="内存上限 MB" min-width="120"><template #default="{ row }">{{ metricText(row.max_memory_mb) }}</template></el-table-column>
        <el-table-column label="CPU累计时间 s（末值）" min-width="180"><template #default="{ row }">{{ metricText(row.used_cpu_seconds) }}</template></el-table-column>
        <el-table-column label="网络入 kbit/s（均值）" min-width="180"><template #default="{ row }">{{ metricText(row.input_kbps) }}</template></el-table-column>
        <el-table-column label="网络出 kbit/s（均值）" min-width="180"><template #default="{ row }">{{ metricText(row.output_kbps) }}</template></el-table-column>
        <el-table-column label="数据条数（末值）" min-width="150"><template #default="{ row }">{{ metricText(row.keys) }}</template></el-table-column>
        <el-table-column label="GET累计次数（末值）" min-width="170"><template #default="{ row }">{{ metricText(row.cmd_get_count) }}</template></el-table-column>
        <el-table-column label="SET累计次数（末值）" min-width="170"><template #default="{ row }">{{ metricText(row.cmd_set_count) }}</template></el-table-column>
        <el-table-column label="末次本地获取时间" min-width="180"><template #default="{ row }">{{ fmtTime(row.last_observed_at, 'YYYY-MM-DD HH:mm:ss') }}</template></el-table-column>
        <el-table-column prop="sample_count" label="采样数" width="110" />
        <el-table-column label="来源" min-width="140"><template #default="{ row }">{{ sourceLabel[row.source as keyof typeof sourceLabel] }}</template></el-table-column>
        <template #empty><el-empty :description="history ? '暂无时段数据' : emptyText" :image-size="48" /></template>
      </el-table>
      <div class="metrics-footer">
        <el-pagination :current-page="tablePage" :page-size="tableSize" :total="totalRows" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next"
          @update:current-page="setTablePage" @update:page-size="setTableSize" />
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.dcs-memory-page { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.hi-card { min-width: 0; }
.filter-heading, .time-filters, .custom-range, .interval-filter, .instance-filter, .instance-footer { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.metric-select { width: 290px; max-width: 100%; }
.instance-filter { flex: 1; justify-content: flex-end; margin-bottom: 0; }
.instance-filter label { flex: none; font-size: 13px; }
.instance-filter > .el-select { flex: 1; min-width: 220px; max-width: 620px; }
.option-scope { margin-left: 12px; color: var(--hi-text-3); font-size: 12px; }
.instance-footer { justify-content: space-between; font-size: 12px; }
.interval-filter .el-select { width: 115px; }
.custom-range { margin-top: 12px; }
.custom-range .el-date-editor { max-width: 100%; }
.filters .el-alert { margin-top: 12px; }
.range-caption { margin-bottom: 12px; overflow-wrap: anywhere; font-size: 12px; }
.chart-note { font-size: 12px; margin-top: 8px; }
.hi-card__title > span { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.hi-card__title .sub { margin-left: 0; }
.source-warning { font-size: 12px; line-height: 1.6; margin-top: 6px; overflow-wrap: anywhere; }
.series-state { margin-left: 8px; font-size: 12px; }
.summary-section :deep(.cell) { overflow-wrap: anywhere; }
.metrics-footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 12px; align-items: center; margin-top: 14px; overflow-x: auto; }
@media (max-width: 650px) {
  .filters .table-heading { flex-direction: column; align-items: stretch; gap: 12px; }
  .instance-filter { width: 100%; align-items: center; justify-content: flex-start; }
  .instance-filter > .el-select { flex-basis: calc(100% - 42px); min-width: 0; max-width: none; }
  .time-filters > .el-radio-group { display: flex; flex-wrap: wrap; max-width: 100%; }
  .custom-range { align-items: stretch; flex-direction: column; }
  .custom-range .el-date-editor { width: 100%; }
  .metrics-footer { align-items: flex-start; flex-direction: column; }
  .metrics-footer .el-pagination { max-width: 100%; overflow-x: auto; }
}
</style>
