<template>
  <div class="ledger-page">
    <PageHeader title="资源台账" desc="每小时自动从 HCS 同步各云服务资源，负责人 / 业务系统 / 环境 / 备注由运维维护；新增、变更、释放全部留痕">
      <span v-if="summary?.last_sync" class="sync-info text-3">
        <i :class="summary.last_sync.status === 'SUCCESS' ? 'ok' : 'warn'" />最近同步 {{ fromNow(summary.last_sync.started_at) }}
        · {{ TASK_STATUS[summary.last_sync.status]?.label ?? summary.last_sync.status }}
      </span>
      <el-button v-perm="'ledger:sync'" :icon="Refresh" :loading="syncing" @click="onSync">立即同步</el-button>
      <el-button v-perm="'ledger:export'" type="primary" :icon="Download" :loading="exporting" @click="onExport">导出 Excel</el-button>
    </PageHeader>

    <!-- 汇总 -->
    <div v-if="summary" class="sum-grid">
      <div class="sum" @click="pickAll">
        <span class="k">台账资源</span><b class="num">{{ summary.total }}<small>个</small></b>
        <span class="s">{{ summary.type_count }} 类资源 · 核心 {{ summary.critical }} 个</span>
      </div>
      <div class="sum" @click="pickType('ecs_server')">
        <span class="k">计算</span><b class="num">{{ summary.compute.vcpus }}<small>vCPU</small></b>
        <span class="s">{{ summary.compute.ecs }} 台 ECS · 运行 {{ summary.ecs_status.ACTIVE ?? 0 }} · 关机 {{ summary.ecs_status.SHUTOFF ?? 0 }}<template v-if="summary.ecs_status.ERROR"> · <em class="err">故障 {{ summary.ecs_status.ERROR }}</em></template></span>
      </div>
      <div class="sum" @click="pickType('evs_volume')">
        <span class="k">块存储</span><b class="num">{{ (summary.storage_gb / 1024).toFixed(1) }}<small>TB</small></b>
        <span class="s">云硬盘合计 {{ summary.storage_gb.toLocaleString() }} GB</span>
      </div>
      <div class="sum warn" :class="{ active: filters.owner === NONE }" @click="toggleUnassigned">
        <span class="k">未分配负责人</span><b class="num">{{ summary.unassigned }}<small>个</small></b>
        <span class="s">点击筛选，补齐负责人便于告警指派</span>
      </div>
      <div class="sum warn" @click="idleVisible = true">
        <span class="k">疑似闲置</span><b class="num">{{ idleTotal }}<small>个</small></b>
        <span class="s">未挂载盘 {{ summary.idle.evs_available }} · 未绑定 EIP {{ summary.idle.eip_unbound }} · 关机 {{ summary.idle.ecs_stopped }}</span>
      </div>
      <div class="sum" @click="tab = 'changes'">
        <span class="k">近 7 天变更</span>
        <b class="num chg"><em class="c">+{{ summary.changes_7d.CREATED ?? 0 }}</em><em class="u">~{{ summary.changes_7d.UPDATED ?? 0 }}</em><em class="d">-{{ summary.changes_7d.DELETED ?? 0 }}</em></b>
        <span class="s">新增 / 变更 / 释放 · ECS 状态变更 {{ summary.changes_7d.STATUS ?? 0 }} · 重启 {{ summary.changes_7d.REBOOT ?? 0 }}</span>
      </div>
    </div>

    <div class="body">
      <!-- 分类导航 -->
      <aside class="hi-card nav">
        <div class="nav-item root" :class="{ active: sel.kind === 'all' }" @click="pickAll">
          <span>全部台账资源</span><em class="num">{{ ledgerTotal }}</em>
        </div>
        <template v-for="c in meta?.categories ?? []" :key="c.code">
          <div class="nav-cat" :class="{ active: sel.kind === 'category' && sel.code === c.code }" @click="pickCategory(c.code)">
            <el-icon><component :is="catIcon(c.code)" /></el-icon><span>{{ INSPECT_ONLY_CAT[c.code] ?? c.name }}</span>
            <em class="num">{{ c.total }}</em>
          </div>
          <div v-for="t in typesOf(c.code)" :key="t.code" class="nav-item" :class="{ active: sel.kind === 'type' && sel.code === t.code }" @click="pickType(t.code)">
            <span class="ellipsis">{{ t.name }}</span>
            <i v-if="t.abnormal" class="abn num">{{ t.abnormal }}</i>
            <em class="num">{{ t.total }}</em>
          </div>
        </template>
      </aside>

      <section class="hi-card main">
        <el-tabs v-model="tab" class="main-tabs">
          <el-tab-pane name="list">
            <template #label>资源清单 <span class="cnt">{{ total }}</span></template>
            <div class="filter-bar mb-12">
              <el-input v-model="filters.keyword" class="kw" clearable placeholder="名称 / IP / 资源 ID / 负责人 / 业务系统" :prefix-icon="Search" @keyup.enter="search" @clear="search" />
              <el-select v-model="filters.business" placeholder="业务系统" clearable filterable style="width: 140px" @change="search">
                <el-option v-for="b in meta?.options.business ?? []" :key="b" :label="b" :value="b" />
              </el-select>
              <el-select v-model="filters.owner" placeholder="负责人" clearable filterable style="width: 130px" @change="search">
                <el-option label="未分配" :value="NONE" />
                <el-option v-for="o in meta?.options.owner ?? []" :key="o" :label="o" :value="o" />
              </el-select>
              <el-select v-model="filters.env_type" placeholder="环境" clearable style="width: 100px" @change="search">
                <el-option v-for="(v, k) in ENV_TYPE" :key="k" :label="v.label" :value="k" />
              </el-select>
              <el-select v-if="sel.kind === 'type' && sel.code === 'ecs_server'" v-model="filters.status" placeholder="运行状态" clearable multiple collapse-tags style="width: 150px" @change="search">
                <el-option v-for="o in ECS_STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-select v-if="selInspected" v-model="filters.health" placeholder="健康度" clearable multiple collapse-tags style="width: 150px" @change="search">
                <el-option v-for="h in ['CRITICAL', 'MAJOR', 'WARNING', 'OK']" :key="h" :label="HEALTH[h].label" :value="h" />
              </el-select>
              <el-select v-model="filters.lifecycle" style="width: 100px" @change="search">
                <el-option label="在用" value="ACTIVE" /><el-option label="已释放" value="DELETED" /><el-option label="全部" value="ALL" />
              </el-select>
              <el-checkbox v-model="filters.critical" @change="search">仅核心</el-checkbox>
              <span class="flex-1" />
              <el-button text :icon="RefreshLeft" @click="resetFilters">重置</el-button>
            </div>

            <el-table v-loading="loading" :data="rows" row-key="id" :row-class-name="rowClass" @row-click="(r: Resource) => openDetail(r.id)">
              <el-table-column label="资源名称" min-width="200" fixed>
                <template #default="{ row }">
                  <div class="name"><span class="ellipsis">{{ row.name }}</span>
                    <el-tag v-if="row.critical" size="small" type="danger" effect="plain" class="core">核心</el-tag></div>
                  <div class="text-3 mono ellipsis">{{ row.resource_id }}</div>
                </template>
              </el-table-column>
              <el-table-column v-if="app.multiTenant" label="租户" width="92"><template #default="{ row }"><TenantTag :id="row.tenant_id" /></template></el-table-column>
              <el-table-column v-if="sel.kind !== 'type'" label="类型" width="130"><template #default="{ row }">{{ typeName(row.resource_type) }}</template></el-table-column>
              <el-table-column label="状态" width="96">
                <template #default="{ row }">
                  <el-tag v-if="row.lifecycle === 'DELETED'" type="info" size="small" effect="dark">已释放</el-tag>
                  <el-tag v-else :type="rawStatusType(row.status_raw)" size="small" effect="light">{{ statusText(row.resource_type, row.status_raw) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="selInspected" label="健康度" width="86"><template #default="{ row }"><HealthBadge :value="row.health" /></template></el-table-column>
              <el-table-column v-if="!isJob" label="规格" min-width="150" show-overflow-tooltip><template #default="{ row }">{{ row.spec ?? '-' }}</template></el-table-column>
              <el-table-column v-if="!isJob && !NO_IP.includes(sel.code)" label="IP 地址" width="128"><template #default="{ row }"><span class="mono">{{ row.ip ?? '-' }}</span></template></el-table-column>

              <!-- 类型专属列（后端下发） -->
              <el-table-column v-for="c in typeColumns" :key="c.key" :label="c.label" :width="c.width" show-overflow-tooltip>
                <template #default="{ row }">{{ fmtValue(c.key.split('.').pop() ?? '', pick(row as Resource, c.key)) }}</template>
              </el-table-column>
              <!-- 巡检对象的监控列 -->
              <el-table-column v-for="c in monitorColumns" :key="c.label" :label="c.label" :width="c.width" :min-width="c.minWidth">
                <template #default="{ row: r }">
                  <template v-if="c.usage">
                    <div class="usage">
                      <el-progress :percentage="Number(c.value(r as Resource)) || 0" :color="usageColor(Number(c.value(r as Resource)) || 0)" :stroke-width="6" :show-text="false" />
                      <b class="num" :style="{ color: usageColor(Number(c.value(r as Resource)) || 0) }">{{ c.value(r as Resource) ?? '-' }}%</b>
                    </div>
                    <div v-if="c.sub" class="text-3">{{ c.sub(r as Resource) }}</div>
                  </template>
                  <template v-else><span :class="{ mono: c.mono }">{{ c.value(r as Resource) ?? '-' }}</span><div v-if="c.sub" class="text-3">{{ c.sub(r as Resource) }}</div></template>
                </template>
              </el-table-column>

              <el-table-column label="业务系统" width="110" show-overflow-tooltip><template #default="{ row }">{{ row.business ?? '-' }}</template></el-table-column>
              <el-table-column label="负责人" width="90">
                <template #default="{ row }"><span v-if="row.owner">{{ row.owner }}</span><span v-else class="unassigned">未分配</span></template>
              </el-table-column>
              <el-table-column label="环境" width="70">
                <template #default="{ row }"><el-tag v-if="row.env_type" :type="ENV_TYPE[row.env_type]?.type" size="small" effect="plain">{{ ENV_TYPE[row.env_type]?.label }}</el-tag><span v-else class="text-3">-</span></template>
              </el-table-column>
              <el-table-column v-if="selInspected" label="活动告警" width="86" align="center">
                <template #default="{ row }">
                  <el-link v-if="row.active_alarms" type="danger" underline="never" @click.stop="$router.push(`/alarms?resource_ref=${row.id}`)"><b class="num">{{ row.active_alarms }}</b>&nbsp;条</el-link>
                  <span v-else class="text-3">-</span>
                </template>
              </el-table-column>
              <el-table-column v-if="!isJob" label="创建时间" width="104"><template #default="{ row }">{{ fmtTime(row.created_time, 'YYYY-MM-DD') }}</template></el-table-column>
              <el-table-column label="" width="60" fixed="right" align="center"><template #default="{ row }"><el-button link type="primary" @click.stop="openDetail(row.id)">详情</el-button></template></el-table-column>
              <template #empty><el-empty description="没有符合条件的资源" :image-size="70" /></template>
            </el-table>
            <div class="pager">
              <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100, 200]" background layout="total, sizes, prev, pager, next" />
            </div>
          </el-tab-pane>
          <el-tab-pane label="变更记录" name="changes" lazy>
            <ChangeTable ref="changeRef" :resource-type="sel.kind === 'type' ? sel.code : undefined"
                         :category="sel.kind === 'category' ? sel.code : 'ledger'" @open="openDetail" />
          </el-tab-pane>
        </el-tabs>
      </section>
    </div>

    <ResourceDrawer v-model="drawer.visible" :resource-id="drawer.id" :inspected-types="inspectedTypes"
                    :options="meta?.options ?? { owner: [], business: [] }" @saved="onSaved" />

    <!-- 疑似闲置清单 -->
    <el-dialog v-model="idleVisible" title="疑似闲置资源" width="720px">
      <p class="text-3 idle-tip">规则：云硬盘未挂载（available）、弹性公网 IP 未绑定（DOWN）、云服务器已关机（SHUTOFF）。确认无用后在 HCS 控制台释放，下次同步自动标记为“已释放”。</p>
      <el-table v-loading="idleLoading" :data="idleRows" size="small" max-height="420" @row-click="(r: Resource) => { idleVisible = false; openDetail(r.id) }">
        <el-table-column label="资源" min-width="180"><template #default="{ row }"><b>{{ row.name }}</b><div class="text-3">{{ typeName(row.resource_type) }}</div></template></el-table-column>
        <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag type="warning" size="small">{{ statusText(row.resource_type, row.status_raw) }}</el-tag></template></el-table-column>
        <el-table-column label="规格" prop="spec" min-width="120" />
        <el-table-column label="负责人" width="90"><template #default="{ row }">{{ row.owner ?? '未分配' }}</template></el-table-column>
        <el-table-column label="备注" prop="remark" min-width="160" show-overflow-tooltip />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Box, Coin, Connection, Cpu, DataAnalysis, Download, Files, Operation, Refresh, RefreshLeft, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, markRaw, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { exportLedger, ledgerSummary, listResources, resourceMeta, syncLedger } from '@/api'
import HealthBadge from '@/components/HealthBadge.vue'
import PageHeader from '@/components/PageHeader.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useTable } from '@/composables/useTable'
import type { LedgerSummary, Resource, ResourceMeta } from '@/types'
import { ENV_TYPE, HEALTH, IDLE_STATES, TASK_STATUS, rawStatusType, statusText, typeName, usageColor } from '@/utils/dict'
import { fmtTime, fromNow } from '@/utils/format'
import ChangeTable from './ChangeTable.vue'
import { MONITOR_COLUMNS, fmtValue, pick } from './columns'
import ResourceDrawer from './ResourceDrawer.vue'

const NONE = '__none__'
/** 没有 IP 概念的资源类型，列表不展示 IP 列 */
const NO_IP = ['evs_volume', 'vpc', 'vpc_subnet']
const ICONS: Record<string, unknown> = { compute: Cpu, storage: Files, network: Connection, database: Coin, bigdata: DataAnalysis, job: Operation, container: Box }
/** 只巡检、不进台账的分类（不参与“全部台账资源”计数、导出） */
const INSPECT_ONLY_CAT: Record<string, string> = { job: '作业（仅巡检）', container: '容器（仅巡检）' }
const INSPECT_ONLY_TYPES = ['dgc_realtime_job', 'dgc_batch_job', 'mrs_job', 'cce_workload']
/** ECS 运行状态筛选；“其他”= 变更中 / 未知等过渡状态 */
const ECS_OTHER = ['BUILD', 'REBOOT', 'HARD_REBOOT', 'RESIZE', 'VERIFY_RESIZE', 'REVERT_RESIZE', 'MIGRATING', 'REBUILD', 'SHELVED', 'SHELVED_OFFLOADED', 'UNKNOWN']
const ECS_STATUS_OPTIONS = [
  { value: 'ACTIVE', label: '运行中' }, { value: 'SHUTOFF', label: '已关机' }, { value: 'ERROR', label: '故障' },
  ...ECS_OTHER.map((v) => ({ value: v, label: statusText('ecs_server', v) })),
]
const app = useAppStore()
const catIcon = (c: string) => markRaw((ICONS[c] ?? Coin) as object)

const route = useRoute()
const router = useRouter()
const meta = ref<ResourceMeta | null>(null)
const summary = ref<LedgerSummary | null>(null)
const tab = ref(route.query.tab === 'changes' ? 'changes' : 'list')
const changeRef = ref<{ reload: () => void }>()

// ---- 分类选择：全部台账 / 某一分类 / 某一类型 ----
type Sel = { kind: 'all' | 'category' | 'type'; code: string }
const q = route.query
const sel = reactive<Sel>(typeof q.type === 'string' ? { kind: 'type', code: q.type }
  : typeof q.category === 'string' ? { kind: 'category', code: q.category } : { kind: 'all', code: 'ledger' })
const typesOf = (cat: string) => (meta.value?.types ?? []).filter((t) => t.category === cat)
const inspectedTypes = computed(() => (meta.value?.types ?? []).filter((t) => t.inspected).map((t) => t.code))
const ledgerTotal = computed(() => (meta.value?.types ?? []).filter((t) => t.ledger).reduce((n, t) => n + t.total, 0))
const selInspected = computed(() => (sel.kind === 'type' && inspectedTypes.value.includes(sel.code)) || (sel.kind === 'category' && !!INSPECT_ONLY_CAT[sel.code]))
/** 作业 / 容器工作负载：没有规格、IP、创建时间等台账字段 */
const isJob = computed(() => (sel.kind === 'category' && !!INSPECT_ONLY_CAT[sel.code]) || INSPECT_ONLY_TYPES.includes(sel.code))
const monitorColumns = computed(() => (sel.kind === 'type' ? MONITOR_COLUMNS[sel.code] ?? [] : []))
/** 类型专属列（后端下发）；有监控列时去掉重复的 metrics.* 列 */
const typeColumns = computed(() => {
  const cols = sel.kind === 'type' ? meta.value?.types.find((t) => t.code === sel.code)?.columns ?? [] : []
  if (!monitorColumns.value.length) return cols
  const dup = sel.code === 'cce_workload' ? ['attrs.cluster', 'attrs.namespace', 'attrs.kind'] : []
  return cols.filter((c) => !c.key.startsWith('metrics.') && !dup.includes(c.key))
})

// ---- 列表 ----
const initStatus = typeof q.status === 'string' && q.status ? (q.status === 'OTHER' ? ECS_OTHER : q.status.split(',')) : []
const filters = reactive({ keyword: typeof q.keyword === 'string' ? q.keyword : '', business: '', owner: q.unassigned === '1' ? NONE : '', env_type: '',
  health: [] as string[], status: initStatus as string[], lifecycle: 'ACTIVE' as 'ACTIVE' | 'DELETED' | 'ALL', critical: false })
const { page, size, total, rows, loading, load, search } = useTable<Resource, object>((p) => listResources({
  ...p,
  resource_type: sel.kind === 'type' ? sel.code : undefined,
  category: sel.kind === 'category' ? sel.code : sel.kind === 'all' ? 'ledger' : undefined,
  keyword: filters.keyword || undefined, business: filters.business || undefined, env_type: filters.env_type || undefined,
  owner: filters.owner && filters.owner !== NONE ? filters.owner : undefined, unassigned: filters.owner === NONE || undefined,
  health: selInspected.value && filters.health.length ? filters.health : undefined, lifecycle: filters.lifecycle,
  status: sel.kind === 'type' && sel.code === 'ecs_server' && filters.status.length ? filters.status : undefined,
  critical: filters.critical || undefined,
}), {})
const rowClass = ({ row }: { row: Resource }) => (row.lifecycle === 'DELETED' ? 'deleted-row' : '')

function syncQuery() {
  const query: Record<string, string> = {}
  if (sel.kind === 'type') query.type = sel.code
  if (sel.kind === 'category') query.category = sel.code
  if (tab.value === 'changes') query.tab = 'changes'
  router.replace({ query })
}
function select(s: Sel) {
  Object.assign(sel, s)
  if (!selInspected.value) filters.health = []
  if (sel.code !== 'ecs_server') filters.status = []
  syncQuery()
  search()
}
const pickAll = () => select({ kind: 'all', code: 'ledger' })
const pickCategory = (code: string) => select({ kind: 'category', code })
const pickType = (code: string) => select({ kind: 'type', code })
function toggleUnassigned() { filters.owner = filters.owner === NONE ? '' : NONE; tab.value = 'list'; search() }
function resetFilters() {
  Object.assign(filters, { keyword: '', business: '', owner: '', env_type: '', health: [], status: [], lifecycle: 'ACTIVE', critical: false })
  search()
}
watch(tab, syncQuery)

// ---- 详情 ----
const drawer = reactive({ visible: false, id: null as number | null })
function openDetail(id: number) { drawer.id = id; drawer.visible = true }
async function onSaved() { await Promise.all([load(), loadMeta()]); changeRef.value?.reload() }

// ---- 汇总 / 同步 / 导出 ----
const loadMeta = async () => { [meta.value, summary.value] = await Promise.all([resourceMeta(), ledgerSummary()]) }
const idleTotal = computed(() => (summary.value ? Object.values(summary.value.idle).reduce((a, b) => a + b, 0) : 0))
const syncing = ref(false)
async function onSync() {
  const scope = sel.kind === 'type' && !isJob.value ? sel.code : undefined
  await ElMessageBox.confirm(scope ? `立即从 HCS 同步「${typeName(scope)}」？` : '立即从 HCS 同步全部台账资源？（约 10~30 秒）', '立即同步', { type: 'info' })
  syncing.value = true
  try {
    const r = await syncLedger(scope)
    const st = r.stats as { created?: number; updated?: number; deleted?: number }
    ElMessage.success(`同步完成：${r.resource_total} 个资源，新增 ${st.created ?? 0} / 变更 ${st.updated ?? 0} / 释放 ${st.deleted ?? 0}`)
    await Promise.all([load(), loadMeta()])
    changeRef.value?.reload()
  } finally { syncing.value = false }
}
const exporting = ref(false)
async function onExport() {
  exporting.value = true
  try {
    const name = await exportLedger(sel.kind === 'type' ? { resource_type: sel.code } : sel.kind === 'category' ? { category: sel.code } : {})
    ElMessage.success(`已导出：${name}`)
  } finally { exporting.value = false }
}

// 疑似闲置
const idleVisible = ref(false)
const idleRows = ref<Resource[]>([])
const idleLoading = ref(false)
watch(idleVisible, async (v) => {
  if (!v) return
  idleLoading.value = true
  try {
    const res = await Promise.all(['evs_volume', 'eip', 'ecs_server'].map((t) => listResources({ resource_type: t, page_size: 500 })))
    idleRows.value = res.flatMap((p) => p.items).filter((r) => r.status_raw && IDLE_STATES.includes(r.status_raw))
  } finally { idleLoading.value = false }
})

onMounted(async () => {
  await Promise.all([loadMeta(), search()])
  if (typeof route.query.id === 'string') openDetail(Number(route.query.id))
})
watch(() => route.query.keyword, (k) => { if (typeof k === 'string' && k !== filters.keyword) { filters.keyword = k; tab.value = 'list'; search() } })
watch(() => route.query.id, (id) => { if (typeof id === 'string') openDetail(Number(id)) })
</script>

<style scoped lang="scss">
.sync-info { display: inline-flex; align-items: center; gap: 6px; margin-right: 8px;
  i { width: 7px; height: 7px; border-radius: 50%; &.ok { background: var(--hi-ok); } &.warn { background: var(--hi-p2); } } }
.sum-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 16px;
  .sum { display: flex; flex-direction: column; gap: 4px; padding: 14px 16px; background: #fff; border: 1px solid var(--hi-border);
    border-radius: var(--hi-radius); cursor: pointer; transition: border-color 0.2s, box-shadow 0.2s; min-width: 0;
    &:hover { box-shadow: var(--hi-shadow-hover); border-color: var(--el-color-primary-light-5); }
    &.active { border-color: var(--hi-p2); background: var(--hi-p2-bg); }
    .k { font-size: 13px; color: var(--hi-text-2); }
    b { font-size: 26px; line-height: 1.15; color: var(--hi-text-1); small { font-size: 12px; font-weight: 500; color: var(--hi-text-3); margin-left: 4px; } }
    .s { font-size: 12px; color: var(--hi-text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    &.warn b { color: var(--hi-p2); }
    .chg { display: flex; gap: 10px; font-size: 22px; em { font-style: normal; } .c { color: var(--hi-ok); } .u { color: var(--el-color-primary); } .d { color: var(--hi-p1); } }
    .err { font-style: normal; color: var(--hi-p1); } } }
.body { display: flex; gap: 16px; align-items: flex-start; }
.nav { width: 228px; flex: none; padding: 10px 8px; position: sticky; top: 0;
  .nav-item, .nav-cat { display: flex; align-items: center; gap: 8px; height: 34px; padding: 0 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--hi-text-2);
    em { margin-left: auto; font-style: normal; font-size: 12px; color: var(--hi-text-3); }
    &:hover { background: var(--hi-bg-soft); }
    &.active { background: var(--hi-sider-active-bg); color: var(--el-color-primary); font-weight: 600; em { color: var(--el-color-primary); } } }
  .root { font-weight: 600; color: var(--hi-text-1); margin-bottom: 4px; }
  .nav-cat { margin-top: 6px; font-weight: 600; color: var(--hi-text-1); }
  .nav-item { padding-left: 34px;
    .abn { font-style: normal; min-width: 18px; height: 16px; padding: 0 5px; border-radius: 8px; background: var(--hi-p1-bg); color: var(--hi-p1); font-size: 11px; display: grid; place-items: center; } } }
.main { flex: 1; min-width: 0; padding-top: 6px;
  .main-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }
  .cnt { margin-left: 4px; font-size: 12px; color: var(--hi-text-3); font-weight: 400; } }
.kw { width: 280px !important; }
.name { display: flex; align-items: center; gap: 6px; font-weight: 600; .core { flex: none; } }
.unassigned { color: var(--hi-p2); font-size: 12px; }
.usage { display: flex; align-items: center; gap: 8px; .el-progress { flex: 1; } b { width: 44px; text-align: right; font-size: 13px; } }
.idle-tip { margin: -6px 0 12px; line-height: 1.7; }
:deep(.el-table__row) { cursor: pointer; }
:deep(.deleted-row) { color: var(--hi-text-3); .name { text-decoration: line-through; font-weight: 500; } }
</style>
