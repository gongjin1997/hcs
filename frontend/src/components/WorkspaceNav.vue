<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { permittedItems, workspaceFor } from '@/config/workspace-navigation'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const user = useUserStore()
const workspace = computed(() => workspaceFor(route.path))
const items = computed(() => workspace.value ? permittedItems(workspace.value, user.canAny) : [])
</script>

<template>
  <nav v-if="workspace && items.length" class="workspace-nav" :aria-label="workspace.title">
    <router-link v-for="item in items" :key="item.path" :to="item.path" class="workspace-link"
      :class="{ active: route.path === item.path }" :aria-current="route.path === item.path ? 'page' : undefined">
      {{ item.label }}
    </router-link>
  </nav>
</template>

<style scoped lang="scss">
.workspace-nav { display: flex; gap: 4px; padding: 0 0 10px; margin-bottom: var(--hi-gap); border-bottom: 1px solid var(--hi-border); min-width: 0; max-width: 100%; overflow-x: auto; scrollbar-width: thin; }
.workspace-link { flex: 0 0 auto; padding: 9px 18px; color: var(--hi-text-2); font-size: 14px; text-decoration: none; border-radius: 6px; white-space: nowrap; }
.workspace-link:hover, .workspace-link:focus-visible { color: var(--el-color-primary); background: var(--hi-bg-soft); }
.workspace-link:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
.workspace-link.active { color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-weight: 600; }
@media (max-width: 767px) { .workspace-link { padding: 8px 12px; font-size: 13px; } }
</style>
