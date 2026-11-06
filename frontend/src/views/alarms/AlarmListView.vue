<template>
  <div class="alarm-page">
    <PageHeader title="告警中心" :refresh="reload" :refreshing="loading" />
    <el-alert v-if="scope.error.value || routeError" :title="scope.error.value || routeError" type="error" :closable="false" show-icon />
    <template v-if="!scope.error.value && !routeError">

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

      <div class="table-toolbar table-heading">
        <div class="table-actions">
          <el-radio-group v-model="query.status_group" @change="search">
            <el-radio-button v-for="g in STATUS_GROUPS" :key="g.value" :value="g.value">{{ g.label }}</el-radio-button>
          </el-radio-group>
          <el-tooltip content="仅“告警中”的告警可勾选认领" placement="top">
            <el-button v-if="canAck" type="primary" plain :icon="Select" :disabled="!selected.length" :loading="batchAcking" @click="onBatchAck">
              批量认领<template v-if="selected.length">（{{ selected.length }}）</template>
            </el-button>
          </el-tooltip>
        </div>
        <div class="table-actions">
          <transition name="fade">
            <span v-if="pending && !autoRefresh" class="pending">有 {{ pending }} 条告警变化 <el-link type="primary" @click="reload">立即刷新</el-link></span>
          </transition>
          <el-switch v-model="autoRefresh" inline-prompt active-text="实时" inactive-text="手动" />
          <span class="text-3">共 {{ total }} 条</span>
          <el-button :icon="Download" :loading="exporting" @click="exportExcel">导出 Excel</el-button>
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
        <template #empty><el-empty description="当前筛选条件下没有告警" :image-size="88" /></template>
      </el-table>

      <div class="pager">
        <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100]" background
                       layout="total, sizes, prev, pager, next, jumper" />
      </div>
    </div>

    <AlarmDrawer v-model="drawerId" @changed="reload" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { Download, Search, Select } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type AlarmQuery, ackAlarm, batchAck, listAlarms } from '@/api'
import { exportAlarms } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import { useTable } from '@/composables/useTable'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { useResourceRouteScope } from '@/composables/useResourceRouteScope'
import { positiveIdParam } from '@/config/resource-navigation'
import type { Alarm } from '@/types'
import { ALARM_STATUS, RESOURCE_TYPES, SERVICES, SEVERITIES, SEVERITY } from '@/utils/dict'
import { durationOf, fmtTime, fromNow } from '@/utils/format'
import AlarmDrawer from './AlarmDrawer.vue'

const STATUS_GROUPS = [
  { value: 'active', label: '活动告警' }, { value: 'firing', label: '待认领' }, { value: 'acked', label: '已认领' },
  { value: 'suppressed', label: '已抑制' }, { value: 'resolved', label: '历史告警' }, { value: 'all', label: '全部' },
]

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const user = useUserStore()
const scope = useResourceRouteScope({ query: () => route.query, app, viewer: user, permission: 'alarm:view' })
const routeError = computed(() => (route.query.resource_ref != null && positiveIdParam(route.query.resource_ref) === null)
  || (route.query.id != null && positiveIdParam(route.query.id) === null) ? '资源或告警链接无效' : '')
const canAck = computed(() => useUserStore().can('alarm:handle'))

const initial: Required<Pick<AlarmQuery, 'status_group'>> & AlarmQuery = {
  status_group: 'active', severity: [], service: undefined, resource_type: undefined, resource_ref: undefined, keyword: '',
}
const { query, page, size, total, rows, loading, load, search, reset } = useTable<Alarm, AlarmQuery>(async params => {
  if (routeError.value) return { items: [], total: 0, page: params.page, page_size: params.page_size }
  return await scope.request('list', config => listAlarms(params, config)) ?? { items: [], total: 0, page: params.page, page_size: params.page_size }
}, initial)
const selected = ref<Alarm[]>([])
const drawerId = ref<number | null>(null)
const autoRefresh = ref(true)
const pending = ref(0)
const exporting = ref(false)
const batchAcking = ref(false)
let disposed = false
onScopeDispose(() => { disposed = true; clearTimeout(debounce) })

// ---- URL 参数 → 查询条件（支持从总览、铃铛、通知弹窗、资源详情跳转）----
function applyRouteQuery() {
  const q = route.query
  const resource = positiveIdParam(q.resource_ref), id = positiveIdParam(q.id)
  query.status_group = typeof q.status_group === 'string' ? q.status_group : 'active'
  query.severity = typeof q.severity === 'string' ? q.severity.split(',') : []
  query.resource_type = typeof q.resource_type === 'string' ? q.resource_type : undefined
  query.resource_ref = resource ?? undefined
  drawerId.value = scope.error.value || routeError.value ? null : id
}
onMounted(() => { applyRouteQuery(); load(); app.refreshStats() })
watch(() => [route.query.resource_ref, route.query.resource_type, route.query.status_group, route.query.severity, scope.key.value], () => {
  rows.value = []; total.value = 0; selected.value = []; drawerId.value = null
  applyRouteQuery(); void search()
}, { flush: 'sync' })
watch(() => route.query.id, () => {
  const id = positiveIdParam(route.query.id)
  drawerId.value = scope.error.value || routeError.value ? null : id
})
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
  if (!canAck.value || batchAcking.value || !selected.value.length) return
  const ids = selected.value.map(alarm => alarm.id)
  const tenantId = app.tenantId
  batchAcking.value = true
  try {
    const { value } = await ElMessageBox.prompt(`将认领选中的 ${ids.length} 条告警，可填写处理备注（选填）`, '批量认领', {
      inputPlaceholder: '处理备注', confirmButtonText: '确认认领', cancelButtonText: '取消',
    })
    if (disposed || tenantId !== app.tenantId) return
    const res = await batchAck(ids, value || undefined)
    if (disposed) return
    ElMessage.success(`已认领 ${res.acked} 条告警`)
    await reload()
  } catch { /* 取消不提交，接口错误由请求层显示。 */ }
  finally { batchAcking.value = false }
}
async function exportExcel() {
  if (exporting.value || routeError.value) return
  exporting.value = true
  try { if (await scope.request('export', config => exportAlarms({ ...query }, config))) ElMessage.success('Excel 已导出') }
  finally { exporting.value = false }
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
