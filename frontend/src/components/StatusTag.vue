<template>
  <el-tag :type="meta.type" size="small" effect="light" disable-transitions>{{ meta.label }}</el-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ALARM_STATUS } from '@/utils/dict'

const props = defineProps<{ status: string }>()
const meta = computed(() => ALARM_STATUS[props.status] ?? { label: props.status, type: 'info' as const })
</script>
