<template>
  <div class="silence-page">
    <PageHeader title="维护与静默" desc="计划内变更、压测、已知问题处理期间屏蔽告警通知：命中的告警照常记录为“已抑制”，窗口结束后仍未恢复的会补发通知">
      <el-button v-if="canManage" type="primary" :icon="Plus" @click="openDialog(null)">新建维护窗口 / 静默</el-button>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </PageHeader>

    <el-row :gutter="16" class="mb-16">
      <el-col :span="6">
        <StatCard title="生效中" :value="counts.ACTIVE ?? 0" unit="个" :icon="MuteNotification" color="#2E7D32" @click="tab = 'active'">
          <template #sub>正在抑制 <b class="num">{{ suppressedNow }}</b> 条告警</template>
        </StatCard>
      </el-col>
      <el-col :span="6">
        <StatCard title="周期等待" :value="counts.WAITING ?? 0" unit="个" :icon="Calendar" color="#1d63f0" @click="tab = 'active'">
          <template #sub>{{ nextWeekly ? `下次窗口 ${fmtTime(nextWeekly.start, 'MM-DD ddd HH:mm')}` : '每周重复的例行变更窗口' }}</template>
        </StatCard>
      </el-col>
      <el-col :span="6">
        <StatCard title="待生效" :value="counts.PENDING ?? 0" unit="个" :icon="AlarmClock" color="#F57C00" @click="tab = 'pending'">
          <template #sub>{{ nextPending ? `最近 ${fmtTime(nextPending.starts_at, 'MM-DD HH:mm')} 开始` : '暂无已排期的窗口' }}</template>
        </StatCard>
      </el-col>
      <el-col :span="6">
        <StatCard title="累计抑制" :value="suppressedTotal" unit="次" :icon="Histogram" color="#8C8C8C" @click="tab = 'all'">
          <template #sub>已结束 {{ counts.EXPIRED ?? 0 }} 个 · 已取消 {{ counts.CANCELLED ?? 0 }} 个</template>
        </StatCard>
      </el-col>
    </el-row>

    <!-- 未来 7 天时间线：一眼看清哪些时段会屏蔽通知 -->
    <div class="hi-card mb-16">
      <div class="section-title tl-title">
        <span>未来 7 天</span>
        <span class="legend"><i class="lg m" />维护窗口 <i class="lg s" />静默 <i class="lg now" />现在</span>
        <span class="text-3 ml-auto">按平台时区显示；每周重复的窗口展开为具体时段</span>
      </div>
      <div v-if="!timeline.length" class="empty-dash">未来 7 天没有生效或待生效的维护窗口 / 静默</div>
      <div v-else class="timeline">
        <div class="axis">
          <div class="label-col" />
          <div class="track">
            <span v-for="d in axisDays" :key="d.left" class="tick" :style="{ left: `${d.left}%` }">{{ d.label }}</span>
          </div>
        </div>
        <div v-for="row in timeline" :key="row.s.id" class="tl-row" :class="{ hl: row.s.id === highlightId }" @click="locate(row.s.id)">
          <div class="label-col">
            <el-tag size="small" :type="SILENCE_KIND[row.s.kind].type" effect="plain">{{ SILENCE_KIND[row.s.kind].label }}</el-tag>
            <span class="ellipsis" :title="row.s.name">{{ row.s.name }}</span>
          </div>
          <div class="track">
            <span v-for="d in axisDays" :key="d.left" class="grid" :style="{ left: `${d.left}%` }" />
            <span class="past" :style="{ width: `${nowLeft}%` }" />
            <el-tooltip v-for="(b, i) in row.bars" :key="i" placement="top" :content="b.text">
              <span class="bar" :class="[row.s.kind === 'MAINTENANCE' ? 'm' : 's', { on: b.on }]" :style="{ left: `${b.left}%`, width: `${b.width}%` }" />
            </el-tooltip>
            <span class="now" :style="{ left: `${nowLeft}%` }" />
          </div>
        </div>
      </div>
    </div>

    <div class="hi-card">
      <div class="table-toolbar">
        <el-radio-group v-model="tab">
          <el-radio-button v-for="t in TABS" :key="t.value" :value="t.value">{{ t.label }}<span class="cnt">{{ tabCount(t.value) }}</span></el-radio-button>
        </el-radio-group>
        <div class="flex gap-12">
          <el-input v-model="keyword" clearable placeholder="名称 / 原因 / 创建人" :prefix-icon="Search" style="width: 240px" />
          <el-link type="primary" :underline="false" @click="router.push({ path: '/alarms', query: { status_group: 'suppressed' } })">查看已抑制告警 →</el-link>
        </div>
      </div>

      <el-table v-loading="loading" :data="rows" row-key="id" :row-class-name="rowClass">
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="SILENCE_STATUS[row.status].type" effect="light">{{ SILENCE_STATUS[row.status].label }}</el-tag></template>
        </el-table-column>
        <el-table-column label="名称 / 原因" min-width="260">
          <template #default="{ row }">
            <div class="name">
              <el-tag size="small" :type="SILENCE_KIND[row.kind].type" effect="plain">{{ SILENCE_KIND[row.kind].label }}</el-tag>
              <span class="ellipsis" :title="row.name">{{ row.name }}</span>
            </div>
            <div class="text-3 ellipsis" :title="row.reason">{{ row.reason || '（未填写原因）' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="匹配范围" min-width="200">
          <template #default="{ row }">
            <span class="scope">{{ row.describe }}</span>
            <el-tag v-if="!row.matchers.severities?.length || row.matchers.severities.includes('P1')" size="small" type="danger" effect="plain" class="ml-4">含 P1</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" min-width="230">
          <template #default="{ row }">
            <template v-if="row.recurrence?.weekdays?.length">
              <div><el-icon class="ico"><RefreshRight /></el-icon>{{ weeklyText(row as Silence) }}</div>
              <div class="text-3">有效期 {{ fmtTime(row.starts_at, 'YYYY-MM-DD') }} ~ {{ fmtTime(row.ends_at, 'YYYY-MM-DD') }}</div>
            </template>
            <template v-else>
              <div>{{ fmtTime(row.starts_at, 'MM-DD HH:mm') }} ~ {{ fmtTime(row.ends_at, 'MM-DD HH:mm') }}</div>
              <div class="text-3">共 {{ durationOf(row.starts_at, row.ends_at) }}</div>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="170">
          <template #default="{ row }"><span :class="progressClass(row as Silence)">{{ progressText(row as Silence) }}</span></template>
        </el-table-column>
        <el-table-column label="抑制告警" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip content="当前被抑制 / 累计抑制（次）" placement="top">
              <span><b class="num" :class="{ 'text-warn': row.suppressed_now }">{{ row.suppressed_now }}</b><span class="text-3"> / {{ row.suppressed_total }}</span></span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="创建" width="140">
          <template #default="{ row }">
            <div>{{ row.created_by }}</div>
            <div class="text-3">{{ fmtTime(row.created_at, 'MM-DD HH:mm') }}</div>
          </template>
        </el-table-column>
        <el-table-column v-if="canManage" label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <template v-if="isLive(row as Silence)">
              <el-button link type="primary" :disabled="!row.editable" @click="openDialog(row as Silence)">编辑</el-button>
              <el-button link type="danger" :disabled="!row.editable" @click="onCancel(row as Silence)">{{ row.status === 'PENDING' ? '撤销' : '提前结束' }}</el-button>
            </template>
            <el-button link type="primary" @click="openDialog(row as Silence, true)">复制</el-button>
          </template>
        </el-table-column>
        <template #empty><div class="empty-dash">{{ keyword ? '没有匹配的记录' : '暂无记录' }}</div></template>
      </el-table>
      <div v-if="!canManage" class="text-3 mt-12">当前角色只能查看；创建 / 修改 / 提前结束需要“创建 / 取消静默与维护窗口”权限。</div>
    </div>

    <SilenceDialog v-model:visible="dialog.visible" :silence="dialog.silence" :copy="dialog.copy" @saved="onSaved" />
  </div>
</template>

<script setup lang="ts">
import { AlarmClock, Calendar, Histogram, MuteNotification, Plus, Refresh, RefreshRight, Search } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { cancelSilence, listSilences, type SilenceTab } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import StatCard from '@/components/StatCard.vue'
import { useUserStore } from '@/stores/user'
import type { Silence, SilenceStatus } from '@/types'
import { SILENCE_KIND, SILENCE_STATUS, WEEKDAYS } from '@/utils/dict'
import { durationOf, fmtTime, humanize } from '@/utils/format'
import SilenceDialog from './SilenceDialog.vue'

const route = useRoute()
const router = useRouter()
const user = useUserStore()
const canManage = computed(() => user.can('silence:manage'))

const TABS: { value: SilenceTab; label: string }[] = [
  { value: 'current', label: '当前' }, { value: 'active', label: '生效 / 周期' }, { value: 'pending', label: '待生效' },
  { value: 'expired', label: '已结束' }, { value: 'cancelled', label: '已取消' }, { value: 'all', label: '全部' },
]
const tab = ref<SilenceTab>('current')
const keyword = ref('')
const loading = ref(false)
const current = ref<Silence[]>([])          // 时间线始终用“当前”（生效中 + 周期等待 + 待生效）
const tabItems = ref<Silence[]>([])
const counts = ref<Partial<Record<SilenceStatus, number>>>({})
const highlightId = ref<number | null>(null)
const now = ref(dayjs())

const tabCount = (t: SilenceTab) => {
  const c = counts.value
  const n = { current: ['ACTIVE', 'WAITING', 'PENDING'], active: ['ACTIVE', 'WAITING'], pending: ['PENDING'], expired: ['EXPIRED'],
    cancelled: ['CANCELLED'], all: ['ACTIVE', 'WAITING', 'PENDING', 'EXPIRED', 'CANCELLED'] }[t] as SilenceStatus[]
  return n.reduce((s, k) => s + (c[k] ?? 0), 0)
}
const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const src = tab.value === 'current' ? current.value : tabItems.value
  return kw ? src.filter((s) => `${s.name} ${s.reason} ${s.created_by} ${s.describe}`.toLowerCase().includes(kw)) : src
})
const suppressedNow = computed(() => current.value.reduce((s, x) => s + x.suppressed_now, 0))
const suppressedTotal = computed(() => (tab.value === 'all' ? tabItems.value : current.value).reduce((s, x) => s + x.suppressed_total, 0))
const nextWeekly = computed(() => current.value.filter((s) => s.status === 'WAITING' && s.next_window)
  .map((s) => s.next_window!).sort((a, b) => a.start.localeCompare(b.start))[0])
const nextPending = computed(() => current.value.filter((s) => s.status === 'PENDING').sort((a, b) => a.starts_at.localeCompare(b.starts_at))[0])

async function load() {
  loading.value = true
  now.value = dayjs()
  try {
    const cur = await listSilences('current')
    current.value = cur.items
    counts.value = cur.counts
    if (tab.value !== 'current') tabItems.value = (await listSilences(tab.value)).items
  } finally { loading.value = false }
}
watch(tab, async (t) => {
  if (t === 'current') return
  loading.value = true
  try { tabItems.value = (await listSilences(t)).items } finally { loading.value = false }
})

// ---- 时间线：今天 00:00 起 7 天 ----
const axisStart = computed(() => now.value.startOf('day'))
const axisEnd = computed(() => axisStart.value.add(7, 'day'))
const SPAN = 7 * 24 * 60
const pos = (d: dayjs.Dayjs) => Math.min(100, Math.max(0, (d.diff(axisStart.value, 'minute') / SPAN) * 100))
const nowLeft = computed(() => pos(now.value))
const axisDays = computed(() => Array.from({ length: 7 }, (_, i) => {
  const d = axisStart.value.add(i, 'day')
  return { left: (i / 7) * 100, label: i === 0 ? `今天 ${d.format('MM-DD')}` : d.format('MM-DD ddd') }
}))
/** 展开窗口：一次性 = [开始, 结束]；每周重复 = 有效期内每个命中星期的时段（end ≤ start 表示跨午夜） */
function windowsOf(s: Silence): [dayjs.Dayjs, dayjs.Dayjs][] {
  const vs = dayjs(s.starts_at)
  const ve = dayjs(s.ends_at)
  const rec = s.recurrence
  if (!rec?.weekdays?.length || !rec.start || !rec.end) return [[vs, ve]]
  const [sh, sm] = rec.start.split(':').map(Number)
  const [eh, em] = rec.end.split(':').map(Number)
  const out: [dayjs.Dayjs, dayjs.Dayjs][] = []
  for (let i = -1; i < 8; i++) {
    const d = axisStart.value.add(i, 'day')
    if (!rec.weekdays.includes(d.day() || 7)) continue
    const ws = d.hour(sh).minute(sm)
    let we = d.hour(eh).minute(em)
    if (!we.isAfter(ws)) we = we.add(1, 'day')
    const a = ws.isAfter(vs) ? ws : vs
    const b = we.isBefore(ve) ? we : ve
    if (b.isAfter(a)) out.push([a, b])
  }
  return out
}
const timeline = computed(() => current.value.map((s) => ({
  s,
  bars: windowsOf(s)
    .filter(([a, b]) => b.isAfter(axisStart.value) && a.isBefore(axisEnd.value))
    .map(([a, b]) => {
      const left = pos(a)
      return {
        left, width: Math.max(0.6, pos(b) - left), on: !now.value.isBefore(a) && now.value.isBefore(b),
        text: `${a.format('MM-DD ddd HH:mm')} ~ ${b.format(b.isSame(a, 'day') ? 'HH:mm' : 'MM-DD HH:mm')}（${humanize(b.diff(a, 'second'))}）`,
      }
    }),
})).filter((r) => r.bars.length))

// ---- 表格文字 ----
const rowClass = ({ row }: { row: Silence }) => (row.id === highlightId.value ? 'hl-row' : '')
const isLive = (s: Silence) => s.status === 'ACTIVE' || s.status === 'WAITING' || s.status === 'PENDING'
function weeklyText(s: Silence) {
  const r = s.recurrence
  const days = [...(r.weekdays ?? [])].sort().map((d) => WEEKDAYS[d - 1]?.label).join('、')
  const cross = !!r.start && !!r.end && r.end <= r.start
  return `每${days} ${r.start}–${cross ? '次日 ' : ''}${r.end}`
}
function progressText(s: Silence) {
  const n = now.value
  switch (s.status) {
    case 'ACTIVE': {
      const end = dayjs(s.next_window?.end ?? s.ends_at)
      return `剩余 ${humanize(end.diff(n, 'second'))}（${end.format('MM-DD HH:mm')} 结束）`
    }
    case 'WAITING':
    case 'PENDING': {
      const st = dayjs(s.next_window?.start ?? s.starts_at)
      return `${humanize(st.diff(n, 'second'))}后开始（${st.format('MM-DD ddd HH:mm')}）`
    }
    case 'EXPIRED': return `已于 ${fmtTime(s.ends_at, 'MM-DD HH:mm')} 结束`
    case 'CANCELLED': return `${s.cancelled_by ?? ''} 于 ${fmtTime(s.cancelled_at, 'MM-DD HH:mm')} 提前结束`
    default: return '-'
  }
}
const progressClass = (s: Silence) => (s.status === 'ACTIVE' ? 'text-ok' : s.status === 'PENDING' || s.status === 'WAITING' ? 'text-2' : 'text-3')

// ---- 操作 ----
const dialog = reactive({ visible: false, silence: null as Silence | null, copy: false })
function openDialog(s: Silence | null, copy = false) {
  Object.assign(dialog, { visible: true, silence: s, copy })
}
async function onSaved(s: Silence) {
  highlightId.value = s.id
  await load()
}
async function onCancel(s: Silence) {
  const tip = s.suppressed_now
    ? `结束后，当前被抑制的 ${s.suppressed_now} 条告警会恢复为“告警中”，仍未恢复的将补发通知。`
    : '结束后不再屏蔽匹配的告警。'
  await ElMessageBox.confirm(`${tip}确认${s.status === 'PENDING' ? '撤销' : '提前结束'}“${s.name}”？`, '确认', { type: 'warning' })
  const res = await cancelSilence(s.id)
  ElMessage.success(`已结束${res.released ? `，${res.released} 条告警已恢复通知` : ''}`)
  await load()
}
function locate(id: number) {
  highlightId.value = id
  if (!rows.value.some((r) => r.id === id)) tab.value = 'current'
  nextTick(() => document.querySelector('.hl-row')?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
}

onMounted(async () => {
  const id = Number(route.query.id)
  if (id) {
    highlightId.value = id
    await load()
    if (!current.value.some((s) => s.id === id)) tab.value = 'all'
    setTimeout(() => document.querySelector('.hl-row')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 400)
  } else {
    await load()
  }
  if (route.query.new === '1' && canManage.value) openDialog(null)
})
</script>

<style scoped lang="scss">
.tl-title { display: flex; align-items: center; gap: 16px;
  .legend { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 400; color: var(--hi-text-2);
    .lg { display: inline-block; width: 14px; height: 8px; border-radius: 2px; margin-left: 8px;
      &.m { background: #f5a623; } &.s { background: #8c9bb0; } &.now { width: 2px; height: 12px; background: var(--hi-p1); } } } }
.timeline { font-size: 12px; user-select: none;
  .axis, .tl-row { display: flex; align-items: center; }
  .label-col { flex: none; width: 260px; display: flex; align-items: center; gap: 6px; padding-right: 12px; min-width: 0;
    .ellipsis { flex: 1; min-width: 0; } }
  .track { position: relative; flex: 1; height: 26px; }
  .axis .track { height: 20px; border-bottom: 1px solid var(--hi-border); }
  .tick { position: absolute; bottom: 3px; padding-left: 6px; color: var(--hi-text-3); white-space: nowrap; }
  .tl-row { cursor: pointer; border-radius: var(--hi-radius-sm); &:hover, &.hl { background: var(--hi-bg-hover); } }
  .grid { position: absolute; top: 0; bottom: 0; border-left: 1px dashed var(--hi-border-light); }
  .past { position: absolute; left: 0; top: 0; bottom: 0; background: rgba(140, 140, 140, 0.06); }
  .bar { position: absolute; top: 7px; height: 12px; border-radius: 3px; opacity: 0.85;
    &.m { background: #f5a623; } &.s { background: #8c9bb0; }
    &.on { opacity: 1; box-shadow: 0 0 0 2px rgba(245, 166, 35, 0.25); } }
  .now { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--hi-p1); border-radius: 1px; }
}
.cnt { margin-left: 4px; font-size: 12px; opacity: 0.75; }
.name { display: flex; align-items: center; gap: 6px; min-width: 0; .ellipsis { min-width: 0; } }
.scope { line-height: 1.5; }
.ml-4 { margin-left: 4px; }
.ico { vertical-align: -2px; margin-right: 4px; color: var(--el-color-primary); }
:deep(.hl-row) { --el-table-tr-bg-color: #fff7e6; }
</style>
