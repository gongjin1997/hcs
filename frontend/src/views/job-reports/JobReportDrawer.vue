<template>
  <el-drawer :model-value="!!row" title="作业详情" size="min(820px, 100vw)" destroy-on-close @update:model-value="value => { if (!value) emit('close') }">
    <template v-if="row">
      <div class="detail-heading">
        <div><h3>{{ row.name }}</h3><p class="mono text-3">{{ row.resource_id }}</p></div>
        <el-tag :type="statusMeta(row.display_status).type" class="state-tag">{{ statusMeta(row.display_status).label }}</el-tag>
      </div>
      <div class="detail-actions">
        <el-button v-if="user.can('alarm:view')" v-perm="'alarm:view'" :icon="Bell" @click="goAlarms">相关活动告警（{{ row.active_alarms }}）</el-button>
        <el-button v-if="user.can('ledger:view')" v-perm="'ledger:view'" :icon="Link" @click="goResource">资源详情</el-button>
      </div>

      <div class="section-title">作业信息</div>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="来源">{{ SOURCE_LABELS[row.resource_type] ?? row.resource_type }} · {{ row.service }}<div class="mono text-3">{{ row.resource_type }}</div></el-descriptions-item>
        <el-descriptions-item label="租户 / 项目">{{ row.tenant_name ?? '未采集' }} / {{ row.project_name ?? row.project_id ?? '未采集' }}</el-descriptions-item>
        <el-descriptions-item label="项目 ID">{{ row.project_id ?? '未采集' }}</el-descriptions-item>
        <el-descriptions-item v-if="row.resource_type !== 'mrs_job'" label="工作空间">{{ factText(row.status_details.workspace) }}</el-descriptions-item>
        <el-descriptions-item label="引擎 / 作业类型">{{ row.engine ?? '未识别' }} / {{ row.job_type ?? '未采集' }}</el-descriptions-item>
        <el-descriptions-item label="区域">{{ row.region ?? '未采集' }}</el-descriptions-item>
        <el-descriptions-item label="生命周期">{{ row.lifecycle }}</el-descriptions-item>
      </el-descriptions>

      <div class="section-title">执行状态</div>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="作业状态"><span class="mono">{{ row.display_status }}</span> · {{ row.status_label }}</el-descriptions-item>
        <el-descriptions-item label="原始状态"><span class="mono">{{ factText(row.status_raw) }}</span></el-descriptions-item>
        <el-descriptions-item :label="row.resource_type === 'dgc_batch_job' ? '实例执行状态' : '执行状态'"><span class="mono">{{ factText(row.execution_state) }}</span><div class="text-3 mono">{{ stateField }}</div></el-descriptions-item>
        <el-descriptions-item v-if="row.resource_type === 'mrs_job' || row.execution_result !== null" label="执行结果"><span class="mono">{{ factText(row.execution_result) }}</span></el-descriptions-item>
        <el-descriptions-item label="最后观测时间">{{ fmtTime(row.updated_at, 'YYYY-MM-DD HH:mm:ss') }}<div class="text-3">{{ fromNow(row.updated_at) }}</div></el-descriptions-item>
        <el-descriptions-item label="状态变化观测时间">{{ fmtTime(row.status_since, 'YYYY-MM-DD HH:mm:ss') }}</el-descriptions-item>
        <el-descriptions-item label="巡检健康度">{{ row.health }}</el-descriptions-item>
      </el-descriptions>

      <div class="section-title">{{ sourceConfig?.title }}</div>
      <el-descriptions :column="1" border>
        <el-descriptions-item v-for="detail in sourceFacts" :key="detail.key" :label="detail.label">
          <span class="fact-value">{{ detailText(detail.key, detail.value, row.resource_type) }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="row.resource_type !== 'dgc_realtime_job' || row.metrics.run_minutes != null" label="持续时长">{{ durationText(row) }}</el-descriptions-item>
      </el-descriptions>

      <template v-if="row.resource_type === 'dgc_realtime_job' || nodes.length">
        <div class="section-title">节点状态<span class="sub">{{ nodes.length ? `${nodes.length} 个节点` : '节点明细未采集' }}</span></div>
        <el-table :data="nodes" size="small" border>
          <el-table-column prop="name" label="节点" min-width="180" show-overflow-tooltip />
          <el-table-column prop="status" label="原始状态" min-width="140" show-overflow-tooltip />
          <template #empty><el-empty description="暂无节点数据" :image-size="48" /></template>
        </el-table>
      </template>

      <div class="section-title">运行指标</div>
      <el-table :data="metrics" size="small" border>
        <el-table-column prop="label" label="指标" min-width="180" />
        <el-table-column prop="key" label="来源字段" min-width="150" show-overflow-tooltip />
        <el-table-column label="值" min-width="170"><template #default="{ row: metric }"><span class="fact-value">{{ factText(metric.value) }}</span></template></el-table-column>
        <template #empty><el-empty description="暂无指标" :image-size="48" /></template>
      </el-table>

      <el-collapse class="source-details">
        <el-collapse-item title="原始字段" name="source">
          <el-descriptions v-if="details.length" :column="1" border>
            <el-descriptions-item v-for="detail in details" :key="detail.key" :label="detail.label">
              <div class="mono text-3">{{ detail.key }}</div><span class="fact-value">{{ factText(detail.value) }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="暂无数据" :image-size="48" />
        </el-collapse-item>
      </el-collapse>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Bell, Link } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import type { JobReportRow } from '@/types/job-reports'
import { fmtTime, fromNow } from '@/utils/format'
import { DETAIL_LABELS, METRIC_LABELS, SOURCE_FACTS, SOURCE_LABELS, statusMeta } from './report-config'
import { detailText, durationText, factText, jobNodes } from './report-utils'

const props = defineProps<{ row: JobReportRow | null }>()
const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const user = useUserStore()
type MetricEntry = { key: string; label: string; value: unknown }
const sourceConfig = computed(() => props.row ? SOURCE_FACTS[props.row.resource_type] : null)
const sourceFacts = computed(() => (sourceConfig.value?.fields ?? []).map(key => ({
  key, label: DETAIL_LABELS[key] ?? key, value: props.row?.status_details[key],
})))
const stateField = computed(() => props.row ? {
  dgc_realtime_job: 'job_status', dgc_batch_job: 'instance_status', mrs_job: 'job_state',
}[props.row.resource_type] : '')
const nodes = computed(() => props.row ? jobNodes(props.row) : [])
const metrics = computed<MetricEntry[]>(() => Object.entries(props.row?.metrics ?? {}).map(([key, value]) => ({ key, label: METRIC_LABELS[key] ?? key, value })))
const details = computed(() => Object.entries(props.row?.status_details ?? {}).filter(([key]) => key !== 'nodes')
  .map(([key, value]) => ({ key, label: DETAIL_LABELS[key] ?? key, value })))

function goAlarms() {
  if (!props.row || !user.can('alarm:view')) return
  void router.push({ path: '/alarms', query: { resource_ref: props.row.id, status_group: 'active' } })
}
function goResource() {
  if (!props.row || !user.can('ledger:view')) return
  void router.push({ path: '/ledger', query: { id: props.row.id, type: props.row.resource_type } })
}
</script>

<style scoped lang="scss">
.detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.detail-heading h3 { margin: 0; font-size: 18px; overflow-wrap: anywhere; }
.detail-heading p { margin: 8px 0 0; overflow-wrap: anywhere; }
.state-tag { flex: none; max-width: 45%; height: auto; min-height: 24px; white-space: normal; padding-top: 3px; padding-bottom: 3px; }
.state-tag :deep(.el-tag__content) { overflow-wrap: anywhere; }
.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.detail-actions .el-button + .el-button { margin-left: 0; }
.source-details { margin-top: 20px; }
.section-title { flex-wrap: wrap; }
.fact-value { white-space: pre-wrap; overflow-wrap: anywhere; }
:deep(.el-descriptions__content) { overflow-wrap: anywhere; }
:deep(.el-descriptions__label) { width: 150px; }
@media (max-width: 600px) { .detail-heading { flex-wrap: wrap; } .state-tag { max-width: 100%; } :deep(.el-descriptions__label) { width: 100px; } }
</style>
