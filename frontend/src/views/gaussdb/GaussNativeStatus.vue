<script setup lang="ts">
import type { GaussNativeStatus } from '@/types/gaussdb'
import { NATIVE_STATUS } from './gauss-utils'

defineProps<{ status: GaussNativeStatus; warning?: string | null; queriedAt?: string | null }>()
</script>

<template>
  <div class="native-state" role="status">
    <el-tag size="small" :type="status === 'AVAILABLE' ? 'success' : status === 'ERROR' ? 'danger' : 'warning'" effect="plain">{{ NATIVE_STATUS[status] }}</el-tag>
    <span class="text-3">GaussDB 原生接口</span>
    <span v-if="queriedAt" class="text-3">查询时间：{{ new Date(queriedAt).toLocaleString('zh-CN', { hour12: false }) }}</span>
    <span v-if="warning" class="native-warning">{{ warning }}</span>
  </div>
</template>

<style scoped>
.native-state { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px; }
.native-warning { flex-basis: 100%; color: var(--hi-text-2); overflow-wrap: anywhere; font-size: 13px; }
</style>
