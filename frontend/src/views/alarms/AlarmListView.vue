<template>
  <div class="alarm-page">
    <PageHeader title="告警中心" desc="同一资源 + 同一检查项自动去重合并为一条告警；恢复后 30 分钟内再次触发将重新打开原告警">
      <el-button :icon="Download" :loading="exporting" @click="exportCsv">导出 CSV</el-button>
      <el-button :icon="Refresh" @click="reload">刷新</el-button>
    </PageHeader>

    <!-- 统计条：点击快速筛选 -->
    <div class="chips">
      <div v-for="c in chips" :key="c.key" class="chip" :class="{ active: c.active }" :style="{ '--c': c.color }" @click="c.onClick">
        <span class="bar" />
        <div><div class="label">{{ c.label }}</div><div class="value num">{{ c.value }}</div></div>
      </div>
    </div>

    <div class="hi-card">
      <div class="filter-bar mb-16">
        <el-select v-model="query.severity" multiple collapse-tags clearable placeholder="告警级别" @change="search">
          <el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" />
        </el-select>
        <el-select v-model="query.service" clearable placeholder="云服务" @change="search">
          <el-option v-for="(n, k) in SERVICES" :key="k" :value="k" :label="n" />
        </el-select>
        <el-select v-model="query.resource_type" clearable placeholder="资源类型" @change="search">
          <el-option v-for="(n, k) in RESOURCE_TYPES" :key="k" :value="k" :label="n" />
        </el-select>
        <el-input v-model="query.keyword" class="kw" clearable placeholder="告警标题 / 资源名称 / 规则 ID" :prefix-icon="Search"
                  @keyup.enter="search" @clear="search" />
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="onReset">重置</el-button>
      </div>

      <div class="table-toolbar">
        <div class="flex gap-12">
          <el-radio-group v-model="query.status_group" @change="search">
            <el-radio-button v-for="g in STATUS_GROUPS" :key="g.value" :value="g.value">{{ g.label }}</el-radio-button>
          </el-radio-group>
          <el-tooltip content="仅“告警中”的告警可勾选认领" placement="top">
            <el-button v-if="canAck" type="primary" plain :icon="Select" :disabled="!selected.length" @click="onBatchAck">
              批量认领<template v-if="selected.length">（{{ selected.length }}）</template>
            </el-button>
          </el-tooltip>
        </div>
        <div class="flex gap-12">
          <transition name="fade">
            <span v-if="pending && !autoRefresh" class="pending">有 {{ pending }} 条告警变化 <el-link type="primary" @click="reload">立即刷新</el-link></span>
          </transition>
          <el-switch v-model="autoRefresh" inline-prompt active-text="实时" inactive-text="手动" />
          <span class="text-3">共 {{ total }} 条</span>
        </div>
      </div>

      <el-table v-loading="loading" :data="rows" row-key="id" :row-class-name="rowClass" @selection-change="(v: Alarm[]) => (selected = v)"
                @row-click="(row: Alarm) => openDrawer(row.id)">
        <el-table-column v-if="canAck" type="selection" width="44" :selectable="(row: Alarm) => row.status === 'FIRING'" />
        <el-table-column label="级别" width="96"><template #default="{ row }"><SeverityTag :level="row.severity" /></template></el-table-column>
        <el-table-column label="告警" min-width="240">
          <template #default="{ row }">
            <div class="title"><span class="ellipsis">{{ row.title }}</span><span v-if="row.count > 1" class="cnt">×{{ row.count }}</span></div>
            <div class="text-3 mono">{{ row.rule_id }} · {{ row.check_item }}</div>
          </template>
        </el-table-column>
        <el-table-column v-if="app.multiTenant" label="租户" width="92"><template #default="{ row }"><TenantTag :id="row.tenant_id" /></template></el-table-column>
        <el-table-column label="资源" min-width="180">
          <template #default="{ row }">
            <div class="ellipsis">{{ row.resource_name }}<span v-if="row.sub_object" class="text-3"> / {{ row.sub_object }}</span></div>
            <div class="text-3">{{ SERVICES[row.service] }} · {{ RESOURCE_TYPES[row.resource_type] }}</div>
          </template>
        </el-table-column>
        <el-table-column label="当前值 / 阈值" width="130">
          <template #default="{ row }">
            <b class="num" :style="{ color: row.status === 'FIRING' ? SEVERITY[row.severity].color : undefined }">{{ row.current_value ?? '-' }}</b>
            <div class="text-3">{{ row.threshold_text ?? '-' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tooltip v-if="row.suppress_reason" :content="row.suppress_reason"><span><StatusTag :status="row.status" /></span></el-tooltip>
            <StatusTag v-else :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="首次发生" width="140">
          <template #default="{ row }"><div>{{ fmtTime(row.first_seen) }}</div><div class="text-3">{{ fromNow(row.first_seen) }}</div></template>
        </el-table-column>
        <el-table-column label="持续时长" width="100"><template #default="{ row }">{{ durationOf(row.first_seen, row.resolved_at) }}</template></el-table-column>
        <el-table-column label="负责人 / 认领人" width="128">
          <template #default="{ row }"><div>{{ row.owner || '-' }}</div><div class="text-3">{{ row.ack_by ? `${row.ack_by} 已认领` : '未认领' }}</div></template>
        </el-table-column>
        <el-table-column label="操作" width="118" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDrawer(row.id)">详情</el-button>
            <el-button v-if="row.status === 'FIRING' && canAck" link type="warning" @click.stop="onAck(row as Alarm)">认领</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty description="当前筛选条件下没有告警，一切正常 🎉" :image-size="88" /></template>
      </el-table>

      <div class="pager">
        <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100]" background
                       layout="total, sizes, prev, pager, next, jumper" />
      </div>
    </div>

    <AlarmDrawer v-model="drawerId" @changed="reload" />
  </div>
</template>

<script setup lang="ts">
import { Download, Refresh, Search, Select } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type AlarmQuery, ackAlarm, batchAck, listAlarms } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import { useTable } from '@/composables/useTable'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { Alarm } from '@/types'
import { ALARM_STATUS, RESOURCE_TYPES, SERVICES, SEVERITIES, SEVERITY } from '@/utils/dict'
import { downloadCsv, durationOf, fmtTime, fromNow } from '@/utils/format'
import AlarmDrawer from './AlarmDrawer.vue'

const STATUS_GROUPS = [
  { value: 'active', label: '活动告警' }, { value: 'firing', label: '待认领' }, { value: 'acked', label: '已认领' },
  { value: 'suppressed', label: '已抑制' }, { value: 'resolved', label: '历史告警' }, { value: 'all', label: '全部' },
]

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const canAck = computed(() => useUserStore().can('alarm:handle'))

const initial: Required<Pick<AlarmQuery, 'status_group'>> & AlarmQuery = {
  status_group: 'active', severity: [], service: undefined, resource_type: undefined, resource_ref: undefined, keyword: '',
}
const { query, page, size, total, rows, loading, load, search, reset } = useTable<Alarm, AlarmQuery>(listAlarms, initial)
const selected = ref<Alarm[]>([])
const drawerId = ref<number | null>(null)
const autoRefresh = ref(true)
const pending = ref(0)
const exporting = ref(false)

// ---- URL 参数 → 查询条件（支持从总览、铃铛、通知弹窗、资源详情跳转）----
function applyRouteQuery() {
  const q = route.query
  if (typeof q.status_group === 'string') query.status_group = q.status_group
  if (typeof q.severity === 'string') query.severity = q.severity.split(',')
  if (typeof q.resource_type === 'string') query.resource_type = q.resource_type
  if (typeof q.resource_ref === 'string') query.resource_ref = Number(q.resource_ref)
  if (typeof q.id === 'string') drawerId.value = Number(q.id)
}
onMounted(() => { applyRouteQuery(); load(); app.refreshStats() })
watch(() => route.query.id, (id) => { if (typeof id === 'string') drawerId.value = Number(id) })
watch(drawerId, (id) => {
  const cur = route.query.id
  if (id === null && cur) router.replace({ query: { ...route.query, id: undefined } })
  if (id !== null && cur !== String(id)) router.replace({ query: { ...route.query, id: String(id) } })
})

const reload = () => { pending.value = 0; app.refreshStats(); return load() }
const onReset = () => { router.replace({ query: {} }); return reset() }
const openDrawer = (id: number) => (drawerId.value = id)
const rowClass = ({ row }: { row: Alarm }) => `sev-row-${row.severity.toLowerCase()}`

// ---- SSE 推送：实时模式下 1.5s 防抖刷新；手动模式下仅提示变化条数 ----
let debounce: number | undefined
watch(() => app.eventSeq, () => {
  if (!autoRefresh.value) { pending.value++; return }
  clearTimeout(debounce)
  debounce = window.setTimeout(load, 1500)
})

// ---- 统计条 ----
const chips = computed(() => {
  const s = app.stats
  const sev = query.severity ?? []
  const pick = (v: string) => { query.status_group = 'active'; query.severity = sev.length === 1 && sev[0] === v ? [] : [v]; search() }
  const group = (g: string) => { query.status_group = g; query.severity = []; search() }
  return [
    { key: 'FIRING', label: '待认领', value: s?.FIRING ?? '-', color: 'var(--hi-p1)', active: query.status_group === 'firing', onClick: () => group('firing') },
    { key: 'ACKED', label: '处理中', value: s?.ACKED ?? '-', color: 'var(--hi-p3)', active: query.status_group === 'acked', onClick: () => group('acked') },
    { key: 'SUPPRESSED', label: '已抑制', value: s?.SUPPRESSED ?? '-', color: 'var(--hi-unknown)', active: query.status_group === 'suppressed', onClick: () => group('suppressed') },
    ...SEVERITIES.map((v) => ({
      key: v, label: `${v} ${SEVERITY[v].label}`, value: s?.[v] ?? '-', color: SEVERITY[v].color,
      active: sev.length === 1 && sev[0] === v, onClick: () => pick(v),
    })),
  ]
})

// ---- 操作 ----
async function onAck(row: Alarm) {
  await ackAlarm(row.id)
  ElMessage.success(`已认领：${row.resource_name} ${row.title}`)
  reload()
}
async function onBatchAck() {
  const { value } = await ElMessageBox.prompt(`将认领选中的 ${selected.value.length} 条告警，可填写处理备注（选填）`, '批量认领', {
    inputPlaceholder: '例如：已定位为上游变更导致，处理中', confirmButtonText: '确认认领', cancelButtonText: '取消',
  })
  const res = await batchAck(selected.value.map((a) => a.id), value || undefined)
  ElMessage.success(`已认领 ${res.acked} 条告警`)
  reload()
}
async function exportCsv() {
  exporting.value = true
  try {
    const all: Alarm[] = []
    for (let p = 1; p <= 10; p++) { // 最多导出 2000 条；更大量请使用报表中心异步导出
      const res = await listAlarms({ ...query, page: p, page_size: 200 })
      all.push(...res.items)
      if (all.length >= res.total) break
    }
    downloadCsv(`告警列表_${fmtTime(new Date().toISOString(), 'YYYYMMDD_HHmm')}.csv`,
      ['告警ID', '租户', '级别', '状态', '告警标题', '规则', '云服务', '资源类型', '资源', '子对象', '当前值', '阈值', '首次发生', '最近发生', '恢复时间', '触发次数', '负责人', '认领人'],
      all.map((a) => [a.id, app.tenantOf(a.tenant_id)?.name ?? '', a.severity, ALARM_STATUS[a.status]?.label, a.title, a.rule_id, SERVICES[a.service], RESOURCE_TYPES[a.resource_type],
        a.resource_name, a.sub_object, a.current_value, a.threshold_text, fmtTime(a.first_seen, 'YYYY-MM-DD HH:mm:ss'),
        fmtTime(a.last_seen, 'YYYY-MM-DD HH:mm:ss'), fmtTime(a.resolved_at, 'YYYY-MM-DD HH:mm:ss'), a.count, a.owner, a.ack_by]))
    ElMessage.success(`已导出 ${all.length} 条`)
  } finally {
    exporting.value = false
  }
}
</script>

<style scoped lang="scss">
.chips {
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin-bottom: 16px;
  .chip {
    position: relative; display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; overflow: hidden;
    background: var(--hi-bg-card); border-radius: var(--hi-radius); box-shadow: var(--hi-shadow); border: 1px solid transparent; transition: all 0.2s;
    .bar { width: 4px; height: 32px; border-radius: 2px; background: var(--c); }
    .label { font-size: 12px; color: var(--hi-text-2); }
    .value { font-size: 22px; font-weight: 700; line-height: 1.25; color: var(--hi-text-1); }
    &:hover { box-shadow: var(--hi-shadow-hover); }
    &.active { border-color: var(--c); background: color-mix(in srgb, var(--c) 6%, #fff); .value { color: var(--c); } }
  }
}
.filter-bar .el-select { width: 180px; }
.kw { width: 280px !important; }
.title { display: flex; align-items: center; min-width: 0; font-weight: 600; color: var(--hi-text-1);
  .ellipsis { flex: 0 1 auto; min-width: 0; }
  .cnt { flex: none; margin-left: 6px; padding: 0 6px; border-radius: 8px; font-size: 11px; font-weight: 500; color: var(--hi-text-2); background: var(--hi-bg-soft); border: 1px solid var(--hi-border); } }
.pending { font-size: 12px; color: var(--hi-p2); }
:deep(.el-table__row) { cursor: pointer; }
</style>
