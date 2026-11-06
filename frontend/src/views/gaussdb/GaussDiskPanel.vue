<script setup lang="ts">
import { computed } from 'vue'
import type { GaussDisk, GaussOverviewOut } from '@/types/gaussdb'
import { fmtTime } from '@/utils/format'
import { quantityText } from './gauss-utils'
import GaussNativeStatus from './GaussNativeStatus.vue'

/** 实时查询结果传 overview；最近一次巡检采集（本地）传 disk。 */
const props = defineProps<{ overview?: GaussOverviewOut | null; disk?: GaussDisk | null }>()
const current = computed(() => props.disk ?? props.overview?.disk ?? null)
</script>

<template>
  <div v-if="current" class="disk-panel">
    <GaussNativeStatus :status="current.status" :warning="current.warning" :queried-at="overview?.queried_at ?? null" />
    <div class="disk-stats">
      <div><span class="text-3">已用存储</span><strong class="num">{{ quantityText(current.used_gb, 'GB') }}</strong></div>
      <div><span class="text-3">总容量</span><strong class="num">{{ quantityText(current.total_gb, 'GB') }}</strong></div>
      <div><span class="text-3">存储使用率</span><strong class="num">{{ quantityText(current.usage_percent, '%') }}</strong></div>
    </div>
    <p v-if="current.time_basis === 'RESPONSE_RECEIVED'" class="text-3">获取时间：{{ fmtTime(current.collected_at, 'YYYY-MM-DD HH:mm:ss') }}（本地接收）</p>
    <p v-else class="text-3">源端采样时间：{{ current.observed_at ? fmtTime(current.observed_at, 'YYYY-MM-DD HH:mm:ss') : '源端未提供' }}</p>
  </div>
</template>

<style scoped>
.disk-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.disk-stats > div { padding: 12px 0; }
.disk-stats strong { display: block; margin-top: 8px; font-size: 22px; overflow-wrap: anywhere; }
@media (max-width: 650px) { .disk-stats { gap: 8px; } .disk-stats strong { font-size: 18px; } }
</style>
