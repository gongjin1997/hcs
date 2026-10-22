<template>
  <span v-if="label" class="tenant-tag" :class="{ plain }" :style="{ '--tc': color }" :title="full">
    <i />{{ label }}
  </span>
  <span v-else class="text-3">-</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useAppStore } from '@/stores/app'

/** 租户标签：色点 + 简称（颜色、简称取自当前用户可见的租户列表；也可直接传 name / color） */
const props = defineProps<{ id?: number | null; name?: string | null; color?: string | null; plain?: boolean }>()
const app = useAppStore()
const t = computed(() => app.tenantOf(props.id))
const label = computed(() => t.value?.short_name ?? props.name ?? '')
const full = computed(() => t.value?.name ?? props.name ?? '')
const color = computed(() => props.color ?? t.value?.color ?? '#8c8c8c')
</script>

<style scoped lang="scss">
.tenant-tag { display: inline-flex; align-items: center; gap: 5px; height: 20px; padding: 0 7px; border-radius: 10px; font-size: 12px; line-height: 1;
  white-space: nowrap; color: var(--tc); background: color-mix(in srgb, var(--tc) 9%, #fff); border: 1px solid color-mix(in srgb, var(--tc) 22%, #fff);
  i { width: 6px; height: 6px; border-radius: 50%; background: var(--tc); flex: none; }
  &.plain { background: none; border: none; padding: 0; color: var(--hi-text-2); } }
</style>
