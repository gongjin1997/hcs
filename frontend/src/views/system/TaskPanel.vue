<template>
  <div v-loading="loading" class="task-panel">
    <div v-if="schedule" class="sched">
      <div class="sched-row">
        <el-icon><Calendar /></el-icon>
        <span>定时报表：<template v-for="(c, k) in schedule.cron" :key="k"><b>{{ schedule.kinds[k] }}</b> {{ cronText(c) }}<span class="mono text-3">（{{ c }}）</span>　</template></span>
        <span class="text-3">时区 {{ schedule.timezone }}</span>
      </div>
      <div class="sched-row recv">
        <span class="lbl">合并报表收件人</span>
        <span v-if="schedule.mail_to.length" class="mono">{{ schedule.mail_to.join('、') }}</span>
        <span v-else class="text-3">未配置（只生成不发送，环境变量 INSPECTOR_REPORT_MAIL_TO）</span>
        <template v-for="t in schedule.tenants" :key="t.id">
          <span class="sep" /><TenantTag :id="t.id" :name="t.name" />
          <span v-if="t.mail_to.length" class="mono">{{ t.mail_to.join('、') }}</span><span v-else class="text-3">未配置收件人</span>
        </template>
      </div>
      <div v-if="schedule.note" class="sched-row text-3">{{ schedule.note }}</div>
    </div>

    <el-table :data="tasks" row-key="code" :expand-row-keys="expanded" @expand-change="onExpand">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="runs">
            <div class="sub-title">最近 {{ row.runs.length }} 次运行（每个租户各执行一次，互不影响）</div>
            <el-table :data="row.runs" size="small" border>
              <el-table-column label="开始时间" width="140"><template #default="{ row: r }">{{ fmtTime(r.started_at) }}</template></el-table-column>
              <el-table-column label="租户" width="110"><template #default="{ row: r }"><TenantTag :id="r.tenant_id" :name="r.tenant_name" /></template></el-table-column>
              <el-table-column label="触发" width="70"><template #default="{ row: r }">{{ r.trigger_type === 'MANUAL' ? '手动' : '调度' }}</template></el-table-column>
              <el-table-column label="状态" width="90"><template #default="{ row: r }"><el-tag :type="TASK_STATUS[r.status]?.type ?? 'info'" size="small">{{ TASK_STATUS[r.status]?.label ?? r.status }}</el-tag></template></el-table-column>
              <el-table-column label="耗时" width="76"><template #default="{ row: r }">{{ r.finished_at ? `${secs(r as TaskRun)} s` : '-' }}</template></el-table-column>
              <el-table-column label="资源 / 异常" width="96"><template #default="{ row: r }">{{ r.resource_total }} / <b :class="{ danger: r.abnormal_total }">{{ r.abnormal_total }}</b></template></el-table-column>
              <el-table-column label="API 调用" width="80" prop="api_calls" />
              <el-table-column label="采集明细" min-width="220"><template #default="{ row: r }"><span class="text-2">{{ statsText(row.code, r as TaskRun) || '-' }}</span></template></el-table-column>
              <el-table-column label="错误摘要" min-width="180" show-overflow-tooltip><template #default="{ row: r }"><span :class="{ danger: r.error_summary }">{{ r.error_summary ?? '-' }}</span></template></el-table-column>
            </el-table>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="200"><template #default="{ row }"><b>{{ row.name }}</b><div class="text-3 mono">{{ row.code }}</div></template></el-table-column>
      <el-table-column label="服务" width="120"><template #default="{ row }">{{ SERVICES[row.service] ?? row.service }}</template></el-table-column>
      <el-table-column label="周期" width="80"><template #default="{ row }">{{ humanize(row.interval_sec) }}</template></el-table-column>
      <el-table-column label="各租户最近一次" min-width="300">
        <template #default="{ row }">
          <div class="latest">
            <div v-for="r in latestByTenant(row as InspectionTask)" :key="r.tenant_id ?? 0" class="lt" :class="r.status.toLowerCase()">
              <TenantTag :id="r.tenant_id" :name="r.tenant_name" />
              <el-tag :type="TASK_STATUS[r.status]?.type ?? 'info'" size="small" effect="plain">{{ TASK_STATUS[r.status]?.label ?? r.status }}</el-tag>
              <span class="text-3">{{ fromNow(r.started_at) }}</span>
              <span class="cnt" title="检查对象 / 异常">检查 {{ r.resource_total }} · 异常 <b :class="{ danger: r.abnormal_total }">{{ r.abnormal_total }}</b></span>
            </div>
            <span v-if="!row.runs.length" class="text-3">未运行</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="`成功率（近 ${maxRuns} 次）`" width="150">
        <template #default="{ row }"><el-progress :percentage="successRate(row as InspectionTask)" :color="successRate(row as InspectionTask) >= 90 ? '#2e7d32' : '#f57c00'" :stroke-width="8" /></template>
      </el-table-column>
      <el-table-column label="启用" width="64"><template #default="{ row }"><el-switch :model-value="row.enabled" size="small" disabled /></template></el-table-column>
      <el-table-column label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button v-perm="'task:run'" link type="primary" :icon="VideoPlay" :loading="running === row.code" @click="run(row as InspectionTask)">立即执行</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="tip text-3">巡检 / 台账同步 / 容器事件采集由调度进程（scheduler）按周期执行，单个租户认证或接口失败只影响该租户；
      手动执行范围跟随顶部租户切换{{ app.currentTenant ? `（当前：${app.currentTenant.name}）` : '（当前：全部租户）' }}。</div>
  </div>
</template>

<script setup lang="ts">
import { Calendar, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage, ElNotification } from 'element-plus'
import { computed, h, onMounted, ref } from 'vue'
import { type InspectionTask, type ReportSchedule, type TaskRun, listTasks, reportSchedule, runTask } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { METRICS_SOURCE, SERVICES, TASK_STATUS } from '@/utils/dict'
import { fmtTime, fromNow, humanize } from '@/utils/format'

const app = useAppStore()
const user = useUserStore()
const loading = ref(false)
const tasks = ref<InspectionTask[]>([])
const schedule = ref<ReportSchedule | null>(null)
const running = ref('')
const expanded = ref<string[]>([])
const maxRuns = computed(() => Math.max(0, ...tasks.value.map((t) => t.runs.length)) || 12)

async function load() {
  loading.value = true
  try {
    const [t, s] = await Promise.all([listTasks(), user.can('report:view') ? reportSchedule() : Promise.resolve(null)])
    tasks.value = t
    schedule.value = s
    if (!expanded.value.length) expanded.value = t.filter((x) => x.runs.slice(0, 4).some((r) => r.status !== 'SUCCESS')).map((x) => x.code).slice(0, 1)
  } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })

function onExpand(_row: unknown, rows: unknown) {
  if (Array.isArray(rows)) expanded.value = (rows as InspectionTask[]).map((r) => r.code)
}

const WEEK: Record<string, string> = { mon: '一', tue: '二', wed: '三', thu: '四', fri: '五', sat: '六', sun: '日', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 0: '日', 7: '日' }
/** 常见 cron 转中文（分 时 日 月 周）；无法识别时原样返回 */
function cronText(c: string) {
  const [m, hh, d, mon, w] = c.trim().split(/\s+/)
  if (!/^\d+$/.test(m ?? '') || !/^\d+$/.test(hh ?? '') || mon !== '*') return c
  const t = `${hh.padStart(2, '0')}:${m.padStart(2, '0')}`
  if (d === '*' && w === '*') return `每天 ${t}`
  if (d === '*' && WEEK[w.toLowerCase()]) return `每周${WEEK[w.toLowerCase()]} ${t}`
  if (/^\d+$/.test(d) && w === '*') return `每月 ${d} 日 ${t}`
  return c
}

/** 每个租户最近一次运行（runs 按开始时间倒序） */
function latestByTenant(t: InspectionTask): TaskRun[] {
  const seen = new Map<number, TaskRun>()
  for (const r of t.runs) if (!seen.has(r.tenant_id ?? 0)) seen.set(r.tenant_id ?? 0, r)
  return [...seen.values()].sort((a, b) => (a.tenant_id ?? 0) - (b.tenant_id ?? 0))
}
const secs = (r: TaskRun) => Math.max(0, Math.round((new Date(r.finished_at!).getTime() - new Date(r.started_at).getTime()) / 100) / 10)
const successRate = (t: InspectionTask) => (t.runs.length ? Math.round((t.runs.filter((r) => r.status === 'SUCCESS').length / t.runs.length) * 100) : 0)

/** 各任务的附加统计（stats 字段，按任务类型解释） */
function statsText(code: string, r: TaskRun): string {
  const s = r.stats ?? {}
  if (code === 'ecs_status' && s.servers != null) {
    const src = METRICS_SOURCE[s.metrics_source]?.label ?? s.metrics_source ?? '-'
    return `指标来源 ${src} · ${s.with_metrics ?? 0}/${s.servers} 台有指标`
  }
  if (code === 'cce_pod_events' && s.events_created != null) return `新增事件 ${s.events_created} · 执行探针 ${s.probes ?? 0}`
  if (code === 'ledger_sync') {
    const vals = Object.values(s).filter((v): v is { total: number; created: number; updated: number; deleted: number; skipped_delete?: boolean } => typeof v === 'object' && v != null && 'total' in v)
    if (!vals.length) return ''
    const sum = (k: 'total' | 'created' | 'updated' | 'deleted') => vals.reduce((a, v) => a + (v[k] ?? 0), 0)
    const skipped = vals.filter((v) => v.skipped_delete).length
    return `${vals.length} 类资源 · 共 ${sum('total')} · 新增 ${sum('created')} · 变更 ${sum('updated')} · 删除 ${sum('deleted')}${skipped ? ` · ${skipped} 类保护性跳过删除` : ''}`
  }
  return ''
}

async function run(t: InspectionTask) {
  running.value = t.code
  try {
    const r = await runTask(t.code)
    const ok = r.status === 'SUCCESS'
    ElNotification({
      title: `${t.name}：${TASK_STATUS[r.status]?.label ?? r.status}`,
      type: ok ? 'success' : 'warning',
      duration: ok ? 4000 : 8000,
      message: h('div', { class: 'run-result' }, r.runs.map((x) => h('div', { style: 'line-height:1.8' }, [
        h('b', `${x.tenant_name ?? '平台'}　`),
        `${TASK_STATUS[x.status]?.label ?? x.status} · 资源 ${x.resource_total} / 异常 ${x.abnormal_total} · API ${x.api_calls} 次`,
        x.error_summary ? h('div', { style: 'color:var(--hi-p1);font-size:12px' }, x.error_summary) : null,
      ]))),
    })
    if (!r.runs.length) ElMessage.info('当前范围内没有启用的租户')
    await load()
  } finally { running.value = '' }
}
</script>

<style scoped lang="scss">
.sched { margin-bottom: 14px; padding: 10px 14px; border-radius: var(--hi-radius-sm); background: var(--hi-bg-soft); border: 1px dashed var(--hi-border);
  font-size: 13px; color: var(--hi-text-2);
  .sched-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; line-height: 1.9; .el-icon { color: var(--el-color-primary); } }
  .recv { padding-left: 22px; .lbl { color: var(--hi-text-3); } .sep { width: 1px; height: 12px; background: var(--hi-border); margin: 0 4px; } }
  .text-3 { font-size: 12px; } .sched-row.text-3 { padding-left: 22px; } }
.runs { padding: 4px 48px 12px; }
.sub-title { margin: 0 0 8px; font-weight: 600; font-size: 13px; color: var(--hi-text-2); }
.latest { display: flex; flex-direction: column; gap: 4px; padding: 2px 0;
  .lt { display: flex; align-items: center; gap: 8px; font-size: 12px; .cnt { margin-left: auto; color: var(--hi-text-2); } } }
.danger { color: var(--hi-p1); }
.tip { margin-top: 12px; font-size: 12px; }
</style>
