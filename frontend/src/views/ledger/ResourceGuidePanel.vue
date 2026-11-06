<template>
  <section v-loading="loading" class="resource-guide">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="guide">
      <div class="table-heading"><h3>{{ guide.name }}</h3><router-link :to="{ path: '/guides', query: { type: resourceType } }">完整指南</router-link></div>
      <p>{{ guide.summary }}</p>
      <div class="section-title">查看与排查</div>
      <ol><li v-for="item in guide.guidance" :key="item">{{ item }}</li></ol>
      <div class="section-title">指标含义</div>
      <el-table :data="guide.metrics" size="small" border>
        <el-table-column prop="label" label="指标" min-width="120" />
        <el-table-column prop="unit" label="单位" width="80" />
        <el-table-column prop="meaning" label="说明" min-width="230" />
        <template #empty>此类型暂无周期监控指标，属性与容量说明见完整指南。</template>
      </el-table>
      <div class="section-title">最佳实践</div>
      <ul><li v-for="item in guide.best_practices" :key="item">{{ item }}</li></ul>
      <el-collapse v-if="guide.limitations.length">
        <el-collapse-item title="采集范围与限制" name="limits">
          <ul><li v-for="item in guide.limitations" :key="item">{{ item }}</li></ul>
        </el-collapse-item>
      </el-collapse>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onScopeDispose, ref, shallowRef, watch } from 'vue'
import { get } from '@/api/request'

interface Field { key: string; label: string; unit: string | null; meaning: string }
interface Guide { name: string; summary: string; metrics: Field[]; guidance: string[]; best_practices: string[]; limitations: string[] }
const props = defineProps<{ resourceType: string }>()
const guide = shallowRef<Guide | null>(null)
const loading = ref(false)
const error = ref('')
let controller: AbortController | undefined
watch(() => props.resourceType, async type => {
  controller?.abort()
  const current = new AbortController()
  controller = current
  guide.value = null
  error.value = ''
  loading.value = true
  try {
    const value = await get<Guide>('/guides/' + encodeURIComponent(type), undefined, { signal: current.signal, silent: true })
    if (!current.signal.aborted) guide.value = value
  } catch (cause) {
    if (!current.signal.aborted) error.value = cause instanceof Error ? cause.message : '指南加载失败'
  } finally { if (!current.signal.aborted) loading.value = false }
}, { immediate: true })
onScopeDispose(() => controller?.abort())
</script>

<style scoped>
.resource-guide { min-height: 160px; line-height: 1.8; }
h3 { margin: 0; }
ul, ol { padding-left: 20px; }
li { margin-bottom: 6px; }
</style>
