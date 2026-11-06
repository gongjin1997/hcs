<template>
  <div class="job-report-page">
    <PageHeader :title="config.title" :refresh="refresh" :refreshing="loading" />

    <section class="hi-card filters" aria-label="报表筛选">
      <div class="scope-top">
        <strong>作业概况</strong><span class="text-3">{{ app.tenantLabel }} · 最新观测快照</span>
      </div>
      <div class="filter-bar">
        <el-input :model-value="query.keyword" placeholder="搜索作业名称 / 资源 ID" aria-label="关键词" clearable :maxlength="255" :prefix-icon="Search"
                  @update:model-value="value => setFilters({ keyword: String(value) }, 300)" @keyup.enter="refresh" />
        <el-select :model-value="query.status" multiple filterable allow-create default-first-option clearable collapse-tags collapse-tags-tooltip placeholder="全部执行状态" aria-label="执行状态"
                   @update:model-value="value => setFilters({ status: value })">
          <el-option v-for="status in statusOptions" :key="status" :label="statusMeta(status).label" :value="status" />
        </el-select>
        <el-select :model-value="query.engine" filterable allow-create default-first-option clearable placeholder="全部引擎" aria-label="引擎"
                   @update:model-value="value => setFilters({ engine: value ?? '' })">
          <el-option v-for="engine in engineOptions" :key="engine" :label="engine" :value="engine" />
        </el-select>
        <el-button @click="reset">重置</el-button>
      </div>
    </section>

    <el-alert v-if="error" :title="report ? `刷新失败：${error}；显示上次查询结果` : `加载失败：${error}`" type="error" :closable="false" show-icon>
      <el-button link type="primary" @click="refresh">重新加载</el-button>
    </el-alert>
    <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" show-icon />
    <div v-if="exporting" class="hi-card export-progress" role="status">
      <span>正在生成 Excel…</span>
      <el-button size="small" @click="cancelExport">取消导出</el-button>
    </div>

    <div v-if="!report && loading" class="hi-card"><el-skeleton :rows="8" animated /></div>
    <template v-if="report">
      <div v-loading="loading" class="report-content" :aria-busy="loading">
        <section class="kpi-grid" aria-label="作业统计">
          <div class="hi-card kpi"><span>{{ config.totalLabel }}</span><strong class="num">{{ report.summary.total }}</strong><small>当前筛选</small></div>
          <div v-for="status in config.focus" :key="status" class="hi-card kpi">
            <span>{{ statusMeta(status).label }}</span><strong class="num" :style="{ color: statusMeta(status).color }">{{ report.summary.status_counts[status] ?? 0 }}</strong>
          </div>
          <div class="hi-card kpi"><span>活动告警</span><strong class="num" :class="{ 'text-danger': report.summary.active_alarms > 0 }">{{ report.summary.active_alarms }}</strong></div>
        </section>

        <div class="exhibit-grid">
          <section class="hi-card status-section">
            <div class="hi-card__title">状态分布</div>
            <div class="status-overview">
              <EChart v-if="report.total" :option="statusChart" height="200px" class="status-chart" />
              <el-empty v-else description="暂无状态分布" :image-size="64" />
              <div class="status-list">
                <button v-for="entry in statusEntries" :key="entry.key" type="button" class="status-item" :aria-pressed="query.status?.includes(entry.key) ?? false" @click="toggleStatus(entry.key)">
                  <i :style="{ background: statusMeta(entry.key).color }" /><span>{{ statusMeta(entry.key).label }}</span><b class="num">{{ entry.count }}</b>
                </button>
              </div>
            </div>
            <div class="coverage-note">未知 {{ report.summary.unknown_total }} 条 · 无状态 {{ report.summary.no_data_total }} 条</div>
          </section>
          <section class="hi-card dimension-section">
            <div class="hi-card__title">{{ panel === 'dgc' ? 'DGC 作业类型' : '来源' }}<span class="sub">当前筛选</span></div>
            <div v-for="entry in dimensionEntries" :key="entry.key" class="dimension-row"><span>{{ entry.label }}</span><b class="num">{{ entry.count }} 条</b></div>
            <div v-if="!dimensionEntries.length" class="dimension-empty">无来源数据</div>
            <div class="section-title">引擎分布</div>
            <div class="engine-list">
              <div v-for="engine in engineEntries" :key="engine.key" class="dimension-row"><span>{{ engine.key === 'UNKNOWN' ? '未识别引擎' : engine.key }}</span><b class="num">{{ engine.count }} 条</b></div>
              <div v-if="!engineEntries.length" class="dimension-empty">无引擎数据</div>
            </div>
          </section>
        </div>

        <section class="hi-card detail-section">
          <div class="hi-card__title table-heading">
            <span>作业明细<span class="sub">共 {{ report.total }} 条</span></span>
            <div class="table-actions">
              <el-button v-perm="'report:view'" type="primary" :icon="Download" :loading="exporting" :disabled="loading || !report.total" @click="onExport">导出 Excel</el-button>
            </div>
          </div>
          <el-table :key="panel" :data="tableRows" row-key="id" size="small" stripe>
            <el-table-column label="作业 / 资源 ID" min-width="210" fixed="left"><template #default="{ row }">
              <el-button link type="primary" class="job-name" @click="openRow(row)">{{ row.name }}</el-button>
              <div class="mono text-3 ellipsis" :title="row.resource_id">{{ row.resource_id }}</div>
            </template></el-table-column>
            <el-table-column label="作业状态" min-width="175"><template #default="{ row }">
              <el-tag :type="statusMeta(row.display_status).type" size="small" class="state-tag">{{ statusMeta(row.display_status).label }}</el-tag>
              <div class="mono text-3 cell-fact">原始：{{ factText(row.status_raw) }}</div>
              <div v-if="row.execution_state !== row.status_raw" class="mono text-3 cell-fact">执行：{{ factText(row.execution_state) }}</div>
            </template></el-table-column>

            <template v-if="panel === 'realtime'">
              <el-table-column label="节点状态" min-width="255"><template #default="{ row }">
                <div v-for="(node, index) in row.nodePreview" :key="index" class="ellipsis" :title="`${node.name}：${node.status}`">{{ node.name }}：<span class="mono">{{ node.status }}</span></div>
                <span v-if="!row.nodeItems.length" class="text-3">未采集节点明细</span>
                <el-button v-if="row.nodeItems.length > 2" link type="primary" @click="openRow(row)">查看全部 {{ row.nodeItems.length }} 个节点</el-button>
                <div class="text-3">节点数 {{ factText(row.metrics.nodes) }} · 异常 {{ factText(row.metrics.bad_nodes) }}</div>
              </template></el-table-column>
              <el-table-column label="工作空间 / 引擎" min-width="190"><template #default="{ row }">
                <div class="cell-fact">{{ factText(row.status_details.workspace) }}</div><div class="text-3 cell-fact">{{ row.engine ?? '未识别引擎' }} · {{ row.job_type ?? '类型未采集' }}</div>
              </template></el-table-column>
            </template>

            <template v-else-if="panel === 'batch'">
              <el-table-column label="实例 ID / 工作空间" min-width="165"><template #default="{ row }">
                <div class="mono cell-fact">{{ factText(row.status_details.instance_id) }}</div><div class="text-3 cell-fact">{{ factText(row.status_details.workspace) }}</div>
              </template></el-table-column>
              <el-table-column label="计划时间" min-width="170"><template #default="{ row }">{{ detailText('plan_time', row.status_details.plan_time, row.resource_type) }}</template></el-table-column>
              <el-table-column label="实际开始 / 结束" min-width="195"><template #default="{ row }">
                <div>始 {{ detailText('start_time', row.status_details.start_time, row.resource_type) }}</div><div class="text-3">终 {{ detailText('end_time', row.status_details.end_time, row.resource_type) }}</div>
              </template></el-table-column>
              <el-table-column label="持续时长" min-width="100"><template #default="{ row }">{{ row.duration }}</template></el-table-column>
            </template>

            <template v-else-if="panel === 'mrs'">
              <el-table-column label="所属作业 / 工作空间" min-width="190"><template #default="{ row }">
                <div class="cell-fact">{{ factText(row.status_details.job_name) }}</div><div class="text-3 cell-fact">工作空间：{{ factText(row.status_details.workspace) }}</div>
              </template></el-table-column>
              <el-table-column label="节点 / 类型" min-width="170"><template #default="{ row }">
                <div class="cell-fact">{{ factText(row.status_details.node_name) }}</div><div class="text-3 cell-fact">{{ row.job_type ?? '类型未采集' }} · {{ row.engine ?? '未识别引擎' }}</div>
              </template></el-table-column>
              <el-table-column label="实例 / 重试" min-width="150"><template #default="{ row }">
                <div class="mono cell-fact">{{ factText(row.status_details.instance_id) }}</div><div class="text-3 cell-fact">重试 {{ factText(row.status_details.retry_times) }} 次</div>
              </template></el-table-column>
              <el-table-column label="实际执行 / 时长" min-width="195"><template #default="{ row }">
                <div>始 {{ detailText('start_time', row.status_details.start_time, row.resource_type) }}</div><div class="text-3">终 {{ detailText('end_time', row.status_details.end_time, row.resource_type) }}</div><div class="text-3">持续 {{ row.duration }}</div>
              </template></el-table-column>
            </template>

            <template v-else>
              <el-table-column :label="panel === 'dgc' ? '类型 / 工作空间' : '来源 / 引擎'" min-width="165"><template #default="{ row }">
                <div>{{ row.sourceLabel }}</div>
                <div class="text-3 cell-fact">工作空间：{{ factText(row.status_details.workspace) }}</div>
                <div class="text-3 cell-fact">{{ row.engine ?? '未识别引擎' }} · {{ row.job_type ?? '类型未采集' }}</div>
              </template></el-table-column>
              <el-table-column label="节点 / 最近实例 / 执行" min-width="230"><template #default="{ row }">
                <template v-if="row.resource_type === 'dgc_realtime_job'">
                  <div v-for="(node, index) in row.nodePreview" :key="index" class="ellipsis" :title="`${node.name}：${node.status}`">{{ node.name }}：<span class="mono">{{ node.status }}</span></div>
                  <span v-if="!row.nodeItems.length" class="text-3">未采集节点明细</span>
                  <el-button v-if="row.nodeItems.length > 2" link type="primary" @click="openRow(row)">查看全部 {{ row.nodeItems.length }} 个节点</el-button>
                </template>
                <template v-else-if="row.resource_type === 'dgc_batch_job'">
                  <div class="cell-fact">实例 <span class="mono">{{ factText(row.status_details.instance_id) }}</span></div><div class="text-3">持续 {{ row.duration }}</div>
                </template>
                <template v-else>
                  <div class="cell-fact">节点 <span class="mono">{{ factText(row.status_details.node_name) }}</span> · 实例 <span class="mono">{{ factText(row.status_details.instance_id) }}</span></div>
                  <div class="text-3">持续 {{ row.duration }}</div>
                </template>
              </template></el-table-column>
              <el-table-column :label="panel === 'dgc' ? '节点指标 / 实例时间' : '运行详情'" min-width="225"><template #default="{ row }">
                <template v-if="row.resource_type === 'dgc_realtime_job'">
                  <div>节点数 {{ factText(row.metrics.nodes) }} · 异常 {{ factText(row.metrics.bad_nodes) }}</div>
                </template>
                <template v-else-if="row.resource_type === 'dgc_batch_job'">
                  <div>计划 {{ detailText('plan_time', row.status_details.plan_time, row.resource_type) }}</div><div class="text-3">始 {{ detailText('start_time', row.status_details.start_time, row.resource_type) }}</div><div class="text-3">终 {{ detailText('end_time', row.status_details.end_time, row.resource_type) }}</div>
                </template>
                <template v-else>
                  <div class="cell-fact">{{ factText(row.status_details.job_name) }} / {{ factText(row.status_details.node_name) }}</div><div class="text-3">持续 {{ row.duration }}</div><div class="text-3">始 {{ detailText('start_time', row.status_details.start_time, row.resource_type) }}</div>
                </template>
              </template></el-table-column>
            </template>

            <el-table-column label="租户 / 项目" min-width="145"><template #default="{ row }"><TenantTag :id="row.tenant_id" :name="row.tenant_name" /><div class="text-3 cell-fact">{{ row.project_name ?? row.project_id ?? '项目未采集' }}</div></template></el-table-column>
            <el-table-column label="最后观测时间" width="170"><template #default="{ row }">{{ fmtTime(row.updated_at, 'YYYY-MM-DD HH:mm:ss') }}<div class="text-3">{{ fromNow(row.updated_at) }}</div></template></el-table-column>
            <el-table-column label="详情" width="68" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openRow(row)">查看</el-button></template></el-table-column>
            <template #empty><el-empty description="暂无匹配作业" :image-size="64" /></template>
          </el-table>
          <div class="pager"><el-pagination :current-page="query.page" :page-size="query.page_size" :total="report.total" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" background
                                               @update:current-page="setPage" @update:page-size="setPageSize" /></div>
          <el-collapse v-if="report.data_notes.length" class="report-notes">
            <el-collapse-item title="数据说明" name="source">
              <ul><li v-for="note in report.data_notes" :key="note">{{ note }}</li></ul>
            </el-collapse-item>
          </el-collapse>
        </section>
      </div>
    </template>
    <JobReportDrawer :row="selected" @close="selected = null" />
  </div>
</template>

<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { Download, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { EChartsCoreOption } from 'echarts/core'
import PageHeader from '@/components/PageHeader.vue'
import EChart from '@/components/EChart.vue'
import TenantTag from '@/components/TenantTag.vue'
import { exportJobReport, fetchJobReport } from '@/api/job-reports'
import { useAppStore } from '@/stores/app'
import type { JobPanel, JobReportRow, JobResourceType } from '@/types/job-reports'
import { fmtTime, fromNow } from '@/utils/format'
import JobReportDrawer from './JobReportDrawer.vue'
import { useJobReport } from './useJobReport'
import { PANEL_CONFIG, SOURCE_LABELS, statusMeta } from './report-config'
import { detailText, durationText, factText, jobNodes } from './report-utils'

const props = defineProps<{ panel: JobPanel }>()
const app = useAppStore()
const config = computed(() => PANEL_CONFIG[props.panel])
const { query, report, rows, loading, error, exporting, exportError,
  refresh, setFilters, reset, setPage, setPageSize, exportExcel, cancelExport } = useJobReport({
  panel: () => props.panel, tenantId: () => app.tenantId, fetcher: fetchJobReport, exporter: exportJobReport,
})
const selected = shallowRef<JobReportRow | null>(null)
type ReportTableRow = JobReportRow & {
  nodeItems: ReturnType<typeof jobNodes>
  nodePreview: ReturnType<typeof jobNodes>
  duration: string
  sourceLabel: string
}
const tableRows = computed<ReportTableRow[]>(() => rows.value.map(row => {
  const nodeItems = jobNodes(row)
  return { ...row, nodeItems, nodePreview: nodeItems.slice(0, 2), duration: durationText(row), sourceLabel: SOURCE_LABELS[row.resource_type] }
}))
watch([query, () => props.panel, () => app.tenantId], () => { selected.value = null }, { flush: 'sync' })
watch(report, value => {
  if (selected.value) selected.value = value?.items.find(row => row.id === selected.value?.id) ?? null
})

const statusOptions = computed(() => [...new Set([...config.value.statuses, ...Object.keys(report.value?.summary.status_counts ?? {}), ...(query.value.status ?? [])])])
const engineOptions = computed(() => [...new Set([...Object.keys(report.value?.summary.by_engine ?? {}).filter(key => key !== 'UNKNOWN'), ...(query.value.engine ? [query.value.engine] : [])])])
const statusEntries = computed(() => statusOptions.value.map(key => ({ key, count: report.value?.summary.status_counts[key] ?? 0 }))
  .filter(entry => entry.count > 0 || config.value.focus.includes(entry.key) || ['UNKNOWN', 'NO_DATA'].includes(entry.key) || query.value.status?.includes(entry.key)))
const SERVICE_LABELS: Record<string, string> = { dataarts: 'DataArts Studio', mrs: 'MRS' }
const DGC_TYPES: JobResourceType[] = ['dgc_realtime_job', 'dgc_batch_job']
const dimensionEntries = computed(() => props.panel === 'dgc'
  ? DGC_TYPES.map(key => ({ key, label: SOURCE_LABELS[key], count: report.value?.summary.by_type[key] ?? 0 }))
  : Object.entries(report.value?.summary.by_source ?? {}).map(([key, count]) => ({ key, label: SERVICE_LABELS[key] ?? key, count })))
const engineEntries = computed(() => Object.entries(report.value?.summary.by_engine ?? {}).map(([key, count]) => ({ key, count })))
const statusChart = computed<EChartsCoreOption>(() => ({
  tooltip: { trigger: 'item', renderMode: 'richText' },
  series: [{ type: 'pie', radius: ['56%', '78%'], center: ['50%', '50%'], label: { show: false },
    data: statusEntries.value.filter(entry => entry.count > 0).map(entry => ({
      name: statusMeta(entry.key).label, value: entry.count, itemStyle: { color: statusMeta(entry.key).color },
    })) }],
}))

// ElTableColumn does not inherit its parent's row generic. Resolve its slot value
// against the controlled page data before passing a row to the detail drawer.
function openRow(value: unknown) {
  if (value && typeof value === 'object' && 'id' in value) {
    selected.value = rows.value.find(row => row.id === value.id) ?? null
  }
}

function toggleStatus(status: string) {
  const selected = query.value.status ?? []
  setFilters({ status: selected.includes(status) ? selected.filter(value => value !== status) : [...selected, status] })
}

async function onExport() {
  if (await exportExcel()) ElMessage.success('Excel 已导出')
}

</script>

<style scoped lang="scss">
.job-report-page { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.scope-top { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; font-size: 12px; }
.scope-top strong { font-size: 13px; color: var(--hi-text-2); }
.filters .filter-bar { margin-bottom: 0; }
.filter-bar .el-input { width: 260px; }
.filter-bar .el-select { width: 240px; }
.report-content { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.kpi-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 16px; }
.kpi { min-width: 0; overflow-wrap: anywhere; display: flex; flex-direction: column; gap: 8px; }
.kpi > span { color: var(--hi-text-2); font-size: 13px; }
.kpi strong { font-size: 28px; line-height: 1.25; }
.kpi small { color: var(--hi-text-3); font-size: 12px; }
.exhibit-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(260px, 1fr); gap: 16px; }
.status-overview { display: grid; grid-template-columns: minmax(140px, 0.8fr) minmax(250px, 1.2fr); align-items: center; gap: 12px; }
.status-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 12px; }
.status-item { display: flex; align-items: center; gap: 7px; padding: 6px 0; border: 0; background: transparent; color: var(--hi-text-2); text-align: left; cursor: pointer; font: inherit; font-size: 12px; min-width: 0; }
.status-item i { flex: none; width: 7px; height: 7px; border-radius: 50%; }
.status-item span { overflow-wrap: anywhere; }
.status-item b { margin-left: auto; color: var(--hi-text-1); }
.status-item:hover, .status-item[aria-pressed='true'] { color: var(--el-color-primary); }
.status-item:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.coverage-note { border-top: 1px solid var(--hi-border-light); padding-top: 12px; margin-top: 12px; color: var(--hi-text-3); font-size: 12px; }
.dimension-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 9px 0; font-size: 13px; border-bottom: 1px solid var(--hi-border-light); }
.dimension-row span { overflow-wrap: anywhere; }
.dimension-row b { flex: none; }
.engine-list { max-height: 170px; overflow-y: auto; }
.dimension-empty { padding: 20px 0; color: var(--hi-text-3); font-size: 13px; }
.detail-section { min-width: 0; }
.report-notes { margin-top: 12px; }
.report-notes ul { margin: 0; padding-left: 18px; color: var(--hi-text-3); line-height: 1.7; overflow-wrap: anywhere; }
.job-name { max-width: 100%; height: auto; text-align: left; white-space: normal; overflow-wrap: anywhere; justify-content: flex-start; }
.cell-fact { overflow-wrap: anywhere; }
.state-tag { max-width: 100%; height: auto; min-height: 20px; white-space: normal; padding-top: 2px; padding-bottom: 2px; }
.state-tag :deep(.el-tag__content) { overflow-wrap: anywhere; }
.export-progress { display: flex; align-items: center; justify-content: space-between; gap: 16px; font-size: 13px; }
.hi-card__title { flex-wrap: wrap; }
.hi-card__title .sub { margin-left: 0; }
.table-heading > span .sub { margin-left: 8px; }
@media (max-width: 1250px) {
  .kpi-grid { gap: 10px; }
  .kpi { padding: 14px; }
  .exhibit-grid { grid-template-columns: minmax(0, 1fr); }
  .status-overview { grid-template-columns: 220px minmax(0, 1fr); }
  .status-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .status-overview { grid-template-columns: minmax(0, 1fr); }
  .status-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filter-bar .el-input, .filter-bar .el-select { width: 100%; }
  .pager { overflow-x: auto; justify-content: flex-start; }
}
</style>
