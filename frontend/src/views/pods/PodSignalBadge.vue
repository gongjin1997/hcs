<template>
  <el-tag v-if="label" :type="type" size="small" effect="plain">{{ label }}</el-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ event: { ongoing: boolean; detail?: Record<string, unknown> | null } }>()
const labels: Record<string, string> = {
  POD_DELETED: '目标已删除，非恢复', POD_REPLACED: 'Pod 已替换，非恢复',
  EVENT_EXPIRED: '事件已过期，恢复未知', CONTAINER_TERMINATED: '容器已终止，非恢复',
  PROBE_CHANGED: '探针已变更，非恢复',
}
const label = computed(() => {
  const detail = props.event.detail ?? {}
  if (props.event.ongoing) return detail.signal_status === 'UNKNOWN' ? '采样中断，状态未知' : '进行中'
  if (detail.recovery_confirmed === true) return '已恢复'
  return typeof detail.closure_reason === 'string' ? labels[detail.closure_reason] ?? '已结束，恢复未确认' : ''
})
const type = computed(() => props.event.ongoing && props.event.detail?.signal_status !== 'UNKNOWN' ? 'danger'
  : props.event.detail?.recovery_confirmed === true ? 'success' : 'warning')
</script>
