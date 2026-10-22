<template>
  <el-drawer :model-value="modelValue" size="760px" class="pod-drawer" @update:model-value="(v: boolean) => emit('update:modelValue', v)" @open="load">
    <template #header>
      <div class="d-head">
        <div class="d-title"><el-icon><Box /></el-icon><b class="mono">{{ podName }}</b>
          <el-tag v-if="st" :type="POD_PHASE[st.phase]?.type ?? 'info'" size="small">{{ POD_PHASE[st.phase]?.label ?? st.phase }}</el-tag>
          <el-tag v-if="st && !st.ready && !st.deleted" type="danger" size="small" effect="plain">未就绪</el-tag>
          <el-tag v-if="st?.deleted" type="info" size="small" effect="plain">已删除</el-tag>
        </div>
        <div class="text-3 d-sub">
          <TenantTag v-if="st" :id="st.tenant_id" :name="st.tenant_name" />
          <span>{{ st?.cluster_name ?? clusterOf }} / {{ st?.namespace ?? nsOf }}</span>
          <span v-if="st">{{ st.workload_kind }} · {{ st.workload }}</span>
        </div>
      </div>
    </template>

    <div v-loading="loading" class="d-body">
      <div class="days">
        <span class="text-3">时间范围</span>
        <el-radio-group v-model="days" size="small" @change="load">
          <el-radio-button :value="1">24 小时</el-radio-button><el-radio-button :value="3">3 天</el-radio-button><el-radio-button :value="7">7 天</el-radio-button>
        </el-radio-group>
        <span class="flex-1" />
        <span class="text-3">窗口内重启 <b class="num" :class="{ warn: (data?.restarts_in_window ?? 0) > 0 }">{{ data?.restarts_in_window ?? 0 }}</b> 次</span>
      </div>

      <template v-if="st">
        <el-descriptions :column="3" size="small" border class="mb-16">
          <el-descriptions-item label="节点">{{ st.node || '-' }}</el-descriptions-item>
          <el-descriptions-item label="Pod IP"><span class="mono">{{ st.pod_ip ?? '-' }}</span></el-descriptions-item>
          <el-descriptions-item label="累计重启"><b :class="{ warn: st.restarts > 0 }">{{ st.restarts }}</b> 次</el-descriptions-item>
          <el-descriptions-item label="启动时间">{{ fmtTime(st.started_at, 'YYYY-MM-DD HH:mm') }}</el-descriptions-item>
          <el-descriptions-item label="状态原因">{{ st.status_reason ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="最近采集">{{ fromNow(st.last_seen) }}</el-descriptions-item>
          <el-descriptions-item label="标签" :span="3">
            <span v-for="(v, k) in st.labels" :key="k" class="label mono">{{ k }}={{ v }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="sub-title">容器</div>
        <el-table :data="st.containers" size="small" border class="mb-16">
          <el-table-column label="容器" min-width="130"><template #default="{ row }"><b>{{ row.name }}</b><div class="text-3 mono img" :title="row.image">{{ shortImage(row.image) }}</div></template></el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <span class="cstate" :class="row.state"><i />{{ STATE[row.state] ?? row.state }}</span>
              <div v-if="row.reason" class="text-3">{{ row.reason }}</div>
            </template>
          </el-table-column>
          <el-table-column label="重启" width="64" align="center"><template #default="{ row }"><b :class="{ warn: row.restart_count > 0 }">{{ row.restart_count }}</b></template></el-table-column>
          <el-table-column label="上次终止" min-width="170">
            <template #default="{ row }">
              <template v-if="row.last_reason"><el-tag :type="row.last_reason === 'OOMKilled' ? 'danger' : 'warning'" size="small" effect="plain">{{ row.last_reason }}</el-tag>
                <span class="text-3"> exit {{ row.last_exit_code ?? '-' }}</span><div class="text-3">{{ fmtTime(row.last_finished) }}</div></template>
              <span v-else class="text-3">-</span>
            </template>
          </el-table-column>
          <el-table-column label="内存限制" width="84"><template #default="{ row }">{{ row.memory_limit ?? '-' }}</template></el-table-column>
        </el-table>
      </template>
      <el-alert v-else-if="!loading" type="info" :closable="false" show-icon title="未采集到该 Pod 的当前状态（可能已被删除或不在采集范围内），以下为历史事件" class="mb-16" />

      <div class="sub-title">事件时间线 <span class="text-3">（{{ data?.events.length ?? 0 }} 条，同类事件按 Pod + 原因合并计数）</span></div>
      <el-timeline v-if="data?.events.length" class="tl">
        <el-timeline-item v-for="e in data.events" :key="e.id" :timestamp="fmtTime(e.first_time)" placement="top" :color="e.color" :hollow="!e.ongoing">
          <div class="ev">
            <div class="ev-head">
              <span class="ev-type" :style="{ '--c': e.color }">{{ e.type_name }}</span>
              <span class="mono text-3">{{ e.reason }}</span>
              <span v-if="e.count > 1" class="cnt">×{{ e.count }}</span>
              <el-tag v-if="e.ongoing" type="danger" size="small" effect="light" round>进行中</el-tag>
              <span class="flex-1" />
              <span class="text-3">{{ POD_SOURCE[e.source] ?? e.source }}</span>
            </div>
            <div class="ev-msg">{{ e.message }}</div>
            <div class="text-3 ev-foot">
              <span v-if="e.container">容器 {{ e.container }}</span>
              <span>最近 {{ fmtTime(e.last_time) }}</span>
              <span v-if="e.duration_sec">持续 {{ humanize(e.duration_sec) }}</span>
              <span v-if="e.peak != null">峰值 {{ e.peak }}{{ e.detail?.unit ?? '' }}</span>
            </div>
          </div>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-else-if="!loading" description="时间范围内没有事件" :image-size="80" />
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { Box } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import { podTimeline } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import type { PodTimeline } from '@/types'
import { POD_PHASE, POD_SOURCE } from '@/utils/dict'
import { fmtTime, fromNow, humanize } from '@/utils/format'

/** podKey = 集群 ID / 命名空间 / Pod 名 */
const props = defineProps<{ modelValue: boolean; podKey: string; clusterName?: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const data = ref<PodTimeline | null>(null)
const loading = ref(false)
const days = ref(7)
const st = computed(() => data.value?.state ?? null)
const parts = computed(() => props.podKey.split('/'))
const podName = computed(() => parts.value[2] ?? props.podKey)
const nsOf = computed(() => parts.value[1] ?? '')
const clusterOf = computed(() => props.clusterName ?? parts.value[0] ?? '')
const STATE: Record<string, string> = { running: '运行中', waiting: '等待中', terminated: '已终止' }
const shortImage = (img: string) => img.split('/').slice(-2).join('/')

async function load() {
  if (!props.podKey) return
  loading.value = true
  try { data.value = await podTimeline(props.podKey, days.value) } finally { loading.value = false }
}
</script>

<style scoped lang="scss">
.d-head { display: flex; flex-direction: column; gap: 6px; min-width: 0;
  .d-title { display: flex; align-items: center; gap: 8px; font-size: 15px; color: var(--hi-text-1); b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } }
  .d-sub { display: flex; align-items: center; gap: 10px; font-size: 12px; } }
.d-body { padding: 0 4px; }
.days { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.flex-1 { flex: 1; }
.mb-16 { margin-bottom: 16px; }
.sub-title { margin: 0 0 8px; font-weight: 600; font-size: 13px; color: var(--hi-text-2); .text-3 { font-weight: 400; font-size: 12px; } }
.warn { color: var(--hi-p2, #f57c00); }
.label { display: inline-block; margin: 2px 6px 2px 0; padding: 0 6px; border-radius: 4px; font-size: 11px; background: var(--hi-bg-soft); border: 1px solid var(--hi-border); }
.img { font-size: 11px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cstate { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; i { width: 7px; height: 7px; border-radius: 50%; background: #8c8c8c; }
  &.running i { background: var(--hi-ok); } &.waiting i { background: #f57c00; } &.terminated i { background: var(--hi-p1); } }
.tl { padding-left: 4px; }
.ev { padding: 8px 12px; border-radius: 6px; background: var(--hi-bg-soft); border: 1px solid var(--hi-border);
  .ev-head { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .ev-type { padding: 0 8px; height: 20px; line-height: 20px; border-radius: 4px; font-size: 12px; font-weight: 600; color: var(--c); background: color-mix(in srgb, var(--c) 10%, #fff); }
  .cnt { font-size: 12px; font-weight: 700; color: var(--hi-p2, #f57c00); }
  .ev-msg { margin: 6px 0 4px; font-size: 13px; color: var(--hi-text-2); line-height: 1.6; word-break: break-all; }
  .ev-foot { display: flex; gap: 14px; font-size: 12px; } }
</style>
