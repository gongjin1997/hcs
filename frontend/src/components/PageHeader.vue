<template>
  <Teleport v-if="target && refresh" :to="target">
    <el-tooltip content="刷新当前页面" placement="bottom">
      <el-button class="page-refresh" text circle aria-label="刷新当前页面" :loading="refreshing || running" :icon="Refresh" @click="runRefresh" />
    </el-tooltip>
  </Teleport>
</template>

<script lang="ts">
import type { InjectionKey, ShallowRef } from 'vue'

export const pageHeaderTargetKey: InjectionKey<Readonly<ShallowRef<HTMLElement | null>>> = Symbol('page-refresh-target')
</script>

<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue'
import { inject, ref, shallowRef } from 'vue'

const props = defineProps<{
  title: string
  desc?: string
  refresh?: () => unknown | Promise<unknown>
  refreshing?: boolean
}>()
const target = inject(pageHeaderTargetKey, shallowRef(null))
const running = ref(false)

async function runRefresh() {
  if (running.value || props.refreshing || !props.refresh) return
  running.value = true
  try { await props.refresh() } catch { /* 请求层负责显示错误。 */ }
  finally { running.value = false }
}
</script>
