<template>
  <div class="pod-page">
    <PageHeader title="容器事件" :refresh="reloadAll" :refreshing="refreshing" />
    <template v-if="tab !== 'clusters'">
    <p class="text-3">次数基于本系统已采集的事件；首次采集和中断期间的历史可能不完整，未知不代表 0。</p>
    <el-alert v-if="scopeError" :title="scopeError" type="error" :closable="false" show-icon />
    <p v-if="query.tenant_id || query.project_ref || query.unknown_project" class="text-3">
      报表限定：租户 {{ query.tenant_id ?? '当前范围' }} · {{ query.unknown_project ? '历史未知项目' : `项目#${query.project_ref ?? '全部'}` }}
    </p>

    <!-- 统计卡片：点击快速筛选 -->
    <div class="stats">
      <StatCard title="事件" :value="stats?.total ?? '-'" :sub="`严重 ${stats?.critical ?? 0} · 进行中 ${stats?.ongoing ?? 0}`" :icon="Bell" color="#1d63f0" @click="quick()" />
      <StatCard title="容器重启" :value="stats ? stats.restarts ?? '未知' : '-'" unit="次" :sub="`OOM 事件 ${typeCount('OOM_KILLED')} 条（不等于重启次数）`" :icon="RefreshRight" color="#F57C00" @click="quick(['RESTART', 'OOM_KILLED'])" />
      <StatCard title="CrashLoopBackOff" :value="stats ? (stats.pods.fresh ? stats.pods.crashloop : '未知') : '-'" unit="个 Pod" :sub="`${hours} 小时内 ${typeCount('CRASH_LOOP')} 次`" :icon="WarningFilled" color="#B3261E" @click="quick(['CRASH_LOOP'])" />
      <StatCard title="背压" :value="typeCount('BACKPRESSURE')" unit="次" :sub="`进行中 ${ongoingBp} · 探针 ${probeCount} 个`" :icon="Odometer" color="#7B1FA2" @click="quick(['BACKPRESSURE'])" />
      <StatCard title="Pod 就绪（当前）" :value="readyValue" :sub="snapshotSummary" :icon="Box" color="#0e9f8f" @click="tab = 'workloads'" />
    </div>
    <div v-if="tab !== 'clusters' && stats" class="snapshot-scopes" aria-label="Pod 快照时效">
      <span v-for="scope in stats.pods.scopes ?? []" :key="`${scope.tenant_id}:${scope.project_ref}:${scope.cluster_id}`" class="snapshot-scope">
        <span>{{ scope.tenant_name ?? '未知租户' }} / 项目 {{ scope.project_ref ?? '未知' }} / {{ scope.cluster_name }}</span>
        <el-tag size="small" :type="scope.freshness.status === 'FRESH' ? 'success' : scope.freshness.status === 'UNKNOWN' ? 'info' : 'warning'">{{ freshnessLabel(scope.freshness.status) }}</el-tag>
        <span class="text-3" :title="fmtTime(scope.freshness.last_observed_at, 'YYYY-MM-DD HH:mm:ss')">最近观测 {{ observedAge(scope.freshness) }}</span>
      </span>
      <span v-if="!stats.pods.scopes?.length" class="text-3">暂无 Pod 快照</span>
    </div>

    <div class="charts">
      <div class="hi-card">
        <div class="card-title">事件趋势<span class="text-3">（按{{ stats?.trend.step === 'day' ? '天' : '小时' }}）</span></div>
        <EChart :option="trendOption" height="240px" />
      </div>
      <div class="hi-card">
        <div class="card-title">事件类型分布<span class="text-3">点击筛选</span></div>
        <div class="types">
          <div v-for="t in stats?.by_type ?? []" :key="t.type" class="type" :class="{ on: query.event_type.includes(t.type) }" :style="{ '--c': t.color }" @click="quick([t.type])">
            <span class="t-name">{{ t.name }}</span>
            <span class="t-bar"><i :style="{ width: `${(t.count / maxType) * 100}%` }" /></span>
            <b class="num">{{ t.count }}</b>
          </div>
          <el-empty v-if="stats && !stats.by_type.length" description="时间范围内没有事件" :image-size="60" />
        </div>
      </div>
    </div>

    </template>

    <div class="hi-card">
      <div v-if="tab !== 'clusters'" class="table-toolbar table-heading">
        <el-select v-model="hours" class="w-130" aria-label="事件时间范围" @change="reloadAll">
          <el-option v-for="h in HOURS" :key="h.value" :value="h.value" :label="h.label" />
        </el-select>
        <div class="table-actions">
          <el-button v-perm="'podevent:config'" type="primary" plain :icon="Download" :loading="collecting" @click="collect">立即采集</el-button>
        </div>
      </div>
      <el-tabs v-model="tab">
        <!-- 事件列表 -->
        <el-tab-pane name="events" :label="`事件列表（${total}）`">
          <div class="filter-bar mb-12">
            <el-select v-model="query.cluster_id" clearable placeholder="CCE 集群" class="w-150" @change="onCluster">
              <el-option v-for="c in clusterOptions" :key="c.cluster_id" :value="c.cluster_id" :label="c.cluster_name">
                <span>{{ c.cluster_name }}</span><TenantTag v-if="app.multiTenant && c.scopeCount === 1" :id="c.tenant_id" :name="c.tenant_name" class="opt-tag" />
              </el-option>
            </el-select>
            <el-select v-model="query.namespace" clearable filterable placeholder="命名空间" class="w-130" @change="search">
              <el-option v-for="n in nsOptions" :key="n" :value="n" :label="n" />
            </el-select>
            <el-select v-model="query.workload_kind" clearable placeholder="工作负载类型" class="w-150" @change="search">
              <el-option v-for="kind in workloadKinds" :key="kind" :value="kind" :label="kind" />
            </el-select>
            <el-select :model-value="selectedWorkload" clearable filterable placeholder="工作负载" class="w-170" @change="selectWorkload">
              <el-option v-for="w in wlOptions" :key="workloadKey(w)" :value="workloadKey(w)" :label="w.name">
                <span>{{ w.name }}</span><span class="text-3 opt-sub">{{ w.namespace }} · {{ w.kind }} · 项目 {{ w.project_ref ?? '未知' }}</span>
              </el-option>
            </el-select>
            <el-select v-model="query.event_type" multiple collapse-tags collapse-tags-tooltip clearable placeholder="事件类型" class="w-170" @change="search">
              <el-option v-for="t in options?.event_types ?? []" :key="t.code" :value="t.code" :label="t.name"><span class="dot" :style="{ background: t.color }" />{{ t.name }}</el-option>
            </el-select>
            <el-select v-model="query.level" multiple collapse-tags clearable placeholder="级别" class="w-110" @change="search">
              <el-option v-for="(l, k) in POD_LEVEL" :key="k" :value="k" :label="l.label" />
            </el-select>
            <el-select v-model="query.source" clearable placeholder="来源" class="w-110" @change="search">
              <el-option v-for="(n, k) in POD_SOURCE" :key="k" :value="k" :label="n" />
            </el-select>
            <el-input v-model="query.keyword" clearable placeholder="Pod / 原因 / 消息" :prefix-icon="Search" class="w-170" @keyup.enter="search" @clear="search" />
            <el-checkbox v-model="query.ongoing" @change="search">只看进行中</el-checkbox>
            <el-button type="primary" @click="search">查询</el-button>
            <el-button @click="reset">重置</el-button>
          </div>
          <el-table v-loading="loading" :data="rows" row-key="id" class="ev-table" @row-click="(r: PodEvent) => openPod(r)">
            <el-table-column label="级别" width="76">
              <template #default="{ row }"><span class="lvl" :style="{ '--c': POD_LEVEL[row.level]?.color }"><i />{{ POD_LEVEL[row.level]?.label ?? row.level }}</span></template>
            </el-table-column>
            <el-table-column label="事件" min-width="215">
              <template #default="{ row }">
                <div class="flex gap-6"><span class="ev-type" :style="{ '--c': row.color }">{{ row.type_name }}</span><span v-if="row.count > 1" class="cnt">×{{ row.count }}</span>
                  <PodSignalBadge :event="row as PodEvent" /></div>
                <div class="text-3 mono sm">{{ row.reason }}</div>
              </template>
            </el-table-column>
            <el-table-column label="工作负载 / Pod" min-width="230">
              <template #default="{ row }">
                <div class="ellipsis"><b>{{ row.workload || '-' }}</b><span class="text-3 sm"> {{ row.workload_kind }}</span></div>
                <div class="text-3 mono sm ellipsis">{{ row.pod }}<template v-if="row.container"> / {{ row.container }}</template></div>
              </template>
            </el-table-column>
            <el-table-column label="集群 / 命名空间" min-width="160">
              <template #default="{ row }">
                <div class="flex gap-6"><TenantTag v-if="app.multiTenant" :id="row.tenant_id" :name="row.tenant_name" /><span class="ellipsis">{{ row.cluster_name }}</span></div>
                <div class="text-3 sm">{{ row.namespace }}<template v-if="row.node"> · {{ row.node }}</template></div>
              </template>
            </el-table-column>
            <el-table-column label="消息" min-width="240" show-overflow-tooltip>
              <template #default="{ row }"><span class="msg">{{ row.message }}</span>
                <span v-if="row.peak != null" class="peak">峰值 {{ row.peak }}{{ row.detail?.unit ?? '' }}</span></template>
            </el-table-column>
            <el-table-column label="时间" width="150">
              <template #default="{ row }">
                {{ fmtTime(row.last_time, 'MM-DD HH:mm:ss') }}
                <div class="text-3 sm">{{ row.duration_sec ? `持续 ${humanize(row.duration_sec)}` : `首次 ${fmtTime(row.first_time, 'MM-DD HH:mm')}` }}</div>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="90"><template #default="{ row }"><span class="text-2 sm">{{ POD_SOURCE[row.source] ?? row.source }}</span></template></el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size" :total="total" :page-sizes="[20, 50, 100]"
                           layout="total, sizes, prev, pager, next" background @current-change="loadEvents" @size-change="search" />
          </div>
        </el-tab-pane>

        <!-- 工作负载 Top -->
        <el-tab-pane name="workloads" label="工作负载 Top">
          <el-table :data="stats?.top_workloads ?? []" size="default">
            <el-table-column type="index" width="50" label="#" />
            <el-table-column label="工作负载" min-width="200"><template #default="{ row }"><b>{{ row.workload }}</b><div class="text-3 sm">{{ row.namespace }}</div></template></el-table-column>
            <el-table-column label="集群" min-width="150"><template #default="{ row }"><div class="flex gap-6"><TenantTag :id="row.tenant_id" :name="row.tenant_name" />{{ row.cluster_name }}</div></template></el-table-column>
            <el-table-column label="事件" width="70" align="center"><template #default="{ row }"><b class="num">{{ row.events }}</b></template></el-table-column>
            <el-table-column label="重启" width="70" align="center"><template #default="{ row }"><b class="num" :class="{ warn: row.restarts }">{{ row.restarts ?? '未知' }}</b></template></el-table-column>
            <el-table-column label="背压" width="70" align="center"><template #default="{ row }"><b class="num" :class="{ bp: row.backpressure }">{{ row.backpressure }}</b></template></el-table-column>
            <el-table-column label="严重" width="70" align="center"><template #default="{ row }"><b class="num" :class="{ crit: row.critical }">{{ row.critical }}</b></template></el-table-column>
            <el-table-column label="类型构成" min-width="280">
              <template #default="{ row }"><span v-for="t in row.types" :key="t.type" class="mini" :style="{ '--c': typeColor(t.type) }">{{ t.name }} {{ t.count }}</span></template>
            </el-table-column>
            <el-table-column label="最近" width="110"><template #default="{ row }">{{ fromNow(row.last_time) }}</template></el-table-column>
            <el-table-column label="操作" width="96" fixed="right">
              <template #default="{ row }"><el-button link type="primary" @click="byWorkload(row as WorkloadScopeRow)">查看事件</el-button></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 背压探针 -->
        <el-tab-pane name="probes" :label="`背压探针（${probeCount}）`">
          <ProbePanel ref="probeRef" :options="options" @count="(n: number) => (probeCount = n)" />
        </el-tab-pane>
        <el-tab-pane name="clusters" label="集群与节点">
          <CceHealthPanel v-if="tab === 'clusters'" ref="healthRef" />
        </el-tab-pane>
      </el-tabs>
    </div>

    <PodTimelineDrawer v-model="drawer.visible" :pod-key="drawer.key" :cluster-name="drawer.cluster" :event-id="drawer.eventId" />
  </div>
</template>

<script setup lang="ts">
import { Bell, Box, Download, Odometer, RefreshRight, Search, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElNotification } from 'element-plus'
import { computed, h, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { collectPodEvents, listPodEvents, listProbes, podEventOptions, podEventStats } from '@/api'
import EChart from '@/components/EChart.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import StatCard from '@/components/StatCard.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import type { PodEvent, PodEventOptions, PodEventStats, PodSnapshotFreshness } from '@/types'
import { POD_LEVEL, POD_SOURCE } from '@/utils/dict'
import { fmtTime, fromNow, humanize } from '@/utils/format'
import PodTimelineDrawer from './PodTimelineDrawer.vue'
import PodSignalBadge from './PodSignalBadge.vue'
import { groupPodClusters } from './cluster-options'
import ProbePanel from './ProbePanel.vue'
import CceHealthPanel from './CceHealthPanel.vue'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const HOURS = [
  { value: 1, label: '最近 1 小时' }, { value: 6, label: '最近 6 小时' }, { value: 24, label: '最近 24 小时' },
  { value: 72, label: '最近 3 天' }, { value: 168, label: '最近 7 天' },
]
const hours = ref(24)
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'events')
const stats = ref<PodEventStats | null>(null)
const options = ref<PodEventOptions | null>(null)
const rows = ref<PodEvent[]>([])
const total = ref(0)
const eventRequest = useLatestRequest()
const statsRequest = useLatestRequest()
const optionsRequest = useLatestRequest()
const loading = eventRequest.loading
const probeRef = ref<{ reload: () => Promise<void> } | null>(null)
const healthRef = ref<{ reload: () => Promise<void> } | null>(null)
const healthRefreshing = ref(false)
const refreshing = computed(() => tab.value === 'clusters' ? healthRefreshing.value : loading.value || statsRequest.loading.value)
const collecting = ref(false)
const probeCount = ref(0)
const ongoingBp = ref(0)
const q = route.query
const str = (v: unknown) => (typeof v === 'string' && v ? v : '')
function readScope(value: Record<string, unknown>) {
  let error = ''
  const id = (key: string) => {
    const raw = value[key]
    if (raw == null || raw === '') return undefined
    if (typeof raw !== 'string' || !/^[1-9]\d*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
      error = `报表链接 ${key} 无效，请重置筛选；未回退到全部范围`
      return undefined
    }
    return Number(raw)
  }
  const tenant_id = id('tenant_id'), project_ref = id('project_ref')
  const unknown_project = value.unknown_project === '1' || value.unknown_project === 'true'
  if (value.unknown_project != null && value.unknown_project !== ''
    && !['1', '0', 'true', 'false'].includes(String(value.unknown_project))) error = '报表未知项目标记无效，请重置筛选'
  for (const key of ['cluster_id', 'namespace', 'workload', 'workload_kind', 'pod']) {
    if (value[key] != null && typeof value[key] !== 'string') error = `报表 ${key} 参数无效，请重置筛选`
  }
  if (project_ref && unknown_project) error = '报表链接同时指定已知与未知项目，请重置筛选'
  let namespace = str(value.namespace), workload = str(value.workload)
  if (workload.includes('/')) {
    const parts = workload.split('/')
    if (parts.length !== 2 || !parts.every(Boolean) || (namespace && namespace !== parts[0])) {
      error = '报表工作负载路径与命名空间不一致，请重置筛选'
    } else { namespace = parts[0]; workload = parts[1] }
  }
  return { tenant_id, project_ref, unknown_project, namespace, workload,
    workload_kind: str(value.workload_kind), cluster_id: str(value.cluster_id), keyword: str(value.pod), error }
}
const initialScope = readScope(q)
const scopeError = ref(initialScope.error)
const query = reactive({
  tenant_id: initialScope.tenant_id, project_ref: initialScope.project_ref, unknown_project: initialScope.unknown_project,
  cluster_id: initialScope.cluster_id, namespace: initialScope.namespace, workload: initialScope.workload,
  workload_kind: initialScope.workload_kind, keyword: initialScope.keyword,
  event_type: str(q.event_type) ? str(q.event_type).split(',') : [] as string[], level: [] as string[], source: '', ongoing: q.ongoing === '1',
  page: 1, page_size: 20,
})

const identityParams = () => ({ tenant_id: query.tenant_id, project_ref: query.project_ref,
  unknown_project: query.unknown_project || undefined })
const filterParams = () => ({ ...identityParams(), cluster_id: query.cluster_id || undefined,
  namespace: query.namespace || undefined, workload: query.workload || undefined, workload_kind: query.workload_kind || undefined })
function validScope() {
  if (scopeError.value) return false
  if (query.tenant_id != null && app.tenantId != null && query.tenant_id !== app.tenantId) {
    scopeError.value = '报表租户与顶栏范围不同，请选择相符租户或重置筛选；未混合查询'
    return false
  }
  return true
}
async function loadPods() {
  if (!validScope()) return false
  const params = identityParams()
  return optionsRequest.run(() => podEventOptions(params), value => { options.value = value })
}

async function loadStats() {
  if (!validScope()) return false
  const params = { hours: hours.value, ...filterParams() }
  return statsRequest.run(() => Promise.all([
    podEventStats(params),
    listPodEvents({ ...params, event_type: ['BACKPRESSURE'], ongoing: true, page: 1, page_size: 1 }),
  ]), ([statsResult, backpressure]) => { stats.value = statsResult; ongoingBp.value = backpressure.total })
}
async function loadEvents() {
  if (!validScope()) return false
  const params = {
    hours: hours.value, ...filterParams(),
    keyword: query.keyword.trim() || undefined, event_type: query.event_type.length ? [...query.event_type] : undefined,
    level: query.level.length ? [...query.level] : undefined, source: query.source || undefined, ongoing: query.ongoing || undefined,
    page: query.page, page_size: query.page_size,
  }
  return eventRequest.run(() => listPodEvents(params), result => { rows.value = result.items; total.value = result.total })
}
async function reloadAll() {
  if (tab.value === 'clusters') {
    healthRefreshing.value = true
    try { await healthRef.value?.reload() }
    finally { healthRefreshing.value = false }
    return
  }
  await Promise.all([loadStats(), loadEvents(), loadPods(), tab.value === 'probes' ? probeRef.value?.reload() : undefined])
}
watch(tab, (active, previous) => {
  if (previous === 'clusters' && active !== 'clusters') {
    // A health-only landing has not loaded any Pod data. Wait for the newly
    // active pane's refs, and do not reload if the user has already switched again.
    void nextTick().then(() => tab.value === active ? reloadAll() : undefined).catch(() => undefined)
  }
})
function invalidateScopeData() {
  eventRequest.invalidate(); statsRequest.invalidate(); optionsRequest.invalidate()
  rows.value = []; total.value = 0; stats.value = null; ongoingBp.value = 0; options.value = null
  drawer.visible = false
}
function syncScopeURL() {
  const next = { ...route.query }
  for (const key of ['tenant_id', 'project_ref', 'unknown_project', 'cluster_id', 'namespace', 'workload', 'workload_kind', 'pod']) delete next[key]
  if (query.tenant_id) next.tenant_id = String(query.tenant_id)
  if (query.project_ref) next.project_ref = String(query.project_ref)
  if (query.unknown_project) next.unknown_project = '1'
  if (query.cluster_id) next.cluster_id = query.cluster_id
  if (query.namespace) next.namespace = query.namespace
  if (query.workload) next.workload = query.workload
  if (query.workload_kind) next.workload_kind = query.workload_kind
  if (query.keyword) next.pod = query.keyword
  void router.replace({ query: next })
}
function clearReportScope() {
  query.tenant_id = undefined; query.project_ref = undefined; query.unknown_project = false; query.workload_kind = ''
  scopeError.value = ''
}
function search() {
  query.page = 1
  reloadAll()
}
function reset() {
  clearReportScope()
  invalidateScopeData()
  Object.assign(query, { cluster_id: '', namespace: '', workload: '', keyword: '', event_type: [], level: [], source: '', ongoing: false, page: 1 })
  syncScopeURL()
  reloadAll()
}
function onCluster() {
  clearReportScope()
  query.namespace = ''; query.workload = ''; query.keyword = ''
  invalidateScopeData()
  syncScopeURL()
  search()
}
/** 统计卡片 / 类型分布点击：按类型筛选 */
function quick(types: string[] = []) {
  query.event_type = types
  query.ongoing = false
  tab.value = 'events'
  search()
}
type WorkloadRow = PodEventOptions['workloads'][number]
type WorkloadScopeRow = { workload: string; namespace: string; tenant_id?: number | null; project_ref?: number | null;
  cluster_id?: string; workload_kind?: string }
function byWorkload(row: WorkloadScopeRow) {
  if (row.tenant_id == null || row.project_ref === undefined || !row.cluster_id || !row.workload_kind) {
    ElMessage.warning('工作负载记录缺少精确范围，未查询其他项目')
    return
  }
  invalidateScopeData()
  Object.assign(query, { tenant_id: row.tenant_id, project_ref: row.project_ref ?? undefined,
    unknown_project: row.project_ref === null, cluster_id: row.cluster_id,
    workload: row.workload, namespace: row.namespace, workload_kind: row.workload_kind, event_type: [], ongoing: false })
  scopeError.value = ''
  tab.value = 'events'
  syncScopeURL()
  search()
}
onMounted(async () => {
  const p = await listProbes().catch(() => [])
  probeCount.value = p.length
  // The conditionally mounted health panel loads itself; avoid a second initial request.
  if (tab.value !== 'clusters') await reloadAll()
})

const readyValue = computed(() => !stats.value ? '-' : stats.value.pods.fresh
  ? `${stats.value.pods.ready}/${stats.value.pods.fresh}` : '未知')
const snapshotSummary = computed(() => !stats.value ? '未采集' :
  `未就绪 ${stats.value.pods.fresh ? stats.value.pods.not_ready : '未知'} · 过期 ${stats.value.pods.stale ?? 0} · 未知 ${stats.value.pods.unknown ?? 0}`)
const freshnessLabel = (status: PodSnapshotFreshness['status']) =>
  ({ FRESH: '当前', STALE: '已过期', UNKNOWN: '未知', PARTIAL: '不完整', DELETED: '已删除' })[status]
const observedAge = (value: PodSnapshotFreshness) => value.age_sec == null ? '时间未知' : `${humanize(value.age_sec)}前`

const clusterOptions = computed(() => groupPodClusters(options.value?.clusters ?? []))
const nsOptions = computed(() => {
  const o = options.value
  if (!o) return []
  return query.cluster_id ? o.namespaces_by_cluster[query.cluster_id] ?? [] : o.namespaces
})
const wlOptions = computed(() => [...new Map((options.value?.workloads ?? [])
  .filter((w) => (!query.cluster_id || w.cluster_id === query.cluster_id) && (!query.namespace || w.namespace === query.namespace)
    && (!query.workload_kind || w.kind === query.workload_kind))
  .map(w => [workloadKey(w), w])).values()])
const workloadKinds = computed(() => [...new Set((options.value?.workloads ?? []).map(w => w.kind))].sort())
const workloadKey = (w: WorkloadRow) => `${w.tenant_id}:${w.project_ref}:${w.cluster_id}:${w.namespace}:${w.kind}:${w.name}`
const selectedWorkload = computed(() => {
  const matched = wlOptions.value.filter(w => w.name === query.workload && (!query.workload_kind || w.kind === query.workload_kind))
  return matched.length === 1 ? workloadKey(matched[0]) : query.workload || undefined
})
function selectWorkload(value: string) {
  if (!value) { query.workload = ''; search(); return }
  const found = wlOptions.value.find(w => workloadKey(w) === value)
  if (found) byWorkload({ ...found, workload: found.name, workload_kind: found.kind })
}
const typeCount = (t: string) => stats.value ? stats.value.by_type.find((x) => x.type === t)?.count ?? 0 : '-'
const typeColor = (t: string) => options.value?.event_types.find((x) => x.code === t)?.color ?? '#8c8c8c'
const maxType = computed(() => Math.max(1, ...(stats.value?.by_type.map((t) => t.count) ?? [1])))

const trendOption = computed(() => {
  const t = stats.value?.trend
  const bar = (name: string, data: (number | null)[], color: string) => ({ name, type: 'bar', stack: 'ev', barMaxWidth: 18, data, itemStyle: { color } })
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { right: 0, top: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: '#4e5969' } },
    grid: { left: 4, right: 8, top: 32, bottom: 0, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'category', data: (t?.labels ?? []).map((l) => l.slice(t?.step === 'day' ? 0 : 6)), axisTick: { show: false },
      axisLine: { lineStyle: { color: '#e5e6eb' } }, axisLabel: { color: '#86909c' } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f0f1f3', type: 'dashed' } }, axisLabel: { color: '#86909c' } },
    series: [
      bar('容器重启 / OOM', t?.series.restart ?? [], '#F57C00'),
      bar('背压', t?.series.backpressure ?? [], '#7B1FA2'),
      { ...bar('其他事件', t?.series.other ?? [], '#9fb4d6'), itemStyle: { color: '#9fb4d6', borderRadius: [3, 3, 0, 0] } },
    ],
  }
})

const drawer = reactive({ visible: false, key: '', cluster: '', eventId: undefined as number | undefined })
watch(() => app.tenantId, () => {
  clearReportScope()
  Object.assign(query, { cluster_id: '', namespace: '', workload: '', keyword: '', page: 1 })
  invalidateScopeData()
  syncScopeURL()
  void nextTick().then(reloadAll).catch(() => undefined)
}, { flush: 'sync' })
watch(() => route.query, value => {
  const next = readScope(value)
  const key = (s: typeof initialScope | typeof query) => JSON.stringify([
    s.tenant_id, s.project_ref, s.unknown_project, s.cluster_id, s.namespace, s.workload, s.workload_kind, s.keyword])
  if (key(next) === key(query) && next.error === scopeError.value) return
  const { error, ...fields } = next
  Object.assign(query, fields, { page: 1 }); scopeError.value = error
  invalidateScopeData()
  void reloadAll().catch(() => undefined)
})
function openPod(r: PodEvent) {
  if (!r.pod) return ElMessage.info('该事件不属于具体 Pod（如节点 / 工作负载级事件）')
  drawer.eventId = r.id
  drawer.key = `${r.cluster_id}/${r.namespace}/${r.pod}`
  drawer.cluster = r.cluster_name
  drawer.visible = true
}

async function collect() {
  if (collecting.value) return
  collecting.value = true
  try {
    const r = await collectPodEvents()
    ElNotification({
      title: `容器事件采集：${r.status === 'SUCCESS' ? '完成' : r.status === 'SKIPPED' ? '已跳过（未执行）' : '未全部成功'}`,
      type: r.status === 'SUCCESS' ? 'success' : 'warning',
      message: h('div', r.runs.map((x) => h('div', { style: 'line-height:1.8' }, [
        h('b', `${x.tenant_name}　`), x.status === 'SUCCESS' ? `新增 / 更新事件 ${x.events} 条` : `失败：${x.error_summary ?? x.status}`,
      ]))),
    })
    await reloadAll()
  } finally { collecting.value = false }
}
</script>

<style scoped lang="scss">
.w-130 { width: 130px; }
.filter-bar { .w-110 { width: 110px; } .w-130 { width: 130px; } .w-150 { width: 150px; } .w-170 { width: 170px; } }
.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 16px; }
.snapshot-scopes { display: flex; flex-wrap: wrap; gap: 8px 20px; margin-bottom: 16px; }
.snapshot-scope { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 12px; }
.charts { display: grid; grid-template-columns: 1.65fr 1fr; gap: 16px; margin-bottom: 16px; }
.card-title { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; font-weight: 600; font-size: 15px; .text-3 { font-weight: 400; font-size: 12px; } }
.types { display: flex; flex-direction: column; gap: 2px; max-height: 240px; overflow-y: auto;
  .type { display: grid; grid-template-columns: 120px 1fr 40px; align-items: center; gap: 10px; padding: 5px 8px; border-radius: 6px; cursor: pointer; font-size: 13px;
    &:hover, &.on { background: color-mix(in srgb, var(--c) 8%, #fff); }
    &.on .t-name { color: var(--c); font-weight: 600; }
    .t-bar { height: 8px; border-radius: 4px; background: var(--hi-bg-soft); i { display: block; height: 100%; border-radius: 4px; background: var(--c); min-width: 4px; } }
    b { text-align: right; } } }
.filter-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.mb-12 { margin-bottom: 12px; }
.opt-tag { margin-left: 8px; } .opt-sub { margin-left: 8px; font-size: 12px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }
.ev-table { cursor: pointer; }
.lvl { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--c); font-weight: 600; i { width: 7px; height: 7px; border-radius: 50%; background: var(--c); } }
.ev-type { padding: 0 8px; height: 20px; line-height: 20px; border-radius: 4px; font-size: 12px; font-weight: 600; color: var(--c); background: color-mix(in srgb, var(--c) 10%, #fff); white-space: nowrap; }
.cnt { font-size: 12px; font-weight: 700; color: var(--hi-p2, #f57c00); }
.gap-6 { gap: 6px; }
.sm { font-size: 12px; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.msg { font-size: 13px; color: var(--hi-text-2); }
.peak { margin-left: 8px; padding: 0 6px; border-radius: 4px; font-size: 12px; color: #7B1FA2; background: #f3e5f5; }
.pager { display: flex; justify-content: flex-end; margin-top: 14px; }
.warn { color: #F57C00; } .bp { color: #7B1FA2; } .crit { color: #D93026; }
.mini { display: inline-block; margin: 2px 6px 2px 0; padding: 0 6px; border-radius: 4px; font-size: 12px; line-height: 20px; color: var(--c); background: color-mix(in srgb, var(--c) 9%, #fff); }
@media (max-width: 1199px) {
  .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .charts { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 767px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
}
</style>
