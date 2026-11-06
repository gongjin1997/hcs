<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import type { GaussMetricSeries } from '@/types/gaussdb'
import EChart from '@/components/EChart.vue'
import { fmtTime } from '@/utils/format'
import { finiteValue, gaussReportChart } from './gauss-chart'
import { quantityText } from './gauss-utils'

const props = defineProps<{ series: GaussMetricSeries[] }>()
const selectedIndex = shallowRef(0)
const metricKind = shallowRef<'all' | 'cpu' | 'memory'>('all')
const visibleSeries = computed(() => props.series.filter(item => metricKind.value === 'all' || item.metric === metricKind.value))
watch(() => props.series, () => { metricKind.value = 'all'; selectedIndex.value = 0 })
watch(metricKind, () => { selectedIndex.value = 0 })
const selected = computed(() => visibleSeries.value[selectedIndex.value] ?? null)
const option = computed(() => selected.value ? gaussReportChart(selected.value) : {})
const validPoints = computed(() => selected.value?.points.filter(point => finiteValue(point.value) !== null).length ?? 0)
const timeRange = computed(() => {
  const times = selected.value?.points.map(point => Date.parse(point.observed_at)).filter(Number.isFinite).sort((a, b) => a - b) ?? []
  return times.length ? [new Date(times[0]).toISOString(), new Date(times[times.length - 1]).toISOString()] : null
})
const label = (item: GaussMetricSeries) => `${item.name} · ${item.node_name || item.node_ip || '未标注节点'}${item.unit ? ` · ${item.unit}` : ''}`
</script>

<template>
  <section class="report-chart" aria-label="巡检指标趋势">
    <el-radio-group v-model="metricKind" size="small" class="metric-kinds" aria-label="巡检指标分类"><el-radio-button value="all">全部指标</el-radio-button><el-radio-button value="cpu">CPU</el-radio-button><el-radio-button value="memory">内存</el-radio-button></el-radio-group>
    <template v-if="selected">
    <div class="table-heading report-chart-heading">
      <h3>报告指标</h3>
      <el-select v-model="selectedIndex" filterable aria-label="报告指标与节点"><el-option v-for="(item, index) in visibleSeries" :key="index" :value="index" :label="label(item)" /></el-select>
    </div>
    <div class="report-metric-summary">
      <div><span>报告均值</span><strong>{{ quantityText(selected.average, selected.unit) }}</strong></div>
      <div><span>报告最大值</span><strong>{{ quantityText(selected.max, selected.unit) }}</strong></div>
      <div><span>报告最小值</span><strong>{{ quantityText(selected.min, selected.unit) }}</strong></div>
    </div>
    <EChart v-if="validPoints" :option="option" height="280px" />
    <el-empty v-else description="该指标没有有效时间序列" :image-size="48" />
    <div class="report-chart-caption text-3">
      <span v-if="timeRange">指标采样：{{ fmtTime(timeRange[0], 'YYYY-MM-DD HH:mm:ss') }} — {{ fmtTime(timeRange[1], 'YYYY-MM-DD HH:mm:ss') }}</span>
      <span>{{ validPoints }} 个有效值 · 单位：{{ selected.unit || '未提供' }} · 缺失处断线</span>
    </div>
    </template>
    <el-empty v-else :description="metricKind === 'memory' ? '此报告没有内存指标' : metricKind === 'cpu' ? '此报告没有 CPU 指标' : '此报告没有指标数据'" :image-size="48" />
  </section>
</template>

<style scoped>
.report-chart { margin: 18px 0 24px; min-width: 0; }
.metric-kinds { margin-bottom: 18px; }
.report-chart-heading { gap: 14px; margin-bottom: 16px; }
.report-chart-heading h3 { margin: 0; font-size: 16px; }
.report-chart-heading > .el-select { width: 430px; max-width: 100%; }
.report-metric-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; padding: 14px 0; border-top: 1px solid var(--hi-border-light); border-bottom: 1px solid var(--hi-border-light); margin-bottom: 18px; }
.report-metric-summary span { color: var(--hi-text-3); font-size: 12px; }
.report-metric-summary strong { display: block; font-size: 21px; font-weight: 600; margin-top: 6px; overflow-wrap: anywhere; }
.report-chart-caption { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; }
@media (max-width: 650px) { .report-chart-heading > .el-select { width: 100%; } .report-metric-summary { gap: 8px; } .report-metric-summary strong { font-size: 17px; } }
</style>
