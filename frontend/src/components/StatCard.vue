<template>
  <div class="stat-card" :style="{ '--accent': color }" @click="$emit('click')">
    <div class="icon"><el-icon :size="22"><component :is="icon" /></el-icon></div>
    <div class="body">
      <div class="title">{{ title }}</div>
      <div class="value num">{{ value }}<small v-if="unit">{{ unit }}</small></div>
      <div class="sub"><slot name="sub">{{ sub }}</slot></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'

withDefaults(defineProps<{ title: string; value: string | number; unit?: string; sub?: string; icon: Component; color?: string }>(), {
  color: '#1d63f0', unit: '', sub: '',
})
defineEmits<{ click: [] }>()
</script>

<style scoped lang="scss">
.stat-card {
  display: flex; gap: 14px; align-items: flex-start; height: 100%; padding: 16px 18px; cursor: pointer;
  background: #fff; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); box-shadow: var(--hi-shadow);
  transition: box-shadow 0.2s, border-color 0.2s;
  &:hover { box-shadow: var(--hi-shadow-hover); border-color: color-mix(in srgb, var(--accent) 35%, var(--hi-border)); }
  .icon {
    flex: none; width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center;
    color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, #fff);
  }
  .body { min-width: 0; }
  .title { color: var(--hi-text-2); font-size: 13px; }
  .value { margin-top: 2px; font-size: 26px; font-weight: 700; line-height: 1.2; color: var(--hi-text-1);
    small { font-size: 13px; font-weight: 500; color: var(--hi-text-3); margin-left: 4px; } }
  .sub { margin-top: 6px; font-size: 12px; color: var(--hi-text-3); }
}
@media (max-width: 767px) {
  .stat-card { padding: 14px 12px; gap: 10px; }
  .stat-card .icon { width: 32px; height: 32px; }
  .stat-card .sub { overflow-wrap: anywhere; line-height: 1.5; }
}
</style>
