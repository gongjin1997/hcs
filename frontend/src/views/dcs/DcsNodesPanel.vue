<script setup lang="ts">
import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { getDcsInstance } from '@/api/dcs'
import type { DcsInstance, DcsNode } from '@/types/dcs'
import { fmtTime } from '@/utils/format'
import { NODE_ROLE_TEXT, NODE_STATUS_TEXT, TOPOLOGY_STATE_TEXT, nodeStatusType, sortNodes, topologyHealth, topologyText } from './dcs-utils'

/** 单个实例的分片与节点（本地保存的最近一次逻辑节点结果，不请求云）。 */
const props = defineProps<{ instance: DcsInstance | null }>()
const detail = shallowRef<DcsInstance | null>(null)
const loading = shallowRef(false)
const error = shallowRef('')
let controller: AbortController | null = null

async function load() {
  controller?.abort()
  controller = null
  const target = props.instance
  detail.value = null
  error.value = ''
  loading.value = false
  if (!target) return
  if (Array.isArray(target.nodes)) { detail.value = target; return }     // 深链打开时已取过单实例详情
  const current = new AbortController()
  controller = current
  loading.value = true
  try {
    const data = await getDcsInstance(target.id, { signal: current.signal, tenantId: target.tenant_id })
    if (controller !== current || current.signal.aborted) return
    if (data.id !== target.id || data.tenant_id !== target.tenant_id) throw new Error('实例范围不匹配')
    detail.value = data
  } catch {
    if (controller === current && !current.signal.aborted) error.value = '节点信息加载失败，请重试'
  } finally {
    if (controller === current) loading.value = false
  }
}
watch(() => props.instance, load, { immediate: true })
onScopeDispose(() => controller?.abort())

const topology = computed(() => detail.value?.topology ?? props.instance?.topology ?? null)
const health = computed(() => topologyHealth(topology.value))
const nodes = computed(() => sortNodes(detail.value?.nodes ?? []))
const shardLabel = (node: DcsNode) => node.kind === 'redis-proxy' ? 'Proxy' : node.shard || '—'
const rowClass = ({ row }: { row: DcsNode }) => (row.status === 'Inactive' ? 'node-fault' : '')
const emptyText = computed(() => {
  if (error.value) return error.value
  if (!topology.value) return '尚未获取节点信息，等待下一轮 DCS 巡检'
  if (topology.value.state === 'NOT_SUPPORTED') return 'Memcached 与 Redis 3.0 实例不提供逻辑节点查询'
  if (topology.value.state === 'NOT_RETURNED') return topology.value.warning ?? '源端未返回此实例的节点'
  return topology.value.warning ?? '暂无节点'
})
</script>

<template>
  <section v-if="props.instance" class="hi-card dcs-nodes" aria-label="分片与节点">
    <div class="hi-card__title nodes-title">
      <span>分片与节点<span class="sub">{{ props.instance.name }}</span></span>
      <span class="nodes-summary">
        <el-tag size="small" :type="health.type" effect="plain">{{ health.text }}</el-tag>
        <span>{{ topologyText(topology) }}</span>
        <span v-if="topology?.observed_at" class="text-3">本地获取 {{ fmtTime(topology.observed_at, 'YYYY-MM-DD HH:mm:ss') }}</span>
      </span>
    </div>
    <el-alert v-if="topology && topology.state === 'STALE'" :title="TOPOLOGY_STATE_TEXT.STALE" type="warning" :closable="false" show-icon />
    <el-alert v-else-if="topology?.unknown" :title="`${topology.unknown} 个节点状态未识别，节点告警暂不恢复`" type="warning" :closable="false" show-icon />
    <el-table v-loading="loading" :data="nodes" row-key="name" size="small" stripe :row-class-name="rowClass" max-height="420">
      <el-table-column label="分片" min-width="120"><template #default="{ row }">{{ shardLabel(row as DcsNode) }}</template></el-table-column>
      <el-table-column label="角色" width="100"><template #default="{ row }">{{ NODE_ROLE_TEXT[row.role ?? ''] ?? row.role ?? '—' }}</template></el-table-column>
      <el-table-column label="节点（IP:端口）" min-width="180" prop="name" />
      <el-table-column label="状态" width="120"><template #default="{ row }">
        <el-tag size="small" :type="nodeStatusType(row.status)" effect="plain">{{ NODE_STATUS_TEXT[row.status ?? ''] ?? row.status ?? '未知' }}</el-tag>
      </template></el-table-column>
      <el-table-column label="可用区" min-width="120"><template #default="{ row }">{{ row.az ?? '—' }}</template></el-table-column>
      <el-table-column label="源端节点名" min-width="200"><template #default="{ row }">{{ row.node_name ?? '—' }}</template></el-table-column>
      <template #empty><el-empty :description="emptyText" :image-size="48" /></template>
    </el-table>
    <div class="text-3 nodes-note">来自 DCS 批量查询实例节点信息接口（每轮巡检一次）。主节点故障会使该分片不可写，从节点或 Proxy 故障会降低冗余或接入能力；节点告警只在实例运行中时判定。</div>
  </section>
</template>

<style scoped>
.dcs-nodes { min-width: 0; }
.nodes-title { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.nodes-title > span { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.nodes-summary { font-size: 13px; font-weight: normal; }
.dcs-nodes .el-alert { margin-bottom: 10px; }
.dcs-nodes :deep(.node-fault td) { background: var(--el-color-danger-light-9) !important; }
.nodes-note { font-size: 12px; margin-top: 8px; line-height: 1.6; }
@media (max-width: 650px) {
  .nodes-title { align-items: flex-start; flex-direction: column; }
}
</style>
