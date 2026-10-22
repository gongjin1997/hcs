<template>
  <div class="changes">
    <div class="filter-bar mb-12">
      <el-segmented v-model="query.days" :options="DAYS" @change="search" />
      <el-select v-model="query.change_type" placeholder="变更类型" clearable style="width: 120px" @change="search">
        <el-option v-for="(v, k) in CHANGE_TYPE" :key="k" :label="v.label" :value="k" />
      </el-select>
      <el-select v-model="query.source" placeholder="来源" clearable style="width: 120px" @change="search">
        <el-option v-for="(v, k) in CHANGE_SOURCE" :key="k" :label="v" :value="k" />
      </el-select>
      <el-input v-model="query.keyword" placeholder="资源名称 / 资源 ID" clearable :prefix-icon="Search" style="width: 220px" @keyup.enter="search" @clear="search" />
      <span class="flex-1" />
      <span class="text-3">共 {{ total }} 条 · 台账变更每小时同步发现；ECS 状态变更 / 重启由巡检实时发现</span>
    </div>
    <el-table v-loading="loading" :data="rows" row-key="id" @row-click="(c: ResourceChange) => $emit('open', c.resource_ref)">
      <el-table-column label="发现时间" width="150"><template #default="{ row }">{{ fmtTime(row.detected_at, 'MM-DD HH:mm') }}<div class="text-3">{{ fromNow(row.detected_at) }}</div></template></el-table-column>
      <el-table-column label="类型" width="90"><template #default="{ row }"><el-tag :type="CHANGE_TYPE[row.change_type]?.type ?? 'info'" size="small" effect="light">{{ CHANGE_TYPE[row.change_type]?.label ?? row.change_type }}</el-tag></template></el-table-column>
      <el-table-column label="资源" min-width="200">
        <template #default="{ row }"><b class="rname">{{ row.name }}</b><div class="flex gap-6 text-3"><TenantTag v-if="app.multiTenant" :id="row.tenant_id" />{{ typeName(row.resource_type) }}</div></template>
      </el-table-column>
      <el-table-column label="变更内容" min-width="320">
        <template #default="{ row }">
          <div v-if="row.changes.length" class="fields">
            <span v-for="f in row.changes" :key="f.field" class="field"><b>{{ f.label }}</b> {{ val(row.resource_type, f.field, f.old) }} <i>→</i> <em>{{ val(row.resource_type, f.field, f.new) }}</em></span>
          </div>
          <span v-else class="text-3">{{ row.change_type === 'CREATED' ? '新资源纳入台账' : '已在 HCS 中释放' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="100"><template #default="{ row }"><span :class="row.source === 'MANUAL' ? 'manual' : 'text-2'">{{ CHANGE_SOURCE[row.source] ?? row.source }}</span></template></el-table-column>
      <el-table-column label="操作人" width="96"><template #default="{ row }">{{ row.operator === 'system' ? (row.source === 'INSPECT' ? '巡检发现' : '自动同步') : row.operator }}</template></el-table-column>
      <template #empty><el-empty description="所选时间范围内无变更" :image-size="70" /></template>
    </el-table>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100]" background layout="total, sizes, prev, pager, next" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import { onMounted, watch } from 'vue'
import { type ChangeQuery, listChanges } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import { useTable } from '@/composables/useTable'
import { useAppStore } from '@/stores/app'
import type { ResourceChange } from '@/types'
import { CHANGE_SOURCE, CHANGE_TYPE, statusText, typeName } from '@/utils/dict'
import { fmtTime, fromNow } from '@/utils/format'
import { fmtValue } from './columns'

const props = defineProps<{ resourceType?: string; category?: string; initialType?: string }>()
defineEmits<{ open: [id: number] }>()
const DAYS = [{ label: '近 7 天', value: 7 }, { label: '近 30 天', value: 30 }, { label: '近 90 天', value: 90 }]
const app = useAppStore()
/** 状态类字段按资源类型翻译，重启时间按本地时间显示 */
function val(type: string, field: string, v: unknown) {
  if (v == null || v === '') return '-'
  if (field === 'status_raw') return statusText(type, String(v))
  if (field === 'boot_time') return fmtTime(String(v), 'MM-DD HH:mm')
  return fmtValue(field, v)
}

const { query, page, size, total, rows, loading, search } = useTable<ResourceChange, ChangeQuery>(
  (q) => listChanges({ ...q, change_type: q.change_type || undefined, source: q.source || undefined,
    resource_type: props.resourceType, category: props.category }),
  { days: 30, change_type: (props.initialType as ChangeQuery['change_type']) || '', source: '', keyword: '' })
watch(() => [props.resourceType, props.category], search)
onMounted(search)
defineExpose({ reload: search })
</script>

<style scoped lang="scss">
.rname { font-weight: 500; }
.fields { display: flex; flex-wrap: wrap; gap: 4px 14px;
  .field { font-size: 12.5px; color: var(--hi-text-3); b { color: var(--hi-text-2); font-weight: 500; } i { font-style: normal; margin: 0 2px; } em { font-style: normal; color: var(--hi-text-1); font-weight: 500; } } }
.manual { color: var(--el-color-primary); }
.gap-6 { gap: 6px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
