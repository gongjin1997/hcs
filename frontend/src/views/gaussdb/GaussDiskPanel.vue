<script setup lang="ts">
import type { GaussOverviewOut } from '@/types/gaussdb'
import { fmtTime } from '@/utils/format'
import { quantityText } from './gauss-utils'
import GaussNativeStatus from './GaussNativeStatus.vue'

defineProps<{ overview: GaussOverviewOut }>()
</script>

<template>
  <div class="disk-panel">
    <GaussNativeStatus :status="overview.disk.status" :warning="overview.disk.warning" :queried-at="overview.queried_at" />
    <div class="disk-stats">
      <div><span class="text-3">已用存储</span><strong class="num">{{ quantityText(overview.disk.used_gb, 'GB') }}</strong></div>
      <div><span class="text-3">总容量</span><strong class="num">{{ quantityText(overview.disk.total_gb, 'GB') }}</strong></div>
      <div><span class="text-3">存储使用率</span><strong class="num">{{ quantityText(overview.disk.usage_percent, '%') }}</strong></div>
    </div>
    <p v-if="overview.disk.time_basis === 'RESPONSE_RECEIVED'" class="text-3">获取时间：{{ fmtTime(overview.disk.collected_at, 'YYYY-MM-DD HH:mm:ss') }}（本地接收）</p>
    <p v-else class="text-3">源端采样时间：{{ overview.disk.observed_at ? fmtTime(overview.disk.observed_at, 'YYYY-MM-DD HH:mm:ss') : '源端未提供' }}</p>

  </div>
</template>

<style scoped>
.disk-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.disk-stats > div { padding: 12px 0; }
.disk-stats strong { display: block; margin-top: 8px; font-size: 22px; overflow-wrap: anywhere; }
@media (max-width: 650px) { .disk-stats { gap: 8px; } .disk-stats strong { font-size: 18px; } }
</style>
