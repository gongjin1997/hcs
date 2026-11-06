<script setup lang="ts">
import { computed, watch } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import type { GaussPanelModel } from './useGaussPanel'
import { inspectionTime, nativeText } from './gauss-utils'
import GaussInspectionDetail from './GaussInspectionDetail.vue'
import GaussNativeStatus from './GaussNativeStatus.vue'

const props = defineProps<{ model: GaussPanelModel; canRun: boolean }>()
const user = useUserStore()
const { rows, detail, inspectionRecord, inspectionFilters, inspectionRange, loading, loadingKind, exporting, canExport, canExportDetail } = props.model
const page = computed(() => Math.floor(inspectionFilters.offset / inspectionFilters.limit) + 1)
const canNext = computed(() => inspectionFilters.offset + inspectionFilters.limit <= 1000 && (rows.value?.total != null ? inspectionFilters.offset + inspectionFilters.limit < rows.value.total : (rows.value?.items.length ?? 0) >= inspectionFilters.limit))
async function query() { await props.model.queryPanel() }
async function changePage(value: number) { props.model.setInspectionPage(value); await query() }
async function changeSize(value: number) { inspectionFilters.offset = 0; inspectionFilters.limit = value; await query() }
watch(rows, result => {
  const first = result?.items.find(row => row.status === 'completed' && typeof row.record_id === 'string')
  if (props.canRun && first && !inspectionRecord.value) void props.model.showDetail(first)
}, { immediate: true })
</script>

<template>
  <div class="performance-panel">
    <header class="table-heading"><div><h3>性能与巡检报告</h3><p class="text-3">CPU、内存来自已完成报告，按报告中的采样时间展示。</p></div><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="model.exportExcel">导出记录 Excel</el-button></header>
    <form class="report-filters" @submit.prevent="query">
      <el-date-picker :model-value="inspectionRange" type="datetimerange" aria-label="巡检时间范围" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY-MM-DD HH:mm" @update:model-value="value => model.setRange('inspection', value)" />
      <el-button native-type="submit" type="primary" :disabled="!canRun || loading" :loading="loadingKind === 'query'">查询报告</el-button>
    </form>
    <GaussNativeStatus v-if="rows" :status="rows.status" :warning="rows.warning" :queried-at="rows.queried_at" />
    <div class="report-layout">
      <aside class="record-list" aria-label="巡检报告列表">
        <div class="records-heading">巡检记录 <span>{{ rows?.total ?? rows?.items.length ?? '—' }}</span></div>
        <el-skeleton v-if="loadingKind === 'query'" :rows="5" animated />
        <template v-else-if="rows?.items.length">
          <button v-for="record in rows.items" :key="String(record.record_id)" type="button" class="record-item" :class="{ selected: inspectionRecord?.record_id === record.record_id }" :aria-pressed="inspectionRecord?.record_id === record.record_id" :disabled="!canRun || loading || record.status !== 'completed'" @click="model.showDetail(record)">
            <strong>{{ inspectionTime(record.start_time) }}</strong><span>{{ record.task_name || '巡检报告' }}</span><small><span>{{ record.status === 'completed' ? '已完成' : nativeText(record.status) }}</span><span v-if="record.abnormal_count != null">异常 {{ record.abnormal_count }}</span></small>
          </button>
        </template>
        <el-empty v-else :description="rows ? '未查到巡检记录' : '暂无巡检记录'" :image-size="42" />
        <div class="record-pagination"><el-button size="small" :disabled="page === 1 || loading || !canRun" @click="changePage(page - 1)">上一页</el-button><span>{{ page }}</span><el-button size="small" :disabled="!canNext || loading || !canRun" @click="changePage(page + 1)">下一页</el-button><el-select :model-value="inspectionFilters.limit" size="small" aria-label="巡检每页条数" :disabled="loading" @update:model-value="changeSize"><el-option v-for="size in [10, 20, 50, 100]" :key="size" :value="size" :label="`${size} 条/页`" /></el-select></div>
      </aside>
      <article class="record-detail" aria-label="巡检报告详情">
        <el-skeleton v-if="loadingKind === 'detail'" :rows="8" animated />
        <template v-else-if="detail && inspectionRecord"><div class="table-heading detail-heading"><h3>{{ inspectionRecord.task_name || '报告详情' }}</h3><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExportDetail" :loading="exporting" @click="model.exportInspectionDetail">导出报告 Excel</el-button></div><GaussInspectionDetail :result="detail" :record="inspectionRecord" /></template>
        <el-empty v-else description="选择一份已完成报告查看指标和检查结果" :image-size="60" />
      </article>
    </div>
  </div>
</template>

<style scoped>
.performance-panel { min-width: 0; }h3 { font-size: 15px; margin: 0; }p { margin: 7px 0 0; font-size: 12px; }.table-heading { margin-bottom: 18px; align-items: flex-start; }
.report-filters { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }.report-filters .el-date-editor { flex: 0 1 430px; min-width: 280px; }
.report-layout { display: grid; grid-template-columns: 260px minmax(0, 1fr); border-top: 1px solid var(--hi-border); margin-top: 16px; }
.record-list { min-width: 0; border-right: 1px solid var(--hi-border); padding: 18px 18px 0 0; }.records-heading { display: flex; justify-content: space-between; font-size: 13px; color: var(--hi-text-2); margin-bottom: 12px; }
.record-item { width: 100%; display: flex; flex-direction: column; gap: 8px; border: 1px solid transparent; background: transparent; padding: 14px 12px; border-radius: 6px; cursor: pointer; text-align: left; color: var(--hi-text-2); font-family: inherit; }.record-item:hover { background: var(--hi-bg-soft); }.record-item.selected { background: var(--hi-bg-hover); border-color: var(--el-color-primary-light-7); }.record-item:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }.record-item strong { font-size: 13px; color: var(--hi-text-1); }.record-item > span { font-size: 12px; overflow-wrap: anywhere; }.record-item small { display: flex; justify-content: space-between; color: var(--hi-text-3); }.record-item:disabled { cursor: default; }
.record-pagination { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 18px; font-size: 12px; }.record-pagination .el-button { margin-left: 0; }.record-pagination .el-select { width: 110px; }.record-detail { min-width: 0; padding: 20px 0 0 24px; }.detail-heading { align-items: center; }
@media (max-width: 1000px) { .report-layout { grid-template-columns: minmax(0, 1fr); }.record-list { border-right: 0; padding: 16px 0; border-bottom: 1px solid var(--hi-border); }.record-item { padding: 12px; }.record-detail { padding: 20px 0 0; }.report-filters .el-date-editor { width: 100%; min-width: 0; flex-basis: 100%; } }
</style>
