<template>
  <span class="health" :style="{ color: meta.color }"><i :style="{ background: meta.color }" />{{ meta.label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { HEALTH } from '@/utils/dict'

const props = defineProps<{ value: string }>()
const meta = computed(() => HEALTH[props.value] ?? HEALTH.UNKNOWN)
</script>

<style scoped>
.health { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; }
.health i { width: 8px; height: 8px; border-radius: 50%; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 15%, transparent); }
</style>
