<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Download } from '@element-plus/icons-vue'
import EChart from '@/components/EChart.vue'
import { gaussDiskApi } from '@/api/gaussdb'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { GaussInstance } from '@/types/gaussdb'
import { fmtTime } from '@/utils/format'
import { gaussDiskChart } from './gauss-chart'
import { useGaussDiskHistory } from './useGaussDiskHistory'

const props = defineProps<{ instance: GaussInstance }>()
const app = useAppStore(), user = useUserStore()
const model = useGaussDiskHistory({ instance: () => props.instance, tenantId: () => app.tenantId,
  canView: () => user.can('ledger:view'), canExport: () => user.can('ledger:export'), api: gaussDiskApi })
const { range, preset, period, history, applied, error, exportError, loading, exporting, canExport } = model
const mode = shallowRef<'usage' | 'capacity'>('usage')
const validPoints = computed(() => history.value?.points.filter(point => point.observed_at !== null).length ?? 0)
const option = computed(() => history.value ? gaussDiskChart(history.value, mode.value) : {})
defineExpose({ refresh: model.query })
</script>

<template>
  <section class="storage-history" aria-label="存储历史趋势">
    <div class="table-heading history-heading">
      <div><h3>存储趋势</h3><span class="text-3">本地定时采样 · 按响应接收时间</span></div>
      <div class="table-actions">
        <el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="model.exportExcel">导出趋势 Excel</el-button>
        <el-button v-if="exporting" @click="model.cancelExport">取消导出</el-button>
      </div>
    </div>
    <div class="history-presets">
      <el-radio-group :model-value="preset ?? undefined" aria-label="存储趋势时间" @update:model-value="value => model.setPreset(Number(value))">
        <el-radio-button :value="1">近 1 小时</el-radio-button><el-radio-button :value="6">近 6 小时</el-radio-button>
        <el-radio-button :value="24">近 24 小时</el-radio-button><el-radio-button :value="168">近 7 天</el-radio-button>
      </el-radio-group>
      <el-radio-group v-model="mode" aria-label="存储趋势指标"><el-radio-button value="usage">使用率</el-radio-button><el-radio-button value="capacity">容量</el-radio-button></el-radio-group>
    </div>
    <form class="history-filters" @submit.prevent="model.query">
      <el-date-picker :model-value="range" type="datetimerange" aria-label="存储趋势时间范围" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY-MM-DD HH:mm" @update:model-value="model.setRange" />
      <el-select v-model="period" aria-label="存储采样间隔"><el-option v-for="item in [{ value: 60, label: '1 分钟' }, { value: 300, label: '5 分钟' }, { value: 900, label: '15 分钟' }, { value: 3600, label: '1 小时' }]" :key="item.value" :value="item.value" :label="item.label" /></el-select>
      <el-button native-type="submit" :loading="loading">查询趋势</el-button>
    </form>
    <el-alert v-if="error || exportError" :title="error || exportError" type="error" :closable="false" show-icon />
    <el-skeleton v-if="loading" :rows="5" animated />
    <template v-else-if="history && validPoints">
      <EChart :option="option" height="280px" />
      <div class="history-caption text-3">{{ fmtTime(applied?.start_at, 'MM-DD HH:mm') }} — {{ fmtTime(applied?.end_at, 'MM-DD HH:mm') }} · {{ validPoints }}/{{ history.points.length }} 个时间桶有采样 · 每桶取最后一次采样，缺失处断线</div>
    </template>
    <el-empty v-else :description="history ? '所选时段暂无存储采样' : error ? '存储趋势查询失败' : '请选择时段查询'" :image-size="52" />
  </section>
</template>

<style scoped>
.storage-history { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--hi-border-light); min-width: 0; }
.history-heading { margin-bottom: 14px; }
.history-heading > div:first-child { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.history-heading h3 { margin: 0; font-size: 15px; }
.history-heading .text-3, .history-caption { font-size: 12px; }
.history-presets { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.history-filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }
.history-filters > .el-date-editor { flex: 1; min-width: 280px; max-width: 450px; }
.history-filters > .el-select { width: 110px; }
.history-caption { overflow-wrap: anywhere; }
@media (max-width: 650px) {
  .history-presets .el-radio-group { max-width: 100%; display: flex; flex-wrap: wrap; }
  .history-filters > .el-date-editor { flex-basis: 100%; width: 100%; min-width: 0; max-width: 100%; }
}
</style>
