<script setup lang="ts">
import type { GaussNativeRow, GaussNativeRows } from '@/types/gaussdb'
import { fmtTime } from '@/utils/format'
import { indicatorMetrics, inspectionIndicators, inspectionPoints, inspectionTime, nativeObject, nativeText } from './gauss-utils'
import GaussNativeStatus from './GaussNativeStatus.vue'
import GaussReportChart from './GaussReportChart.vue'

defineProps<{ result: GaussNativeRows; record: GaussNativeRow | null }>()
const hasNativeData = (value: unknown) => nativeObject(value) !== null || (Array.isArray(value) && value.some(item => nativeObject(item) !== null))
const metricUnit = (indicator: GaussNativeRow, metric: GaussNativeRow) => nativeText(metric.unit ?? indicator.unit)
const statusLabel = (value: unknown) => ({ normal: '正常', abnormal: '异常', failed: '失败', success: '正常', warning: '警告' }[String(value)] || nativeText(value))
const statusType = (value: unknown) => ['normal', 'success'].includes(String(value)) ? 'success' : ['abnormal', 'failed'].includes(String(value)) ? 'danger' : 'info'
</script>

<template>
  <div class="inspection-detail">
    <GaussNativeStatus :status="result.status" :warning="result.status === 'AVAILABLE' ? null : result.warning" />
    <div class="report-period text-3"><span>报告时间</span>{{ fmtTime(result.start_at, 'YYYY-MM-DD HH:mm:ss') }} — {{ fmtTime(result.end_at, 'YYYY-MM-DD HH:mm:ss') }}</div>
    <GaussReportChart v-if="result.metric_series?.length" :series="result.metric_series" />
    <el-empty v-else description="此报告未提供可绘制的 CPU 或内存指标" :image-size="48" />
    <section class="check-results" aria-label="巡检检查结果">
      <h3>检查结果 <span class="text-3">{{ result.items.length }} 项</span></h3>
      <el-empty v-if="!result.items.length" description="巡检报告暂无明细" :image-size="48" />
      <article v-for="(item, itemIndex) in result.items" :key="itemIndex" class="check-item">
        <div class="check-heading"><strong>{{ item.name || item.item || '未命名检查项' }}</strong><el-tag size="small" effect="plain" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag></div>
        <p v-if="item.inspection_result" class="check-result">{{ nativeText(item.inspection_result) }}</p>
        <dl v-if="item.possible_causes || item.suggestions" class="check-advice"><template v-if="item.possible_causes"><dt>原因</dt><dd>{{ nativeText(item.possible_causes) }}</dd></template><template v-if="item.suggestions"><dt>建议</dt><dd>{{ nativeText(item.suggestions) }}</dd></template></dl>
        <details class="raw-inspection"><summary>检查项与原始采样</summary>
          <p v-if="item.description" class="text-3">{{ nativeText(item.description) }}</p>
          <p v-if="item.item" class="text-3">检查项：{{ nativeText(item.item) }}</p>
          <section v-for="(indicator, index) in inspectionIndicators(item)" :key="index" class="indicator-block">
            <h4>{{ nativeText(indicator.name) }}</h4><p class="text-3">节点：{{ nativeText(indicator.node) }} · 指标单位：{{ nativeText(indicator.unit) }}</p>
            <p v-if="indicator.remarks" class="text-3">{{ nativeText(indicator.remarks) }}</p>
            <div v-for="(metric, metricIndex) in indicatorMetrics(indicator)" :key="metricIndex" class="metric-block">
              <div class="metric-heading">{{ nativeText(metric.name) }} · IP：{{ nativeText(metric.ip) }} · 采样单位：{{ metricUnit(indicator, metric) }}<span v-if="metric.unit == null && indicator.unit != null" class="text-3">（指标单位）</span></div>
              <div class="metric-stats"><span v-if="metric.value != null">值：{{ nativeText(metric.value) }}</span><span>最小值：{{ nativeText(metric.min) }}</span><span>最大值：{{ nativeText(metric.max) }}</span><span>均值：{{ nativeText(metric.average) }}</span><span v-if="metric.p95 != null">P95：{{ nativeText(metric.p95) }}</span></div>
              <el-table v-if="hasNativeData(metric.data)" :data="inspectionPoints(metric.data)" size="small">
                <el-table-column label="采样时间（本地，原始时间为毫秒）" min-width="230"><template #default="{ row }"><span :title="nativeText(row.time)">{{ inspectionTime(row.time) }}</span></template></el-table-column>
                <el-table-column label="采样值" min-width="120"><template #default="{ row }">{{ nativeText(row.value) }}</template></el-table-column>
                <el-table-column label="采样单位" min-width="100"><template #default>{{ metricUnit(indicator, metric) }}</template></el-table-column>
              </el-table>
              <pre v-else class="native-value">{{ nativeText(metric.data) }}</pre>
            </div>
          </section>
        </details>
      </article>
    </section>
  </div>
</template>

<style scoped>
.inspection-detail { min-width: 0; }.report-period { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; }
.check-results { border-top: 1px solid var(--hi-border); margin-top: 24px; padding-top: 18px; }.check-results h3 { margin: 0 0 16px; font-size: 15px; }.check-results h3 span { font-size: 12px; font-weight: 400; margin-left: 8px; }.check-item { padding: 16px 0; border-bottom: 1px solid var(--hi-border-light); }.check-heading { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 14px; }.check-result { font-size: 13px; line-height: 1.7; margin: 10px 0; white-space: pre-wrap; overflow-wrap: anywhere; }.check-advice { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 8px 14px; font-size: 13px; }.check-advice dt { color: var(--hi-text-3); }.check-advice dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.raw-inspection { margin-top: 10px; }.raw-inspection > summary { cursor: pointer; font-size: 12px; color: var(--el-color-primary); padding: 5px 0; }.raw-inspection p { font-size: 12px; line-height: 1.7; }.native-value { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }.indicator-block { min-width: 0; margin-top: 16px; }.indicator-block h4 { margin: 0; font-size: 13px; }.metric-block { min-width: 0; padding: 12px 0; border-top: 1px solid var(--hi-border-light); }.metric-heading { font-size: 12px; overflow-wrap: anywhere; }.metric-stats { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 10px 0; font-size: 12px; }
</style>
