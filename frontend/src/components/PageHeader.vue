<template>
  <div class="page-header">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="desc">{{ desc }}</p>
    </div>
    <div class="extra"><slot /></div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ title: string; desc?: string }>()
</script>

<style scoped lang="scss">
.page-header {
  display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 16px;
  h2 { margin: 0; font-size: 19px; font-weight: 600; color: var(--hi-text-1); letter-spacing: 0.2px; }
  p { margin: 6px 0 0; font-size: 13px; color: var(--hi-text-3); }
  .extra { display: flex; align-items: center; gap: 8px; flex: none; }
}
</style>
