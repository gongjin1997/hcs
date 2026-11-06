<template>
  <div v-loading="loading" class="dashboard">
    <PageHeader title="运行总览" :refresh="load" :refreshing="loading" />
    <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" class="mb-16">
      <el-button text @click="load">重试</el-button>
    </el-alert>

    <template v-if="data">
      <el-row :gutter="16">
        <el-col :xs="24" :lg="6">
          <div class="hi-card score-card">
            <div class="hi-card__title"><el-tooltip content="按资源告警级别加权，核心资源权重为 3；无数据不计入评分。" placement="top"><span>平台健康分</span></el-tooltip></div>
            <EChart :option="gaugeOption" height="168px" />
            <div class="score-foot">
              <span>巡检对象 <b class="num">{{ data.kpis.resources }}</b></span>
              <span>异常 <b class="num danger">{{ data.kpis.abnormal_resources }}</b></span>
              <span v-if="data.kpis.unknown_resources">无数据 <b class="num">{{ data.kpis.unknown_resources }}</b></span>
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :lg="18">
          <el-row :gutter="16" class="kpis">
            <el-col :xs="12" :md="6"><StatCard title="巡检对象" :value="data.kpis.resources" unit="个" :icon="Coin"
              :sub="`其中异常 ${data.kpis.abnormal_resources} 个`" /></el-col>
            <el-col :xs="12" :md="6"><StatCard title="活动告警" :value="data.kpis.active_alarms" unit="条" :icon="Bell" color="#D93026"
              @click="$router.push('/alarms')"><template #sub>
                <span class="sev-sum"><span v-for="s in (['P1', 'P2', 'P3'] as const)" :key="s"><i :style="{ background: SEVERITY[s].color }" />{{ s }} <b>{{ data.kpis[s.toLowerCase() as 'p1' | 'p2' | 'p3'] }}</b></span></span></template></StatCard></el-col>
            <el-col :xs="12" :md="6"><StatCard title="待认领" :value="data.kpis.firing" unit="条" :icon="Warning" color="#F57C00"
              sub="尚未认领的告警" @click="$router.push('/alarms?status_group=firing')" /></el-col>
            <el-col :xs="12" :md="6"><StatCard title="平均恢复 MTTR" :value="data.kpis.mttr_min ?? '-'" unit="分钟" :icon="Timer" color="#2E7D32"
              :sub="`MTTA ${data.kpis.mtta_min ?? '-'} 分 · 恢复 ${data.kpis.resolved_24h} 条`" /></el-col>
          </el-row>
          <!-- 租户健康：点击切换到该租户视角 -->
          <div class="tenants" :class="{ single: data.tenants.length < 2 }">
            <div v-for="t in data.tenants" :key="t.id" class="tenant" :class="{ current: app.tenantId === t.id }" :style="{ '--tc': t.color }"
                 @click="app.setTenant(app.tenantId === t.id ? null : t.id)">
              <div class="t-head">
                <span class="t-name">{{ t.name }}</span>
                <span class="badge" :style="{ '--bc': METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.color }">ECS 指标 {{ METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.short ?? t.metrics_source }}</span>
                <span class="flex-1" />
                <el-tooltip :content="t.last_check_at ? `最近连通性测试 ${fmtTime(t.last_check_at)}` : '尚未做连通性测试'" placement="top">
                  <span class="chk" :class="t.last_check_ok == null ? 'unknown' : t.last_check_ok ? 'ok' : 'fail'"><i />{{ t.last_check_ok == null ? '未测试' : t.last_check_ok ? '接入正常' : '接入异常' }}</span>
                </el-tooltip>
              </div>
              <div class="t-body">
                <div class="t-score num" :style="{ color: scoreColor(t.score) }">{{ t.score ?? '—' }}<small>{{ t.score == null ? '暂无数据' : '分' }}</small></div>
                <div class="t-kv">
                  <span>资源 <b class="num">{{ t.resources }}</b></span>
                  <span>异常 <b class="num" :class="{ danger: t.abnormal }">{{ t.abnormal }}</b></span>
                  <span>ECS <b class="num">{{ ecsCount(t.ecs, 'ACTIVE') }}</b>/{{ ecsTotal(t.ecs) }} 运行</span>
                </div>
                <div class="t-sevs"><template v-for="s in SEVERITIES" :key="s"><span v-if="t.alarms[s]" class="sev" :style="{ '--sc': SEVERITY[s].color }">{{ s }} {{ t.alarms[s] }}</span></template>
                  <span v-if="!SEVERITIES.some((s) => t.alarms[s])" class="text-3">无活动告警</span></div>
              </div>
              <div class="t-caps">
                <span v-for="(label, k) in CAPABILITY" :key="k" class="cap" :class="capClass(capOf(t, k))">
                  <i />{{ label }}</span>
                <span class="flex-1" /><span class="go text-3">{{ app.tenantId === t.id ? '返回全部租户' : '切换到该租户' }}<el-icon><ArrowRight /></el-icon></span>
              </div>
            </div>
          </div>
        </el-col>
      </el-row>

      <div class="groups mt-16">
        <div v-for="g in data.groups" :key="g.code" class="group">
          <div class="g-head"><button v-if="groupLocation(g.code)" class="g-name group-link" @click="goGroup(g.code)">{{ g.name }}<el-icon><ArrowRight /></el-icon></button><span v-else class="g-name">{{ g.name }}</span></div>
          <div class="g-mid">
            <div class="g-score num" :style="{ color: scoreColor(g.score) }">{{ g.score ?? '—' }}<small>{{ g.score == null ? '暂无数据' : '分' }}</small></div>
            <span class="sevs"><template v-for="s in SEVERITIES" :key="s"><i v-if="g.alarms[s]" :title="`${s} ${g.alarms[s]} 条`" :style="{ background: SEVERITY[s].color }">{{ g.alarms[s] }}</i></template></span>
          </div>
          <el-progress :percentage="g.score ?? 0" :color="scoreColor(g.score)" :show-text="false" :stroke-width="6" />
          <div class="g-foot">
            <span>资源 {{ g.resources }}</span><span>异常 <b :class="{ danger: g.abnormal }">{{ g.abnormal }}</b></span>
            <span v-if="g.unknown_resources" class="unavailable">无数据 {{ g.unknown_resources }}</span>
            <router-link v-if="g.code === 'mrs' && user.can('ledger:view')" :to="{ path: '/ledger', query: { type: 'mrs_cluster' } }">集群台账</router-link>
          </div>
        </div>
      </div>

      <el-row :gutter="16" class="mt-16 stretch-row">
        <el-col :xs="24" :lg="16">
          <div class="hi-card fill-card">
            <div class="hi-card__title">近 24 小时新增告警</div>
            <EChart :option="trendOption" height="auto" class="fill-chart" />
          </div>
        </el-col>
        <el-col :xs="24" :lg="8">
          <div class="hi-card fill-card ledger">
            <div class="hi-card__title">资源台账<el-link v-if="user.can('ledger:view')" type="primary" underline="never" @click="$router.push('/ledger')">进入台账</el-link></div>
            <div class="l-top">
              <div class="l-num" @click="$router.push('/ledger')"><b class="num">{{ data.ledger.total }}</b><span>台账资源（个）</span></div>
              <div class="l-num warn" :class="{ ok: !data.ledger.unassigned }" @click="$router.push({ path: '/ledger', query: { unassigned: '1' } })">
                <b class="num">{{ data.ledger.unassigned }}</b><span>未分配负责人</span></div>
            </div>
            <div class="l-cats">
              <div v-for="c in data.ledger.by_category" :key="c.code" class="l-cat" @click="$router.push({ path: '/ledger', query: { category: c.code } })">
                <span class="c-name">{{ c.name }}</span>
                <div class="c-bar"><i :style="{ width: `${catPct(c.count)}%`, background: CAT_COLOR[c.code] }" /></div>
                <b class="num">{{ c.count }}</b>
              </div>
            </div>
            <div class="l-changes" @click="$router.push('/ledger?tab=changes')">
              <span class="k">近 7 天变更</span>
              <span v-for="k in (['CREATED', 'UPDATED', 'DELETED'] as const)" :key="k" class="chg">
                <i :style="{ background: CHANGE_TYPE[k].color }" />{{ CHANGE_TYPE[k].label }} <b class="num">{{ data.ledger.changes_7d[k] ?? 0 }}</b></span>
              <el-icon class="go"><ArrowRight /></el-icon>
            </div>
          </div>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="mt-16 stretch-row">
        <!-- ECS 状态 -->
        <el-col :xs="24" :lg="8">
          <div class="hi-card fill-card ecs">
            <div class="hi-card__title">ECS 云服务器状态
              <el-link type="primary" underline="never" @click="$router.push({ path: '/ledger', query: { type: 'ecs_server' } })">查看台账</el-link></div>
            <div class="ecs-body">
              <EChart :option="ecsOption" height="180px" class="ecs-pie" />
              <div class="ecs-legend">
                <div v-for="s in data.ecs.by_status" :key="s.status" class="el" @click="goEcs(s.status)">
                  <i :style="{ background: ECS_STATUS_COLOR[s.status] }" /><span class="n">{{ s.name }}</span><b class="num">{{ s.count }}</b>
                </div>
              </div>
            </div>
            <div class="ecs-foot">
              <div class="cover">
                <span>监控指标覆盖</span>
                <el-progress :percentage="pct(data.ecs.with_metrics, data.ecs.total)" :stroke-width="6" :show-text="false" class="flex-1" />
                <b class="num">{{ data.ecs.with_metrics }}/{{ data.ecs.total }}</b>
              </div>
              <div class="srcs">
                <span v-for="t in data.tenants" :key="t.id" class="src"><TenantTag :id="t.id" :name="t.short_name" :color="t.color" plain />
                  <em :style="{ '--bc': METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.color }">{{ METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.label ?? t.metrics_source }}</em></span>
              </div>
            </div>
          </div>
        </el-col>
        <!-- 容器事件 -->
        <el-col :xs="24" :lg="8">
          <div class="hi-card fill-card pods">
            <div class="hi-card__title">容器事件<span class="sub">近 24 小时 · {{ data.pods.events_24h }} 条</span>
              <el-link v-if="user.can('podevent:view')" type="primary" underline="never" @click="$router.push('/pod-events')">进入容器事件</el-link></div>
            <div class="p-nums">
              <div v-for="n in podNums" :key="n.label" class="p-num" :style="{ '--pc': n.color }" @click="goPods(n.types)">
                <b class="num">{{ n.value ?? '—' }}</b><span>{{ n.label }}</span>
              </div>
            </div>
            <div class="p-sub">重启最多的工作负载</div>
            <div class="p-top">
              <div v-for="w in data.pods.top_restart" :key="w.scope_key || w.workload" class="pt" @click="goWorkload(w)">
                <span class="w mono">{{ w.workload }}</span>
                <span class="bar"><i v-if="w.count != null" :style="{ width: `${pct(w.count, topRestartMax)}%` }" /></span>
                <b class="num">{{ w.count ?? '—' }}</b>
              </div>
              <div v-if="!data.pods.top_restart.length" class="text-3 empty">近 24 小时没有容器重启</div>
            </div>
            <div class="p-sub">进行中<em v-if="data.pods.ongoing_total" class="text-3">（{{ data.pods.ongoing_total }}）</em>
              <el-link v-if="data.pods.ongoing_total && user.can('podevent:view')" type="primary" underline="never" class="p-more" @click="$router.push({ path: '/pod-events', query: { ongoing: '1' } })">全部</el-link></div>
            <div class="p-ongoing">
              <div v-for="e in data.pods.ongoing ?? []" :key="e.id" class="po" @click="goWorkload(e)">
                <span class="po-type" :style="{ '--c': e.color }">{{ e.type_name }}</span>
                <span class="w mono ellipsis">{{ e.workload }}</span>
                <span class="text-3 since">已持续 {{ sinceText(e.first_time) }}</span>
              </div>
              <div v-if="!data.pods.ongoing?.length" class="text-3 empty">当前没有进行中的容器事件</div>
            </div>
          </div>
        </el-col>
        <!-- 容量水位 -->
        <el-col :xs="24" :lg="8">
          <div class="hi-card fill-card">
            <div class="hi-card__title">容量水位 Top 5
              <el-select v-model="capTab" size="small" class="cap-sel">
                <el-option v-for="(c, k) in CAP" :key="k" :value="k" :label="c.label" />
              </el-select>
            </div>
            <EChart :option="capacityOption" height="auto" class="fill-chart cap-chart" />
            <div class="cap-legend">
              <span v-for="l in capLines" :key="l.sev"><i :style="{ color: SEVERITY[l.sev].color }" />{{ l.sev }} ≥ {{ l.value }}%</span>
            </div>
          </div>
        </el-col>
      </el-row>

      <div class="hi-card mt-16">
        <div class="hi-card__title">重点活动告警 Top 10<el-link type="primary" underline="never" @click="$router.push('/alarms')">全部告警</el-link></div>
        <el-table :data="data.top_alarms" size="small" :row-class-name="({ row }) => `sev-row-${row.severity.toLowerCase()}`"
                  @row-click="(row) => (drawerId = row.id)">
          <el-table-column label="级别" width="92"><template #default="{ row }"><SeverityTag :level="row.severity" /></template></el-table-column>
          <el-table-column label="告警" min-width="260" show-overflow-tooltip>
            <template #default="{ row }"><b>{{ row.title }}</b></template>
          </el-table-column>
          <el-table-column label="资源" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.resource_name }}<span v-if="row.sub_object" class="text-3"> / {{ row.sub_object }}</span></template>
          </el-table-column>
          <el-table-column v-if="data.tenants.length > 1" label="租户" width="100"><template #default="{ row }"><TenantTag :id="row.tenant_id" /></template></el-table-column>
          <el-table-column label="类型" width="130"><template #default="{ row }">{{ typeName(row.resource_type) }}</template></el-table-column>
          <el-table-column label="当前值" width="110"><template #default="{ row }"><b class="num">{{ row.current_value ?? '-' }}</b></template></el-table-column>
          <el-table-column label="持续" width="110"><template #default="{ row }">{{ durationOf(row.first_seen) }}</template></el-table-column>
          <el-table-column label="状态" width="86"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
        </el-table>
      </div>

      <div class="hi-card mt-16 tasks">
        <div class="hi-card__title">巡检任务
          <el-link type="primary" underline="never" @click="$router.push('/system?tab=tasks')">任务管理</el-link></div>
        <div class="task-grid">
          <div v-for="t in data.tasks" :key="t.code" class="task" :class="`st-${(t.last_status || 'none').toLowerCase()}`">
            <div class="t-head">
              <span class="t-name">{{ t.name }}</span>
              <el-tag :type="TASK_STATUS[t.last_status || '']?.type || 'info'" size="small">{{ TASK_STATUS[t.last_status || '']?.label || '未运行' }}</el-tag>
            </div>
            <div class="text-3">{{ t.cron ? `计划 ${t.cron}` : `每 ${every(t.interval_sec)}执行` }} · 上次 {{ fromNow(t.last_run_at) }} · 耗时 {{ t.duration_sec ?? '-' }}s</div>
            <div class="t-foot">
              <template v-if="t.service === 'ledger'"><span>同步资源 <b class="num">{{ t.resource_total ?? '—' }}</b></span><span>近 7 天变更 <b class="num">{{ changes7d }}</b></span></template>
              <template v-else><span>检查对象 <b class="num">{{ t.resource_total ?? '—' }}</b></span><span>异常 <b class="num" :class="{ danger: t.abnormal_total }">{{ t.abnormal_total ?? '—' }}</b></span></template>
              <span v-if="t.counts_complete === false" class="text-3">项目数据未齐</span>
            </div>
          </div>
        </div>
      </div>
    </template>
    <AlarmDrawer v-model="drawerId" @changed="load" />
  </div>
</template>

<script setup lang="ts">
import { ArrowRight, Bell, Coin, Timer, Warning } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchSummary } from '@/api'
import EChart from '@/components/EChart.vue'
import PageHeader from '@/components/PageHeader.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import StatCard from '@/components/StatCard.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { healthGroupLocation } from '@/config/workspace-navigation'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { CapacityKey, EcsStatusCount, Severity, Summary, TenantHealth } from '@/types'
import {
  CAPABILITY, CHANGE_TYPE, ECS_STATUS_COLOR, METRICS_SOURCE, SEVERITIES, SEVERITY, TASK_STATUS, effectiveMetricsSource, scoreColor, typeName, usageColor,
} from '@/utils/dict'
import { durationOf, fmtTime, fromNow, humanize } from '@/utils/format'
import AlarmDrawer from '../alarms/AlarmDrawer.vue'

const data = ref<Summary>()
const request = useLatestRequest()
const loading = request.loading
const loadError = ref('')
const capTab = ref<CapacityKey>('ecs_cpu')
const app = useAppStore()
const user = useUserStore()
const sinceText = (t: string) => humanize((Date.now() - new Date(t).getTime()) / 1000)
const drawerId = ref<number | null>(null)
const router = useRouter()
let timer: number | undefined

async function load() {
  try {
    await request.run(() => fetchSummary(), value => { data.value = value; loadError.value = '' })
  } catch (error) { loadError.value = error instanceof Error ? error.message : '加载失败' }
}
onMounted(() => { load(); timer = window.setInterval(load, 30000) })
onBeforeUnmount(() => clearInterval(timer))

const groupLocation = (code: string) => healthGroupLocation(code, user.canAny)
const goGroup = (code: string) => { const location = groupLocation(code); if (location) return router.push(location) }
const goEcs = (status: string) => router.push({ path: '/ledger', query: { type: 'ecs_server', status } })
const goPods = (types: string[]) => router.push({ path: '/pod-events', query: types.length ? { event_type: types.join(',') } : {} })
/** top_restart 的 workload 形如 命名空间/工作负载 */
const goWorkload = (item: string | { workload: string; namespace?: string; workload_name?: string; tenant_id?: number | null;
  project_ref?: number | null; cluster_id?: string; workload_kind?: string }) => {
  const path = typeof item === 'string' ? item : item.workload
  const [namespace, workload] = path.includes('/') ? path.split('/') : ['', path]
  router.push({ path: '/pod-events', query: { namespace, workload,
    ...(typeof item === 'string' ? {} : { namespace: item.namespace || namespace, workload: item.workload_name || workload,
      tenant_id: item.tenant_id ?? undefined, project_ref: item.project_ref ?? undefined,
      unknown_project: item.project_ref === null ? '1' : undefined,
      cluster_id: item.cluster_id || undefined, workload_kind: item.workload_kind || undefined }) } })
}

// 租户健康卡片
const ecsCount = (list: EcsStatusCount[], st: string) => list.find((x) => x.status === st)?.count ?? 0
const ecsTotal = (list: EcsStatusCount[]) => list.reduce((a, x) => a + x.count, 0)
const capClass = (v: boolean | null | undefined) => (v == null ? 'unknown' : v ? 'ok' : 'off')
const capOf = (t: TenantHealth, k: string | number) => (t.capabilities as Record<string, boolean | null>)[String(k)]
const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0)

// 容器事件卡片
const podNums = computed(() => {
  const p = data.value!.pods
  return [
    { label: '容器重启', value: p.restarts_24h, color: '#F57C00', types: ['RESTART', 'OOM_KILLED'] },
    { label: 'OOM', value: p.oom_24h, color: '#D93026', types: ['OOM_KILLED'] },
    { label: 'CrashLoop', value: p.crashloop, color: '#B3261E', types: ['CRASH_LOOP'] },
    { label: '背压', value: p.backpressure, color: '#7B1FA2', types: ['BACKPRESSURE'] },
  ]
})
const topRestartMax = computed(() => Math.max(1, ...(data.value?.pods.top_restart ?? []).map((w) => w.count ?? 0)))

// ECS 状态环图
const ecsOption = computed(() => {
  const e = data.value!.ecs
  return {
    tooltip: { trigger: 'item', formatter: '{b}：{c} 台（{d}%）' },
    series: [{
      type: 'pie', radius: ['58%', '82%'], center: ['50%', '50%'], padAngle: 2, itemStyle: { borderRadius: 4 }, avoidLabelOverlap: false,
      label: { show: true, position: 'center', formatter: () => `{v|${e.total}}\n{l|台 ECS}`, rich: { v: { fontSize: 26, fontWeight: 700, color: '#1f2329' }, l: { fontSize: 12, color: '#86909c', padding: [4, 0, 0, 0] } } },
      labelLine: { show: false },
      data: e.by_status.filter((s) => s.count).map((s) => ({ name: s.name, value: s.count, itemStyle: { color: ECS_STATUS_COLOR[s.status] } })),
    }],
  }
})

// 资源台账卡片
const CAT_COLOR: Record<string, string> = { compute: '#1d63f0', storage: '#0e9f8f', network: '#7b61ff', database: '#f08a24', bigdata: '#e0457b' }
const catMax = computed(() => Math.max(1, ...(data.value?.ledger.by_category ?? []).map((c) => c.count)))
const catPct = (n: number) => Math.max(2, Math.round((n / catMax.value) * 100))
const changes7d = computed(() => Object.values(data.value?.ledger.changes_7d ?? {}).reduce((a, b) => a + b, 0))
const every = (sec: number) => (sec % 3600 === 0 ? `${sec / 3600} 小时` : sec % 60 === 0 ? `${sec / 60} 分钟` : `${sec} 秒`)

const gaugeOption = computed(() => {
  const score = data.value?.health_score
  const v = score ?? 0
  return {
    series: [{
      type: 'gauge', startAngle: 220, endAngle: -40, min: 0, max: 100, radius: '96%', center: ['50%', '58%'],
      progress: { show: score != null, width: 14, roundCap: true, itemStyle: { color: scoreColor(score) } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#eef1f6']] }, roundCap: true },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, pointer: { show: false },
      title: { offsetCenter: [0, '38%'], fontSize: 13, color: '#86909c' },
      detail: { offsetCenter: [0, '-2%'], fontSize: 38, fontWeight: 700, color: scoreColor(score), formatter: score == null ? '—' : '{value}' },
      data: [{ value: v, name: score == null ? '暂无有效巡检数据' : v >= 90 ? '运行良好' : v >= 75 ? '需要关注' : v >= 60 ? '存在风险' : '严重异常' }],
    }],
  }
})

const trendOption = computed(() => {
  const t = data.value?.trend
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { right: 0, top: 0, icon: 'circle', itemWidth: 8, data: SEVERITIES.map((s) => `${s} ${SEVERITY[s].label}`) },
    grid: { left: 8, right: 8, top: 36, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'category', data: t?.hours ?? [], axisTick: { show: false }, axisLine: { lineStyle: { color: '#e6e9ef' } }, axisLabel: { color: '#86909c' } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } }, axisLabel: { color: '#86909c' } },
    series: SEVERITIES.map((s) => ({
      name: `${s} ${SEVERITY[s].label}`, type: 'bar', stack: 'total', barWidth: 14, data: t?.series[s] ?? [],
      itemStyle: { color: SEVERITY[s].color, borderRadius: s === 'P4' ? [3, 3, 0, 0] : 0 },
    })),
  }
})

// 阈值与默认规则 R-ECS-004 / R-ECS-006 / R-DCS-005 / R-GS-006 的分级保持一致
const CAP: Record<CapacityKey, { label: string; lines: { sev: Severity; value: number }[] }> = {
  ecs_cpu: { label: 'ECS CPU', lines: [{ sev: 'P3', value: 85 }, { sev: 'P2', value: 95 }] },
  ecs_disk: { label: 'ECS 磁盘', lines: [{ sev: 'P3', value: 85 }, { sev: 'P2', value: 95 }] },
  dcs_memory: { label: 'DCS 内存', lines: [{ sev: 'P3', value: 80 }, { sev: 'P2', value: 90 }, { sev: 'P1', value: 95 }] },
  gaussdb_disk: { label: 'GaussDB 磁盘', lines: [{ sev: 'P3', value: 75 }, { sev: 'P2', value: 85 }, { sev: 'P1', value: 90 }] },
}
const capLines = computed(() => CAP[capTab.value].lines)
const capacityOption = computed(() => {
  const rows = [...(data.value?.capacity[capTab.value] ?? [])].reverse()
  const lines = capLines.value
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: number) => `${v}%` },
    grid: { left: 8, right: 48, top: 8, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'value', max: Math.max(100, ...rows.map(r => Math.ceil(r.value))), splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } }, axisLabel: { color: '#86909c', formatter: '{value}%' } },
    yAxis: { type: 'category', data: rows.map((r) => r.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#4e5969' } },
    series: [{
      type: 'bar', barWidth: 14, data: rows.map((r) => ({ value: r.value, itemStyle: { color: usageColor(r.value), borderRadius: [0, 7, 7, 0] } })),
      label: { show: true, position: 'right', formatter: '{c}%', color: '#1f2329', fontWeight: 600 },
      showBackground: true, backgroundStyle: { color: '#f3f5f9', borderRadius: [0, 7, 7, 0] },
      markLine: { symbol: 'none', silent: true, label: { show: false },
        data: lines.map((l) => ({ xAxis: l.value, lineStyle: { color: SEVERITY[l.sev].color, type: 'dashed', opacity: 0.8 } })) },
    }],
  }
})
</script>

<style scoped lang="scss">
.score-card { height: 100%; .score-foot { display: flex; justify-content: space-around; margin-top: 4px; color: var(--hi-text-2); font-size: 13px; b { font-size: 16px; margin-left: 4px; } } }
.kpis { margin-bottom: 16px; }
.danger { color: var(--hi-p1); }
.sev-sum { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px;
  > span { display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; }
  i { width: 6px; height: 6px; border-radius: 50%; margin-left: 6px; &:first-child { margin-left: 0; } } b { color: var(--hi-text-1); } }
.groups { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; }
.flex-1 { flex: 1; }
.tenants { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; &.single { grid-template-columns: 1fr; } }
.tenant { position: relative; padding: 12px 16px 10px; background: #fff; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); cursor: pointer; overflow: hidden;
  transition: box-shadow 0.2s, border-color 0.2s;
  &::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--tc); }
  &:hover { border-color: color-mix(in srgb, var(--tc) 45%, var(--hi-border)); box-shadow: var(--hi-shadow-hover); }
  &.current { border-color: var(--tc); box-shadow: 0 0 0 2px color-mix(in srgb, var(--tc) 18%, transparent); }
  .t-head { display: flex; align-items: center; gap: 6px; .t-name { font-weight: 600; font-size: 14px; color: var(--hi-text-1); margin-right: 4px; } }
  .badge { height: 18px; line-height: 18px; padding: 0 6px; border-radius: 4px; font-size: 11px; color: var(--bc); background: color-mix(in srgb, var(--bc) 10%, #fff); }
  .chk { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; i { width: 6px; height: 6px; border-radius: 50%; }
    &.ok { color: var(--hi-ok); i { background: var(--hi-ok); } } &.fail { color: var(--hi-p1); i { background: var(--hi-p1); } } &.unknown { color: var(--hi-text-3); i { background: var(--hi-text-3); } } }
  .t-body { display: flex; align-items: center; gap: 18px; margin: 8px 0 6px; }
  .t-score { font-size: 28px; font-weight: 700; line-height: 1; small { font-size: 12px; margin-left: 2px; color: var(--hi-text-3); font-weight: 400; } }
  .t-kv { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--hi-text-3); b { color: var(--hi-text-1); margin: 0 1px; } b.danger { color: var(--hi-p1); } }
  .t-sevs { margin-left: auto; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; max-width: 45%;
    .sev { padding: 0 6px; height: 20px; line-height: 20px; border-radius: 10px; font-size: 11px; font-weight: 600; color: var(--sc); background: color-mix(in srgb, var(--sc) 10%, #fff); } }
  .t-caps { display: flex; align-items: center; gap: 12px; padding-top: 8px; border-top: 1px dashed var(--hi-border); font-size: 12px; color: var(--hi-text-2);
    .cap { display: inline-flex; align-items: center; gap: 4px; i { width: 6px; height: 6px; border-radius: 50%; background: var(--hi-text-3); }
      &.ok i { background: var(--hi-ok); } &.off { color: var(--hi-text-3); text-decoration: line-through; i { background: var(--hi-border); } } &.unknown { color: var(--hi-text-3); } }
    .go { display: inline-flex; align-items: center; gap: 2px; } }
  &:hover .go { color: var(--tc); } }
.ecs { .ecs-body { display: flex; align-items: center; gap: 8px; .ecs-pie { width: 52%; flex: none; } }
  .ecs-legend { flex: 1; display: flex; flex-direction: column; gap: 6px;
    .el { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; &:hover { background: var(--hi-bg-soft); }
      i { width: 8px; height: 8px; border-radius: 2px; flex: none; } .n { flex: 1; color: var(--hi-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; } } }
  .ecs-foot { margin-top: auto; padding-top: 10px; border-top: 1px dashed var(--hi-border); font-size: 12px; color: var(--hi-text-3);
    .cover { display: flex; align-items: center; gap: 10px; b { color: var(--hi-text-1); font-size: 13px; } }
    .srcs { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 8px;
      .src { display: inline-flex; align-items: center; gap: 6px; em { font-style: normal; padding: 0 6px; border-radius: 4px; line-height: 18px; color: var(--bc); background: color-mix(in srgb, var(--bc) 10%, #fff); } } } } }
.pods { .p-nums { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .p-num { padding: 10px 0; text-align: center; border-radius: 8px; cursor: pointer; background: color-mix(in srgb, var(--pc) 7%, #fff); transition: background 0.2s;
    &:hover { background: color-mix(in srgb, var(--pc) 14%, #fff); }
    b { display: block; font-size: 22px; font-weight: 700; line-height: 1.2; color: var(--pc); } span { font-size: 12px; color: var(--hi-text-3); } }
  .p-sub { margin: 14px 0 6px; font-size: 12px; color: var(--hi-text-3); }
  .p-top { display: flex; flex-direction: column; gap: 4px;
    .pt { display: grid; grid-template-columns: minmax(0, 1.3fr) 1fr 28px; align-items: center; gap: 10px; padding: 4px 6px; border-radius: 6px; cursor: pointer; font-size: 12px;
      &:hover { background: var(--hi-bg-soft); .w { color: var(--el-color-primary); } }
      .w { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--hi-text-2); }
      .bar { height: 6px; border-radius: 3px; background: #eef1f6; i { display: block; height: 100%; border-radius: 3px; background: #F57C00; } }
      b { text-align: right; } }
    .empty { padding: 20px 0; text-align: center; } }
  .p-sub { display: flex; align-items: center; em { font-style: normal; } .p-more { margin-left: auto; font-size: 12px; } }
  .p-ongoing { display: flex; flex-direction: column; gap: 4px;
    .po { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; cursor: pointer; font-size: 12px;
      &:hover { background: var(--hi-bg-soft); .w { color: var(--el-color-primary); } }
      .po-type { padding: 0 6px; line-height: 18px; border-radius: 4px; color: var(--c); background: color-mix(in srgb, var(--c) 10%, #fff); white-space: nowrap; }
      .w { color: var(--hi-text-2); } .since { white-space: nowrap; } }
    .empty { padding: 12px 0; text-align: center; } } }
.cap-sel { width: 130px; margin-left: auto; }
.cap-chart { min-height: 230px; }
.group {
  padding: 14px 16px; background: #fff; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
  .g-head { display: flex; justify-content: space-between; color: var(--hi-text-2); font-size: 13px; .el-icon { color: var(--hi-text-3); } }
  .group-link { display: flex; justify-content: space-between; align-items: center; gap: 6px; width: 100%; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
  .group-link:hover { color: var(--el-color-primary); }
  .group-link:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 4px; }
  .g-score { margin: 6px 0 8px; font-size: 26px; font-weight: 700; small { font-size: 12px; margin-left: 2px; color: var(--hi-text-3); font-weight: 400; } }
  .g-mid { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
  .sevs { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; i { font-style: normal; color: #fff; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; font-size: 11px; display: grid; place-items: center; } }
  .g-foot { display: grid; grid-template-columns: 1fr auto; gap: 4px; margin-top: 8px; font-size: 12px; color: var(--hi-text-3);
    span { white-space: nowrap; } .unavailable { grid-column: 1 / -1; }
    a { color: var(--el-color-primary); text-decoration: none; }
  }
}
.stretch-row > .el-col > .fill-card { height: 100%; display: flex; flex-direction: column; }
.fill-chart { flex: 1; min-height: 290px; }
.cap-legend { display: flex; justify-content: flex-end; gap: 16px; padding-top: 6px; font-size: 12px; color: var(--hi-text-3);
  i { display: inline-block; width: 14px; height: 0; margin-right: 6px; vertical-align: middle; border-top: 2px dashed; } }
.ledger {
  .l-top { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }
  .l-num { padding: 12px 14px; border-radius: 8px; background: var(--hi-bg-soft, #f5f7fb); cursor: pointer; transition: background 0.2s;
    &:hover { background: #eef3fe; }
    b { display: block; font-size: 26px; font-weight: 700; line-height: 1.2; color: var(--hi-text-1); }
    span { font-size: 12px; color: var(--hi-text-3); }
    &.warn b { color: var(--hi-p2); } &.warn.ok b { color: var(--hi-text-1); } }
  .l-cats { flex: 1; display: flex; flex-direction: column; justify-content: space-around; gap: 6px; }
  .l-cat { display: grid; grid-template-columns: 96px 1fr 36px; align-items: center; gap: 10px; padding: 4px 0; cursor: pointer; font-size: 13px;
    &:hover .c-name { color: var(--el-color-primary); }
    .c-name { color: var(--hi-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .c-bar { height: 8px; border-radius: 4px; background: #eef1f6; overflow: hidden; i { display: block; height: 100%; border-radius: 4px; } }
    b { text-align: right; color: var(--hi-text-1); } }
  .l-changes { display: flex; align-items: center; gap: 14px; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--hi-border); font-size: 13px; cursor: pointer;
    .k { color: var(--hi-text-3); } .chg { color: var(--hi-text-2); i { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 5px; vertical-align: middle; } b { margin-left: 2px; color: var(--hi-text-1); } }
    .go { margin-left: auto; color: var(--hi-text-3); } &:hover .go { color: var(--el-color-primary); } }
}
.tasks {
  .task-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
  .task { padding: 12px 14px; border: 1px solid var(--hi-border); border-left: 3px solid var(--el-color-success); border-radius: 8px; display: flex; flex-direction: column; gap: 6px;
    &.st-partial { border-left-color: var(--el-color-warning); } &.st-failed, &.st-timeout { border-left-color: var(--el-color-danger); } &.st-none { border-left-color: var(--hi-border); }
    .t-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .t-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .t-foot { display: flex; gap: 18px; font-size: 13px; color: var(--hi-text-2); b { margin-left: 4px; color: var(--hi-text-1); } b.danger { color: var(--hi-p1); } } }
}
:deep(.el-table__row) { cursor: pointer; }
@media (max-width: 1399px) {
  .groups { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (max-width: 1199px) {
  .score-card { height: auto; margin-bottom: 16px; }
  .tenants { grid-template-columns: minmax(0, 1fr); }
  .stretch-row { row-gap: 16px; }
}
@media (max-width: 767px) {
  .kpis { row-gap: 12px; }
  .groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .group { min-width: 0; }
  .tenant .t-head, .tenant .t-body, .tenant .t-caps { flex-wrap: wrap; }
  .tenant .t-head .t-name { flex: 1 1 100%; }
  .tenant .t-caps { gap: 8px; }
  .tenant .t-sevs { max-width: 100%; }
}
@media (max-width: 1399px) {
  .ecs .ecs-body { flex-direction: column; align-items: stretch; }
  .ecs .ecs-body .ecs-pie { width: 100%; }
  .ecs .ecs-legend { width: 100%; min-width: 0; }
}
</style>
