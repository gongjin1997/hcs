<script setup lang="ts">
import { computed } from 'vue'
import type { GaussNativeRow, GaussNativeRows } from '@/types/gaussdb'
import { inspectionTime, isNativeTimeKey, nativeColumnLabel, nativeColumns, nativeText, nativeTimeTitle } from './gauss-utils'
import GaussNativeStatus from './GaussNativeStatus.vue'

const props = defineProps<{ result: GaussNativeRows; kind: 'slow-sql' | 'sessions' | 'inspections'; busy?: boolean; canRun?: boolean }>()
const emit = defineEmits<{ detail: [row: GaussNativeRow] }>()
const allColumns = computed(() => nativeColumns(props.result.items))
const primary: Record<typeof props.kind, string[]> = {
  'slow-sql': ['sql_id', 'sql_text', 'calls', 'avg_exec_time', 'avg_cpu_time', 'avg_io_time', 'start_time', 'finish_time'],
  sessions: ['session_id', 'database_name', 'user_name', 'state', 'wait', 'exec_time', 'query_runtime', 'query'],
  inspections: ['task_name', 'status', 'start_time', 'end_time', 'total_count', 'normal_count', 'abnormal_count', 'failed_count'],
}
const columns = computed(() => {
  const keys = primary[props.kind].filter(key => allColumns.value.includes(key))
  return keys.length ? keys : allColumns.value.length ? allColumns.value : primary[props.kind]
})
const extraColumns = computed(() => allColumns.value.filter(key => !columns.value.includes(key)))
const cellText = (row: GaussNativeRow, key: string) => isNativeTimeKey(key) ? inspectionTime(row[key]) : nativeText(row[key])
const columnLabel = (key: string) => nativeColumnLabel(key, props.result.units, props.result.items.map(row => row[key]))
const cellTitle = (row: GaussNativeRow, key: string) => isNativeTimeKey(key) ? nativeTimeTitle(row[key]) : nativeText(row[key])
const columnWidth = (key: string) => ({ session_id: 110, database_name: 130, user_name: 140, state: 100, wait: 70,
  exec_time: 150, query_runtime: 190, calls: 100, sql_id: 180, sql_text: 260, query: 260 }[key] ?? 160)
</script>

<template>
  <div class="native-result">
    <GaussNativeStatus :status="result.status" :warning="result.warning" :queried-at="result.queried_at" />
    <div class="result-count text-3">
      本次返回 {{ result.items.length }} 条<span v-if="result.total != null">，总数 {{ result.total }}</span><span v-else>；源端未提供总数</span><span v-if="result.truncated">；结果已截断，请缩小查询范围</span>
    </div>
    <el-table :data="result.items" size="small" stripe>
      <el-table-column v-if="extraColumns.length" type="expand" width="40">
        <template #default="{ row }"><dl class="extra-fields"><template v-for="key in extraColumns" :key="key"><dt>{{ columnLabel(key) }}</dt><dd>{{ cellText(row, key) }}</dd></template></dl></template>
      </el-table-column>
      <el-table-column v-for="key in columns" :key="key" :label="columnLabel(key)" :min-width="columnWidth(key)">
        <template #default="{ row }"><span class="native-cell" :title="cellTitle(row, key)">{{ cellText(row, key) }}</span></template>
      </el-table-column>
      <el-table-column v-if="kind !== 'sessions'" label="详情" width="90" fixed="right">
        <template #default="{ row }"><el-button link type="primary" :disabled="!canRun || busy || (kind === 'inspections' && row.status === 'running') || !(kind === 'inspections' ? row.record_id : row.sql_id)" @click="emit('detail', row)">展开</el-button></template>
      </el-table-column>
      <template #empty><el-empty description="本次查询暂无数据" :image-size="48" /></template>
    </el-table>
  </div>
</template>

<style scoped>
.result-count { margin-bottom: 10px; }
.native-cell { display: block; max-height: 100px; white-space: pre-wrap; overflow: auto; overflow-wrap: anywhere; }
.extra-fields { display: grid; grid-template-columns: minmax(110px, 160px) minmax(0, 1fr); gap: 8px 20px; padding: 8px 24px; font-size: 13px; }
.extra-fields dt { color: var(--hi-text-3); overflow-wrap: anywhere; }
.extra-fields dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 650px) { .extra-fields { grid-template-columns: 100px minmax(0, 1fr); gap: 8px; padding: 8px 12px; } }
</style>
