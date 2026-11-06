<script setup lang="ts">
import { computed } from 'vue'
import { nativePanelData } from './cce-native-display'

const props = defineProps<{ native: Record<string, unknown> }>()
const panel = computed(() => nativePanelData(props.native))
</script>

<template>
  <section class="cce-native" aria-label="原生指标与工作负载归属">
    <h4 class="native-heading">原生指标与归属</h4>
    <p v-if="!panel.hasMetrics && !panel.hasOwners" class="text-3">尚无原生采集数据</p>
    <el-tabs v-else>
      <el-tab-pane v-for="section in panel.metrics" :key="section.kind" :label="section.label" :name="section.kind" lazy>
        <div class="native-state">
          <el-tag type="info" size="small" effect="plain">{{ section.status }}</el-tag>
          <span class="text-3">{{ section.hint }}</span>
        </div>
        <div v-if="section.rows.length" class="native-scroll" tabindex="0" :aria-label="`${section.label}表格`">
          <table class="native-table metric-table">
            <caption class="sr-only">{{ section.label }}，CPU 单位核，内存单位 MiB</caption>
            <thead><tr>
              <th v-if="section.kind === 'pods'" scope="col">命名空间</th>
              <th scope="col">{{ section.kind === 'pods' ? 'Pod' : 'Node' }}</th>
              <th scope="col" class="numeric">CPU（核）</th>
              <th scope="col" class="numeric">内存（MiB）</th>
              <th scope="col">上游采样时间（本地）</th>
              <th scope="col">采样窗口</th>
              <th scope="col">时效</th>
            </tr></thead>
            <tbody><tr v-for="row in section.rows" :key="row.key" :class="{ historical: row.historical }">
              <td v-if="section.kind === 'pods'">{{ row.namespace }}</td>
              <td>{{ row.name }}</td>
              <td class="numeric">{{ row.cpu }}</td>
              <td class="numeric">{{ row.memory }}</td>
              <td><time v-if="row.observedRaw" :datetime="row.observedRaw" :title="row.observedRaw">{{ row.observed }}</time><span v-else>未知</span></td>
              <td>{{ row.window }}</td>
              <td>{{ row.state }}</td>
            </tr></tbody>
          </table>
        </div>
      </el-tab-pane>
      <el-tab-pane v-if="panel.hasOwners" label="工作负载归属" name="owners" lazy>
        <div class="native-state">
          <el-tag type="info" size="small" effect="plain">{{ panel.owners.status }}</el-tag>
          <span class="text-3">{{ panel.owners.hint }} {{ panel.owners.sources }}</span>
          <span v-if="panel.owners.collectedAt" class="text-3">采集：{{ panel.owners.collectedAt }}</span>
        </div>
        <div v-if="panel.owners.rows.length" class="native-scroll" tabindex="0" aria-label="工作负载归属表格">
          <table class="native-table owner-table">
            <caption class="sr-only">Pod 直接归属和经 UID 核对的顶层归属</caption>
            <thead><tr><th scope="col">命名空间</th><th scope="col">Pod</th><th scope="col">直接归属</th><th scope="col">顶层归属</th><th scope="col">核对结果</th></tr></thead>
            <tbody><tr v-for="row in panel.owners.rows" :key="row.key">
              <td>{{ row.namespace }}</td><td>{{ row.name }}</td><td>{{ row.direct }}</td><td>{{ row.top }}</td><td>{{ row.state }}</td>
            </tr></tbody>
          </table>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.cce-native { margin-top: 24px; min-width: 0; }
.native-heading { margin: 0 0 12px; }
.native-state { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 12px; }
.native-scroll { max-height: 320px; overflow: auto; border: 1px solid var(--el-border-color-lighter); }
.native-scroll:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.native-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; color: var(--el-text-color-regular); }
.metric-table { min-width: 920px; }
.owner-table { min-width: 860px; }
.native-table th, .native-table td { padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--el-border-color-lighter); overflow-wrap: anywhere; }
.native-table th { position: sticky; top: 0; z-index: 1; background: var(--el-fill-color-light); color: var(--el-text-color-secondary); white-space: nowrap; }
.native-table .numeric { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.native-table tbody tr:last-child td { border-bottom: 0; }
.historical { color: var(--el-text-color-secondary); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
