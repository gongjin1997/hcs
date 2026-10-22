<template>
  <span class="sev-tag" :class="{ solid }" :style="style">
    <i class="dot" />{{ level }}<template v-if="!short">&nbsp;{{ meta.label }}</template>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { SEVERITY } from '@/utils/dict'

const props = defineProps<{ level: string; short?: boolean; solid?: boolean }>()
const meta = computed(() => SEVERITY[props.level] ?? { label: '-', color: '#8C8C8C', bg: '#F2F3F5' })
const style = computed(() => ({ '--c': meta.value.color, '--bg': meta.value.bg }))
</script>

<style scoped lang="scss">
.sev-tag {
  display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 11px;
  font-size: 12px; font-weight: 600; line-height: 1; white-space: nowrap;
  color: var(--c); background: var(--bg); border: 1px solid color-mix(in srgb, var(--c) 25%, transparent);
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--c); margin-right: 5px; }
  &.solid { color: #fff; background: var(--c); .dot { background: #fff; } }
}
</style>
