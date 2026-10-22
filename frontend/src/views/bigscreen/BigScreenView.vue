<template>
  <div ref="root" class="bs-root">
    <div class="screen" :style="{ transform: `scale(${scale})` }">
      <!-- 顶栏 -->
      <header class="bs-header">
        <div class="h-left">
          <span class="env" :style="{ background: app.currentTenant?.color ?? '#1d63f0' }">{{ app.currentTenant ? '租户' : '多租户' }}</span><span>{{ app.tenantLabel }}</span>
          <span class="live" :class="{ on: connected }"><i />{{ connected ? '实时推送' : '重连中' }}</span>
        </div>
        <div class="h-title"><h1>HCS 云服务运行态势</h1><p>ECS · CCE · DataArts · MRS · GaussDB · DCS</p></div>
        <div class="h-right"><b class="num">{{ clock.time }}</b><span>{{ clock.date }}</span><el-icon class="exit" title="返回控制台" @click="$router.push('/dashboard')"><Close /></el-icon></div>
      </header>

      <main v-if="data" class="bs-main">
        <!-- 左列 -->
        <section class="col">
          <div class="panel score">
            <div class="p-title">平台健康分</div>
            <EChart :option="gaugeOption" height="170px" />
            <div class="score-foot">
              <div><span>纳管资源</span><b class="num">{{ data.kpis.resources }}</b></div>
              <div><span>异常资源</span><b class="num red">{{ data.kpis.abnormal_resources }}</b></div>
              <div><span>MTTR</span><b class="num">{{ data.kpis.mttr_min ?? '-' }}<small>min</small></b></div>
            </div>
          </div>
          <div class="panel">
            <div class="p-title">服务健康度</div>
            <div v-for="g in data.groups" :key="g.code" class="svc">
              <div class="svc-top">
                <span class="s-name">{{ g.name }}</span>
                <span class="s-meta">资源 {{ g.resources }} · 异常 <b :class="{ red: g.abnormal }">{{ g.abnormal }}</b></span>
                <span class="sevs"><template v-for="s in SEVERITIES" :key="s"><em v-if="g.alarms[s]" :style="{ background: SEVERITY[s].color }">{{ s }}·{{ g.alarms[s] }}</em></template></span>
                <b class="num score" :style="{ color: scoreColor(g.score) }">{{ g.score }}</b>
              </div>
              <div class="bar"><i :style="{ width: `${g.score}%`, background: `linear-gradient(90deg, ${scoreColor(g.score)}33, ${scoreColor(g.score)})` }" /></div>
            </div>
          </div>
          <div class="panel flex-panel">
            <div class="p-title">巡检任务<span class="p-sub">异常优先 · 共 {{ data.tasks.length }} 个</span></div>
            <div v-for="t in topTasks" :key="t.code" class="task">
              <i class="dot" :class="t.last_status?.toLowerCase()" />
              <span class="t-name">{{ t.name }}</span>
              <span class="t-meta">{{ fromNow(t.last_run_at) }} · {{ t.duration_sec ?? '-' }}s</span>
              <span v-if="t.service === 'ledger'" class="t-abn muted">同步 {{ t.resource_total }}</span>
              <span v-else class="t-abn" :class="{ red: t.abnormal_total }">{{ t.abnormal_total }}/{{ t.resource_total }}</span>
            </div>
          </div>
        </section>

        <!-- 中列 -->
        <section class="col center">
          <div class="kpis">
            <div v-for="k in kpis" :key="k.label" class="kpi" :style="{ '--c': k.color }">
              <span>{{ k.label }}</span><b class="num" :class="{ sm: String(k.value).length > 3 }">{{ k.value }}</b><em>{{ k.sub }}</em>
            </div>
          </div>
          <div class="panel fill-panel">
            <div class="p-title">近 24 小时新增告警趋势</div>
            <EChart :option="trendOption" height="auto" class="grow" />
          </div>
          <div class="cap-row">
            <div class="panel"><div class="p-title">ECS CPU 使用率 Top 5</div><EChart :option="capOption('ecs_cpu')" height="236px" /></div>
            <div class="panel"><div class="p-title">DCS 内存水位 Top 5</div><EChart :option="capOption('dcs_memory')" height="236px" /></div>
            <div class="panel"><div class="p-title">GaussDB 磁盘水位 Top 5</div><EChart :option="capOption('gaussdb_disk')" height="236px" /></div>
          </div>
        </section>

        <!-- 右列 -->
        <section class="col">
          <div v-if="data.tenants.length > 1" class="panel">
            <div class="p-title">租户健康</div>
            <div v-for="t in data.tenants" :key="t.id" class="tenant" :style="{ '--c': t.color }">
              <i class="t-dot" /><span class="t-name">{{ t.short_name }}</span>
              <span class="t-meta">ECS {{ ecsActive(t) }}/{{ ecsTotal(t) }} · {{ METRICS_SOURCE[t.metrics_source]?.short ?? t.metrics_source }}</span>
              <span class="sevs"><template v-for="s in SEVERITIES" :key="s"><em v-if="t.alarms[s]" :style="{ background: SEVERITY[s].color }">{{ s }}·{{ t.alarms[s] }}</em></template></span>
              <b class="num" :style="{ color: scoreColor(t.score) }">{{ t.score }}</b>
            </div>
          </div>
          <div class="panel">
            <div class="p-title">活动告警级别分布</div>
            <EChart :option="pieOption" :height="data.tenants.length > 1 ? '190px' : '230px'" />
          </div>
          <div class="panel flex-panel">
            <div class="p-title">实时告警<span class="cnt num">{{ feed.length }}</span></div>
            <transition-group name="feed" tag="div" class="feed">
              <div v-for="f in feed" :key="f.key" class="item" :style="{ '--c': SEVERITY[f.alarm.severity].color }">
                <div class="i-top"><span class="sev">{{ f.alarm.severity }}</span><b>{{ f.alarm.resource_name }}</b><span class="t">{{ fromNow(f.alarm.first_seen) }}</span></div>
                <div class="i-title">{{ f.alarm.title }}</div>
                <div class="i-foot"><span>{{ RESOURCE_TYPES[f.alarm.resource_type] }}</span><span :class="['st', f.alarm.status.toLowerCase()]">{{ ALARM_STATUS[f.alarm.status]?.label }}</span></div>
              </div>
            </transition-group>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Close } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { fetchSummary } from '@/api'
import EChart from '@/components/EChart.vue'
import { useAlarmStream } from '@/composables/useAlarmStream'
import { useAppStore } from '@/stores/app'
import type { Alarm, CapacityKey, Summary, TenantHealth } from '@/types'
import { ALARM_STATUS, METRICS_SOURCE, RESOURCE_TYPES, SEVERITIES, SEVERITY, scoreColor, usageColor } from '@/utils/dict'
import { fromNow } from '@/utils/format'

// 设计稿 1920×1080，等比缩放适配任意分辨率（拼接屏 / 投屏）
const W = 1920
const H = 1080
const root = ref<HTMLDivElement>()
const scale = ref(1)
const fit = () => { scale.value = Math.min(window.innerWidth / W, window.innerHeight / H) }

const app = useAppStore()
const data = ref<Summary>()
/** 大屏空间有限：失败 / 异常的任务优先，最多展示 4 个（台账同步的 abnormal_total 表示“变更数”，不参与异常排序） */
const abn = (t: { service: string; abnormal_total: number }) => (t.service === 'ledger' ? 0 : t.abnormal_total ?? 0)
const topTasks = computed(() => [...(data.value?.tasks ?? [])].sort((a, b) =>
  Number(b.last_status === 'FAILED') - Number(a.last_status === 'FAILED') || abn(b) - abn(a)).slice(0, 4))
const feed = ref<{ key: string; alarm: Alarm }[]>([])
const clock = reactive({ time: '', date: '' })
let timers: number[] = []

async function load() {
  data.value = await fetchSummary()
  if (!feed.value.length) feed.value = data.value.top_alarms.slice(0, 8).map((a) => ({ key: `init-${a.id}`, alarm: a }))
}
const tick = () => { const n = dayjs(); clock.time = n.format('HH:mm:ss'); clock.date = n.format('YYYY-MM-DD dddd') }

// 实时推送：新告警插入列表顶部（保留 8 条）并刷新统计
const { connected } = useAlarmStream((e) => {
  if (['FIRE', 'UPGRADE', 'RESOLVE'].includes(e.type)) {
    feed.value = [{ key: `${e.type}-${e.alarm.id}-${Date.now()}`, alarm: e.alarm }, ...feed.value.filter((f) => f.alarm.id !== e.alarm.id)].slice(0, 8)
  }
  load()
})

onMounted(() => {
  fit(); tick(); load()
  window.addEventListener('resize', fit)
  timers = [window.setInterval(tick, 1000), window.setInterval(load, 30000)]
})
onBeforeUnmount(() => { window.removeEventListener('resize', fit); timers.forEach(clearInterval) })

// ---- 图表（暗色）----
const AXIS = { axisLine: { lineStyle: { color: 'rgba(143,163,191,0.3)' } }, axisLabel: { color: '#8fa3bf', fontSize: 13 }, axisTick: { show: false } }
const SPLIT = { splitLine: { lineStyle: { color: 'rgba(143,163,191,0.12)', type: 'dashed' } } }

const ecsBy = (st: string) => data.value?.ecs.by_status.find((x) => x.status === st)?.count ?? 0
const ecsTotal = (t: TenantHealth) => t.ecs.reduce((a, x) => a + x.count, 0)
const ecsActive = (t: TenantHealth) => t.ecs.find((x) => x.status === 'ACTIVE')?.count ?? 0
const kpis = computed(() => {
  const k = data.value!.kpis
  const p = data.value!.pods
  return [
    { label: '活动告警', value: k.active_alarms, sub: '条', color: '#ff6b5e' },
    { label: 'P1 紧急', value: k.p1, sub: '需立即处理', color: '#ff4d4f' },
    { label: '待认领', value: k.firing, sub: '告警中', color: '#ffa940' },
    { label: 'ECS 运行', value: `${ecsBy('ACTIVE')}/${data.value!.ecs.total}`, sub: `关机 ${ecsBy('SHUTOFF')} · 故障 ${ecsBy('ERROR')}`, color: '#4096ff' },
    { label: '容器重启 24h', value: p.restarts_24h, sub: `OOM ${p.oom_24h} · 背压 ${p.backpressure}`, color: '#b37feb' },
    { label: 'MTTA', value: k.mtta_min ?? '-', sub: '分钟', color: '#36cfc9' },
  ]
})

const gaugeOption = computed(() => {
  const v = data.value?.health_score ?? 0
  const c = scoreColor(v)
  return {
    series: [
      { type: 'gauge', startAngle: 225, endAngle: -45, radius: '96%', center: ['50%', '58%'], min: 0, max: 100, splitNumber: 10,
        axisLine: { lineStyle: { width: 16, color: [[1, 'rgba(64,150,255,0.12)']] } },
        progress: { show: true, width: 16, itemStyle: { color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: '#1d63f0' }, { offset: 1, color: c }] } } },
        // 刻度画在色环内侧，避免 190px 高度下被裁切
        axisTick: { distance: 4, length: 4, lineStyle: { color: 'rgba(143,163,191,0.45)' } },
        splitLine: { distance: 4, length: 8, lineStyle: { color: 'rgba(143,163,191,0.7)', width: 2 } },
        axisLabel: { show: false }, pointer: { show: false },
        title: { offsetCenter: [0, '46%'], color: '#8fa3bf', fontSize: 15 },
        detail: { offsetCenter: [0, '2%'], fontSize: 52, fontWeight: 700, color: '#fff', fontFamily: 'DIN Alternate, Bahnschrift, Roboto' },
        data: [{ value: v, name: v >= 90 ? '运行良好' : v >= 75 ? '需要关注' : v >= 60 ? '存在风险' : '严重异常' }] },
    ],
  }
})

const trendOption = computed(() => {
  const t = data.value?.trend
  return {
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(10,24,48,0.92)', borderColor: '#1d63f0', textStyle: { color: '#e6f0ff' } },
    legend: { right: 10, top: 0, textStyle: { color: '#8fa3bf', fontSize: 13 }, icon: 'roundRect', itemWidth: 12, itemHeight: 6 },
    grid: { left: 10, right: 16, top: 36, bottom: 4, containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: t?.hours ?? [], ...AXIS },
    yAxis: { type: 'value', minInterval: 1, ...AXIS, axisLine: { show: false }, ...SPLIT },
    series: SEVERITIES.map((s) => ({
      name: `${s} ${SEVERITY[s].label}`, type: 'line', stack: 'total', smooth: 0.3, symbol: 'none', data: t?.series[s] ?? [],
      lineStyle: { width: 2, color: SEVERITY[s].color },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: `${SEVERITY[s].color}88` }, { offset: 1, color: `${SEVERITY[s].color}08` }] } },
      itemStyle: { color: SEVERITY[s].color },
    })),
  }
})

const capOption = (key: CapacityKey) => {
  const rows = [...(data.value?.capacity[key] ?? [])].reverse()
  return {
    grid: { left: 8, right: 56, top: 6, bottom: 0, containLabel: true },
    xAxis: { type: 'value', max: 100, show: false },
    yAxis: { type: 'category', data: rows.map((r) => r.name), ...AXIS, axisLine: { show: false }, axisLabel: { color: '#c5d3e8', fontSize: 13 } },
    series: [{
      type: 'bar', barWidth: 12, showBackground: true, backgroundStyle: { color: 'rgba(64,150,255,0.08)', borderRadius: 6 },
      data: rows.map((r) => ({ value: r.value, itemStyle: { borderRadius: 6, color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: `${usageColor(r.value)}40` }, { offset: 1, color: usageColor(r.value) }] } } })),
      label: { show: true, position: 'right', formatter: '{c}%', color: '#fff', fontSize: 14, fontWeight: 700, fontFamily: 'DIN Alternate, Bahnschrift, Roboto' },
    }],
  }
}

const pieOption = computed(() => {
  const k = data.value!.kpis
  const vals = { P1: k.p1, P2: k.p2, P3: k.p3, P4: k.p4 }
  return {
    tooltip: { trigger: 'item', backgroundColor: 'rgba(10,24,48,0.92)', borderColor: '#1d63f0', textStyle: { color: '#e6f0ff' } },
    legend: { orient: 'vertical', right: 16, top: 'middle', textStyle: { color: '#c5d3e8', fontSize: 14 }, itemGap: 14, icon: 'circle',
      formatter: (n: string) => `${n}   ${vals[n.slice(0, 2) as keyof typeof vals] ?? 0}` },
    series: [{
      type: 'pie', radius: ['52%', '76%'], center: ['32%', '50%'], padAngle: 2, itemStyle: { borderRadius: 4 }, label: { show: true, position: 'center', formatter: () => `${k.active_alarms}\n活动告警`, color: '#fff', fontSize: 20, fontWeight: 700, lineHeight: 28 },
      data: SEVERITIES.map((s) => ({ name: `${s} ${SEVERITY[s].label}`, value: vals[s], itemStyle: { color: SEVERITY[s].color } })),
    }],
  }
})
</script>

<style scoped lang="scss">
.bs-root { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; overflow: hidden; background: #050d1a; }
.screen {
  flex: none; width: 1920px; height: 1080px; transform-origin: center center; color: #e6f0ff; display: flex; flex-direction: column;
  background: radial-gradient(ellipse at 50% 0%, rgba(29, 99, 240, 0.28), transparent 60%), linear-gradient(180deg, #071226 0%, #050d1a 100%);
  background-image: radial-gradient(ellipse at 50% 0%, rgba(29, 99, 240, 0.28), transparent 60%),
    linear-gradient(rgba(64, 150, 255, 0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(64, 150, 255, 0.05) 1px, transparent 1px);
  background-size: 100% 100%, 40px 40px, 40px 40px;
}
.bs-header {
  position: relative; height: 92px; flex: none; display: grid; grid-template-columns: 1fr 760px 1fr; align-items: center; padding: 0 32px;
  &::after { content: ''; position: absolute; left: 32px; right: 32px; bottom: 0; height: 1px; background: linear-gradient(90deg, transparent, #1d63f0 20%, #36cfc9 50%, #1d63f0 80%, transparent); }
  .h-left { display: flex; align-items: center; gap: 16px; font-size: 16px; color: #8fa3bf;
    .env { padding: 2px 10px; border-radius: 4px; font-weight: 700; color: #fff; background: #d93026; letter-spacing: 1px; }
    .live { display: inline-flex; align-items: center; gap: 6px; i { width: 8px; height: 8px; border-radius: 50%; background: #6b7f99; }
      &.on { color: #52c41a; i { background: #52c41a; box-shadow: 0 0 10px #52c41a; } } } }
  .h-title { text-align: center;
    h1 { margin: 0; font-size: 40px; letter-spacing: 8px; font-weight: 700; background: linear-gradient(180deg, #fff, #8ec5ff); -webkit-background-clip: text; background-clip: text; color: transparent; }
    p { margin: 4px 0 0; font-size: 14px; letter-spacing: 4px; color: #5d7699; } }
  .h-right { display: flex; justify-content: flex-end; align-items: baseline; gap: 14px; color: #8fa3bf; font-size: 16px;
    b { font-size: 34px; color: #fff; letter-spacing: 2px; } .exit { cursor: pointer; align-self: center; font-size: 20px; &:hover { color: #fff; } } }
}
.bs-main { flex: 1; min-height: 0; display: grid; grid-template-columns: 470px 1fr 470px; gap: 20px; padding: 20px 32px 28px; }
.col { display: flex; flex-direction: column; gap: 20px; min-height: 0; }
.panel {
  position: relative; padding: 16px 20px; border-radius: 6px; background: linear-gradient(180deg, rgba(16, 38, 74, 0.72), rgba(9, 22, 44, 0.72));
  border: 1px solid rgba(64, 150, 255, 0.22); box-shadow: inset 0 0 30px rgba(29, 99, 240, 0.08);
  &::before, &::after { content: ''; position: absolute; width: 14px; height: 14px; border: 2px solid #3a8bff; }
  &::before { left: -1px; top: -1px; border-right: none; border-bottom: none; }
  &::after { right: -1px; bottom: -1px; border-left: none; border-top: none; }
  .p-title { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 600; margin-bottom: 10px; padding-left: 12px; border-left: 4px solid #3a8bff; line-height: 1;
    .cnt { margin-left: auto; font-size: 14px; padding: 2px 10px; border-radius: 10px; background: rgba(217, 48, 38, 0.25); color: #ff7875; }
    .p-sub { margin-left: auto; font-size: 12px; font-weight: 400; color: #8fa3bf; } }
}
.flex-panel { flex: 1; min-height: 0; overflow: hidden; }
.fill-panel { flex: 1; min-height: 0; display: flex; flex-direction: column; .grow { flex: 1; min-height: 0; } }
.red { color: #ff6b5e !important; }
.score-foot { display: grid; grid-template-columns: repeat(3, 1fr); text-align: center; margin-top: -6px;
  span { display: block; font-size: 13px; color: #8fa3bf; } b { font-size: 28px; color: #fff; small { font-size: 13px; margin-left: 2px; color: #8fa3bf; } } }
.svc { padding: 3px 0;
  .svc-top { display: flex; align-items: center; gap: 10px; font-size: 15px;
    .s-name { flex: none; }
    .s-meta { flex: 1; font-size: 12px; color: #8fa3bf; white-space: nowrap; }
    .sevs { display: flex; gap: 4px; em { font-style: normal; color: #fff; padding: 0 6px; border-radius: 8px; font-size: 11px; line-height: 18px; } }
    .score { width: 40px; text-align: right; font-size: 22px; } }
  .bar { height: 6px; margin: 5px 0 2px; border-radius: 3px; background: rgba(64, 150, 255, 0.1); i { display: block; height: 100%; border-radius: 3px; } } }
.task { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 14px; border-bottom: 1px dashed rgba(143, 163, 191, 0.15);
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #6b7f99; &.success { background: #52c41a; box-shadow: 0 0 8px #52c41a; } &.partial { background: #ffa940; box-shadow: 0 0 8px #ffa940; } &.failed { background: #ff4d4f; } }
  .t-name { flex: 1; } .t-meta { color: #6b7f99; font-size: 12px; } .t-abn { width: 58px; text-align: right; white-space: nowrap; font-family: var(--hi-font-num); color: #8fa3bf; &.muted { font-size: 12px; color: #6b7f99; } } }
.kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px;
  .kpi { padding: 16px 14px; text-align: center; border-radius: 6px; background: linear-gradient(180deg, color-mix(in srgb, var(--c) 18%, transparent), rgba(9, 22, 44, 0.6));
    border: 1px solid color-mix(in srgb, var(--c) 40%, transparent);
    span { display: block; font-size: 14px; color: #c5d3e8; } b { display: block; margin: 6px 0 2px; font-size: 46px; line-height: 1; color: var(--c); text-shadow: 0 0 16px color-mix(in srgb, var(--c) 60%, transparent); }
    b.sm { font-size: 34px; margin: 12px 0 8px; }
    em { font-style: normal; font-size: 12px; color: #6b7f99; white-space: nowrap; } } }
.cap-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.tenant { display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 15px; border-bottom: 1px dashed rgba(143, 163, 191, 0.15);
  &:last-child { border-bottom: none; }
  .t-dot { width: 10px; height: 10px; border-radius: 3px; background: var(--c); box-shadow: 0 0 8px var(--c); }
  .t-name { font-weight: 600; } .t-meta { flex: 1; font-size: 12px; color: #8fa3bf; white-space: nowrap; }
  .sevs { display: flex; gap: 4px; em { font-style: normal; color: #fff; padding: 0 6px; border-radius: 8px; font-size: 11px; line-height: 18px; } }
  b { width: 40px; text-align: right; font-size: 22px; } }
.feed { display: flex; flex-direction: column; gap: 10px;
  .item { padding: 10px 12px; border-radius: 4px; background: rgba(255, 255, 255, 0.03); border-left: 3px solid var(--c);
    .i-top { display: flex; align-items: center; gap: 8px; font-size: 14px;
      .sev { padding: 0 6px; border-radius: 3px; font-size: 12px; font-weight: 700; color: #fff; background: var(--c); }
      .t { margin-left: auto; font-size: 12px; color: #6b7f99; } }
    .i-title { margin: 5px 0; font-size: 14px; color: #c5d3e8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .i-foot { display: flex; justify-content: space-between; font-size: 12px; color: #6b7f99;
      .st.firing { color: #ff7875; } .st.acked { color: #ffc53d; } .st.resolved { color: #73d13d; } } } }
.feed-enter-active { transition: all 0.5s ease; }
.feed-enter-from { opacity: 0; transform: translateX(30px); background: rgba(217, 48, 38, 0.3); }
</style>
