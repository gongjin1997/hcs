<script setup lang="ts">
import { onMounted, onScopeDispose, shallowRef } from 'vue'
import type { DcsInstance, DcsMetricKey } from '@/types/dcs'
import { rawStatusType, statusText } from '@/utils/dict'
import { fmtTime } from '@/utils/format'
import { freshnessState, metricText, topologyHealth, topologyText } from './dcs-utils'

const props = defineProps<{ instances: DcsInstance[]; loading: boolean; selectedIds?: number[] }>()
const emit = defineEmits<{ select: [id: number] }>()
const now = shallowRef(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 15000) })
onScopeDispose(() => { clearInterval(timer) })
const stateLabels = { FRESH: '新鲜', STALE: '已过期', NO_DATA: '未采样', LEGACY: '旧监控快照' }
const columns: { key: DcsMetricKey; label: string; width: number }[] = [
  { key: 'memory_usage', label: '内存使用率 %', width: 130 },
  { key: 'used_memory_mb', label: '已用内存 MB', width: 130 },
  { key: 'max_memory_mb', label: '内存上限 MB', width: 130 },
  { key: 'used_cpu_seconds', label: 'CPU累计时间 s', width: 150 },
  { key: 'input_kbps', label: '网络入 kbit/s', width: 140 },
  { key: 'output_kbps', label: '网络出 kbit/s', width: 140 },
  { key: 'keys', label: '数据条数', width: 120 },
  { key: 'cmd_get_count', label: 'GET累计次数', width: 135 },
  { key: 'cmd_set_count', label: 'SET累计次数', width: 135 },
]
// 范围外的实例已不再采集，页面上的数值可能是很久以前的，一律不显示
const currentText = (instance: DcsInstance, key: DcsMetricKey | 'cpu_usage') =>
  !instance.scope_excluded && freshnessState(instance, now.value) === 'FRESH' ? metricText(instance[key]) : '—'
const rowClass = ({ row }: { row: DcsInstance }) => (props.selectedIds?.includes(row.id) ? 'picked' : '')
</script>

<template>
  <section class="hi-card current-statistics" aria-label="当前原生统计">
    <div class="hi-card__title">当前原生统计<span class="sub">源端未提供采样时间；新鲜度按本地获取时间计算 · 点击一行查看该实例的分片节点与趋势</span></div>
    <el-table v-loading="props.loading" :data="props.instances" row-key="id" size="small" stripe :row-class-name="rowClass"
              @row-click="(row: DcsInstance) => emit('select', row.id)">
      <el-table-column label="实例" min-width="205" fixed><template #default="{ row }">
        <strong>{{ row.name }}</strong>
        <el-tag v-if="row.status_raw" size="small" :type="rawStatusType(row.status_raw)" effect="plain" class="gap">{{ statusText('dcs_instance', row.status_raw) }}</el-tag>
        <el-tag v-if="row.scope_excluded" size="small" type="info" effect="plain" class="gap" title="被采集范围规则排除，不再采集">范围外</el-tag>
        <div class="text-3">{{ row.tenant_name }} · {{ row.project_name ?? row.resource_id }}</div>
      </template></el-table-column>
      <el-table-column label="分片 / 节点" min-width="200"><template #default="{ row }">
        <el-tag size="small" :type="topologyHealth(row.topology).type" effect="plain">{{ topologyHealth(row.topology).text }}</el-tag>
        <div class="text-3">{{ topologyText(row.topology) }}</div>
      </template></el-table-column>
      <el-table-column label="新鲜度 / 获取时间" min-width="230"><template #default="{ row }">
        <el-tag :type="freshnessState(row as DcsInstance, now) === 'FRESH' ? 'success' : 'info'" size="small" effect="plain">{{ stateLabels[freshnessState(row as DcsInstance, now)] }}</el-tag>
        <div>{{ fmtTime(row.metrics_observed_at, 'YYYY-MM-DD HH:mm:ss') }}</div>
        <div v-if="row.warning" class="text-3 current-warning">{{ row.warning }}</div>
      </template></el-table-column>
      <el-table-column v-for="column in columns" :key="column.key" :label="column.label" :min-width="column.width" align="right">
        <template #default="{ row }">{{ currentText(row as DcsInstance, column.key) }}</template>
      </el-table-column>
      <el-table-column min-width="150" align="right">
        <template #header><el-tooltip content="由两次累计 CPU 秒折算：Δ秒 ÷ Δ时间 ÷ 主节点数。首次采样、实例重启后暂无；近似反映 Redis 主线程饱和度" placement="top"><span>CPU 折算 %</span></el-tooltip></template>
        <template #default="{ row }">{{ currentText(row as DcsInstance, 'cpu_usage') }}</template>
      </el-table-column>
      <el-table-column label="来源接口" min-width="185"><template #default="{ row }">
        {{ row.metrics_source === 'DCS' ? `DCS V2 · ${row.source_endpoint ?? '—'}` : '—' }}
      </template></el-table-column>
      <template #empty><el-empty description="当前范围暂无 DCS 实例" :image-size="48" /></template>
    </el-table>
  </section>
</template>

<style scoped>
.current-statistics { min-width: 0; }
.gap { margin-left: 6px; }
.current-statistics :deep(.el-table__row) { cursor: pointer; }
.current-statistics :deep(.picked td) { background: var(--el-color-primary-light-9) !important; }
.hi-card__title { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.current-warning { white-space: normal; overflow-wrap: anywhere; }
</style>
