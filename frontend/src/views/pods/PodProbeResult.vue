<template>
  <template v-if="result?.at">
    <div v-if="result.max != null" class="sm">
      <b>{{ result.hit }}</b> / {{ result.pods }} 个目标 Pod 已确认超阈值
      <span class="text-3"> · 最大 {{ result.max }}{{ unit }}</span>
    </div>
    <el-tag v-if="!result.pods || result.max == null || result.errors?.length" type="warning" size="small" effect="plain">
      {{ !result.pods ? '未匹配到目标，状态未知' : '采样不完整，状态未知' }}
    </el-tag>
    <div class="text-3 sm">{{ fromNow(result.at) }}</div>
    <el-tooltip v-if="result.errors?.length" :content="result.errors.join('；')" placement="top">
      <el-tag type="danger" size="small" effect="plain">{{ result.errors.length }} 个错误</el-tag>
    </el-tooltip>
  </template>
  <span v-else class="text-3">{{ enabled ? '等待下一轮采集' : '未执行（已停用）' }}</span>
</template>

<script setup lang="ts">
import { fromNow } from '@/utils/format'

defineProps<{
  result?: { at?: string; pods: number; hit: number; max: number | null; errors: string[] } | null
  unit?: string
  enabled: boolean
}>()
</script>
