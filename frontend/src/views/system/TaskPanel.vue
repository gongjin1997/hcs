<template>
  <div v-loading="loading" class="task-panel">
    <div v-if="schedule" class="sched">
      <div class="sched-row">
        <el-icon><Calendar /></el-icon>
        <span>定时报表：<template v-for="(c, k) in schedule.cron" :key="k">
          <b :class="{ off: schedule.enabled?.[k] === false }">{{ schedule.kinds[k] }}</b>
          <template v-if="schedule.enabled?.[k] === false"> <el-tag size="small" type="info">已停用</el-tag></template>
          <template v-else> {{ cronText(c) }}</template><span class="mono text-3">（{{ c }}）</span>　</template></span>
        <span class="text-3">时区 {{ schedule.timezone }}<template v-if="schedule.customized && schedule.updated_by"> · {{ schedule.updated_by }} 于 {{ fmtTime(schedule.updated_at, 'MM-DD HH:mm') }} 修改</template></span>
        <el-button v-if="canReportConfig" class="ml-auto" size="small" :icon="Edit" @click="openSchedule">修改发送时间 / 收件人</el-button>
      </div>
      <div class="sched-row recv">
        <span class="lbl">合并报表收件人</span>
        <span v-if="schedule.mail_to.length" class="mono">{{ schedule.mail_to.join('、') }}</span>
        <span v-else class="text-3">未设置收件人，仅生成报表</span>
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
            <div class="sub-title">已展示 {{ row.runs.length }} 次运行（每个租户 / 项目各保留最近 12 次；未知项目单独保留）</div>
            <el-table :data="row.runs" size="small" border>
              <el-table-column label="开始时间" width="140"><template #default="{ row: r }">{{ fmtTime(r.started_at) }}</template></el-table-column>
              <el-table-column label="租户" width="110"><template #default="{ row: r }"><TenantTag :id="r.tenant_id" :name="r.tenant_name" /></template></el-table-column>
              <el-table-column label="项目" min-width="170"><template #default="{ row: r }">{{ projectLabel(r as ProjectRun) }}</template></el-table-column>
              <el-table-column label="触发" width="70"><template #default="{ row: r }">{{ r.trigger_type === 'MANUAL' ? '手动' : '调度' }}</template></el-table-column>
              <el-table-column label="状态" width="90"><template #default="{ row: r }"><el-tag :type="TASK_STATUS[r.status]?.type ?? 'info'" size="small">{{ TASK_STATUS[r.status]?.label ?? r.status }}</el-tag></template></el-table-column>
              <el-table-column label="耗时" width="76"><template #default="{ row: r }">{{ r.finished_at ? `${secs(r as TaskRun)} s` : '-' }}</template></el-table-column>
              <el-table-column :label="cnt(row.code).b ? `${cnt(row.code).a} / ${cnt(row.code).b}` : cnt(row.code).a" width="96"><template #default="{ row: r }">{{ r.resource_total }}<template v-if="cnt(row.code).b"> / <b :class="{ danger: cnt(row.code).warn && r.abnormal_total }">{{ r.abnormal_total }}</b></template></template></el-table-column>
              <el-table-column label="API 调用" width="80" prop="api_calls" />
              <el-table-column label="API 错误" width="90"><template #default="{ row: r }">{{ r.stats?.hcs?.errors ?? '-' }}</template></el-table-column>
              <el-table-column label="限流等待" width="100"><template #default="{ row: r }">{{ r.stats?.hcs ? `${Math.round(r.stats.hcs.rate_limit_ms)} ms` : '-' }}</template></el-table-column>
              <el-table-column label="采集明细" min-width="220"><template #default="{ row: r }"><span class="text-2">{{ statsText(row.code, r as TaskRun) || '-' }}</span></template></el-table-column>
              <el-table-column label="错误摘要" min-width="180" show-overflow-tooltip><template #default="{ row: r }"><span :class="{ danger: r.error_summary }">{{ r.error_summary ?? '-' }}</span></template></el-table-column>
            </el-table>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="210">
        <template #default="{ row }">
          <b>{{ row.name }}</b>
          <div class="text-3 mono">{{ row.code }}</div>
          <div v-if="row.note" class="text-3 note">{{ row.note }}</div>
          <div v-if="ECS_POLICY_TASKS.includes(row.code)" class="text-3 note">各租户可单独设置频率、范围与指标（租户策略优先于这里的全局周期）。</div>
          <div v-if="row.code === 'cce_cluster_health' && user.can('podevent:view')" class="text-3 note">采集哪些集群、启用哪些原生指标：<router-link :to="{ path: '/pod-events', query: { tab: 'clusters' } }">容器事件 → 集群与节点 → 采集配置</router-link></div>
        </template>
      </el-table-column>
      <el-table-column label="服务" width="110"><template #default="{ row }">{{ SERVICES[row.service] ?? row.service }}</template></el-table-column>
      <el-table-column label="周期" width="130">
        <template #default="{ row }">
          <template v-if="row.cron"><div>{{ cronText(row.cron) }}</div><div class="mono text-3">{{ row.cron }}</div></template>
          <template v-else>每 {{ humanize(row.interval_sec) }}</template>
        </template>
      </el-table-column>
      <el-table-column label="下次执行" width="128">
        <template #default="{ row }">
          <span v-if="!row.enabled" class="text-3">已停用</span>
          <template v-else-if="row.next_run">
            <div class="num">{{ nextText(row.next_run) }}</div>
            <el-tooltip v-if="row.next_run_source === 'estimate'" content="调度进程未上报（未运行或心跳超时），按上次执行时间 + 周期估算" placement="top">
              <el-tag size="small" type="warning" effect="plain">预计</el-tag>
            </el-tooltip>
          </template>
          <span v-else class="text-3">-</span>
        </template>
      </el-table-column>
      <el-table-column label="各租户 / 项目最近一次" min-width="380">
        <template #default="{ row }">
          <div class="latest">
            <div v-for="r in latestByTenant(row as InspectionTask)" :key="`${r.tenant_id ?? 'global'}:${r.project_ref ?? 'unknown'}`" class="lt" :class="r.status.toLowerCase()">
              <TenantTag :id="r.tenant_id" :name="r.tenant_name" />
              <span class="text-3">{{ projectLabel(r) }}</span>
              <el-tag :type="TASK_STATUS[r.status]?.type ?? 'info'" size="small" effect="plain">{{ TASK_STATUS[r.status]?.label ?? r.status }}</el-tag>
              <span class="text-3">{{ fromNow(r.started_at) }}</span>
              <span class="cnt">{{ cnt(row.code).a }} {{ r.resource_total }}<template v-if="cnt(row.code).b"> · {{ cnt(row.code).b }} <b :class="{ danger: cnt(row.code).warn && r.abnormal_total }">{{ r.abnormal_total }}</b></template></span>
            </div>
            <span v-if="!row.runs.length" class="text-3">未运行</span>
            <div v-if="row.skipped_tenants?.length" class="lt skipped">
              <el-tooltip :content="`这些租户在“采集开关”中关闭了 ${row.types.map((t: string) => typeName(t)).join('、')}，调度和手动执行都会跳过`" placement="top">
                <el-tag size="small" type="info" effect="plain">已关闭采集</el-tag>
              </el-tooltip>
              <span class="text-3">{{ row.skipped_tenants.join('、') }}</span>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="已展示执行成功率" width="150">
        <template #default="{ row }"><el-progress v-if="successRate(row as InspectionTask) != null" :percentage="successRate(row as InspectionTask) ?? 0" :color="(successRate(row as InspectionTask) ?? 0) >= 90 ? '#2e7d32' : '#f57c00'" :stroke-width="8" /><span v-else class="text-3">未知（无已完成执行）</span></template>
      </el-table-column>
      <el-table-column label="启用" width="64">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" size="small" :disabled="!canEdit" :loading="toggling === row.code"
                     @change="(v: string | number | boolean) => toggle(row as InspectionTask, !!v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-button v-perm="'task:run'" link type="primary" :icon="VideoPlay" :loading="running === row.code" :disabled="!row.enabled" @click="run(row as InspectionTask)">立即执行</el-button>
          <el-button v-if="canEdit" link type="primary" @click="openEdit(row as InspectionTask)">修改</el-button>
          <el-button v-if="ECS_POLICY_TASKS.includes(row.code)" link type="primary" @click="openEcsPolicy">租户策略</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="tip text-3">巡检 / 台账同步 / 容器事件采集由调度进程（scheduler）按周期执行，单个租户认证或接口失败只影响该租户；修改周期后调度进程最迟 30 秒生效；
      手动执行范围跟随顶部租户切换{{ app.currentTenant ? `（当前：${app.currentTenant.name}）` : '（当前：全部租户）' }}。</div>

    <!-- ECS 租户采集策略：顶部选了租户时直接编辑该租户，否则在对话框里选择租户 -->
    <EcsPolicyDialog v-model="ecsPolicy.visible" :tenant-id="ecsPolicy.tenantId" :tenant-name="ecsPolicy.tenantName" @saved="load" />

    <!-- 修改任务周期 -->
    <el-dialog v-model="edit.visible" :title="`修改任务：${edit.task?.name ?? ''}`" width="560px" append-to-body>
      <el-form v-if="edit.task" label-width="92px" @submit.prevent>
        <el-form-item label="执行方式">
          <el-radio-group v-model="edit.mode">
            <el-radio value="interval" :disabled="edit.task.code === 'data_cleanup'">按周期</el-radio>
            <el-radio value="cron">crontab 定时</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="edit.mode === 'interval'" label="周期">
          <el-input-number v-model="edit.value" :min="1" :step="1" controls-position="right" style="width: 140px" />
          <el-select v-model="edit.unit" style="width: 90px; margin-left: 8px">
            <el-option :value="1" label="秒" /><el-option :value="60" label="分钟" /><el-option :value="3600" label="小时" />
          </el-select>
          <div class="text-3 hint">最短 {{ humanize(edit.task.min_interval) }}；= {{ humanize(edit.value * edit.unit) }}。周期越短 HCS API 调用越多，建议 ≥ 1 分钟</div>
        </el-form-item>
        <el-form-item v-else label="crontab">
          <el-input v-model="edit.cron" class="mono" placeholder="分 时 日 月 周，例如 30 3 * * *" style="width: 240px" />
          <span class="text-3 hint-inline">{{ edit.cron.trim() ? cronText(edit.cron.trim()) : '' }}</span>
          <div class="presets">
            <el-button v-for="p in CRON_PRESETS" :key="p.v" size="small" @click="edit.cron = p.v">{{ p.label }}</el-button>
          </div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="edit.enabled" />
          <span class="text-3 hint-inline">停用后调度进程不再执行（手动执行也不可用）</span>
        </el-form-item>
        <el-form-item v-if="edit.task.code === 'dgc_batch_instance'" label="P90 样本数">
          <el-input-number v-model="edit.p90MinSamples" :min="1" :max="10000" :step="1" :precision="0" aria-label="P90 最少有效样本数" />
          <div class="text-3 hint">近24小时同作业成功实例的有效耗时；样本不足时基线留空。</div>
        </el-form-item>
        <div v-if="edit.task.code === 'dcs_status'" class="text-3 hint">指标来自 DCS 原生接口，历史为实际采样记录。CPU 累计秒数不作为使用率，实例连接数暂无数据。</div>
        <div v-if="edit.task.code === 'gaussdb_status'" class="text-3 hint">指标来自 GaussDB 原生接口；磁盘、慢 SQL、会话等在 GaussDB 面板查看，不使用 AOM 映射。</div>
        <el-form-item v-if="hasLegacyAom" label="旧配置">
          <el-checkbox v-model="edit.clearLegacyAom" :disabled="edit.saving">清除旧 AOM 映射</el-checkbox>
          <div class="text-3 hint">AOM 已停用，旧映射不再生效。勾选后保存清除，保留其他任务参数。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="edit.visible = false">取消</el-button>
        <el-button type="primary" :loading="edit.saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 修改定时报表 -->
    <el-dialog v-model="sched.visible" title="定时报表设置" width="640px" append-to-body>
      <el-form label-width="110px" @submit.prevent>
        <el-form-item v-for="(_c, k) in schedule?.cron ?? {}" :key="k" :label="schedule?.kinds[k] ?? String(k)">
          <el-switch v-model="sched.enabled[k]" />
          <el-input v-model="sched.cron[k]" class="mono cron-in" :disabled="!sched.enabled[k]" placeholder="分 时 日 月 周" />
          <span class="text-3 hint-inline">{{ sched.enabled[k] ? cronText(sched.cron[k] ?? '') : '不自动生成' }}</span>
        </el-form-item>
        <el-form-item label="合并报表收件人">
          <RecipientSelect v-model="sched.recipient_user_ids" global-only :active="sched.visible" :disabled="sched.saving" />
          <div class="text-3 hint">各租户报表发给“租户接入”里配置的租户收件人；这里是全部租户合并报表的收件人</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sched.visible = false">取消</el-button>
        <el-button type="primary" :loading="sched.saving" @click="saveSchedule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Calendar, Edit, VideoPlay } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { computed, h, onMounted, reactive, ref } from 'vue'
import { type InspectionTask, type ReportSchedule, type TaskRun, listTasks, reportSchedule, runTask, saveReportSchedule, updateTask } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import RecipientSelect from '@/components/RecipientSelect.vue'
import EcsPolicyDialog from '@/views/ecs/EcsPolicyDialog.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { METRICS_SOURCE, SERVICES, TASK_STATUS, historicalMetricsSourceLabel, typeName } from '@/utils/dict'
import { fmtTime, fromNow, humanize } from '@/utils/format'

const app = useAppStore()
const user = useUserStore()
const globalConfigScope = computed(() => !!user.user && user.user.tenant_ids == null)
const canEdit = computed(() => globalConfigScope.value && user.can('task:edit'))
const canReportConfig = computed(() => globalConfigScope.value && user.can('report:config') && !!schedule.value?.editable)
const loading = ref(false)
/** 这些任务按租户 ECS 采集策略调度（collection_policy.TASK_PHASES）；策略原先放在“租户接入”卡片上 */
const ECS_POLICY_TASKS = ['ecs_status', 'ecs_metrics', 'ecs_quota']
const ecsPolicy = reactive({ visible: false, tenantId: null as number | null, tenantName: '' })
function openEcsPolicy() {
  const tenant = app.currentTenant
  Object.assign(ecsPolicy, { visible: true, tenantId: tenant?.id ?? null, tenantName: tenant ? tenant.short_name || tenant.name : '' })
}
const tasks = ref<InspectionTask[]>([])
const schedule = ref<ReportSchedule | null>(null)
const running = ref('')
const expanded = ref<string[]>([])
type ProjectRun = TaskRun & { project_ref?: number | null; project_scope?: string; project_label?: string }
const projectLabel = (r: ProjectRun) => r.project_label ?? (r.project_ref != null ? `项目#${r.project_ref}` : '历史未知项目（未记录）')

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

/** 下次执行：1 分钟内显示“即将执行”，当天显示时间，否则显示日期 */
function nextText(v: string) {
  const d = dayjs(v)
  if (d.diff(dayjs(), 'second') <= 60) return '即将执行'
  return d.isSame(dayjs(), 'day') ? d.format('HH:mm:ss') : d.format('MM-DD HH:mm')
}
const CRON_PRESETS = [
  { v: '30 3 * * *', label: '每天 03:30' }, { v: '0 */6 * * *', label: '每 6 小时' }, { v: '0 8 * * *', label: '每天 08:00' },
  { v: '0 9 * * 1', label: '每周一 09:00' }, { v: '0 9 1 * *', label: '每月 1 日 09:00' },
]

// ---- 修改任务 ----
const edit = reactive({ visible: false, task: null as InspectionTask | null, mode: 'interval' as 'interval' | 'cron', value: 1, unit: 60, cron: '', enabled: true, saving: false,
  clearLegacyAom: false, p90MinSamples: 5,
})
const hasLegacyAom = computed(() => !!edit.task && Object.prototype.hasOwnProperty.call(edit.task.params ?? {}, 'aom')
  && (edit.task.params as { aom?: unknown }).aom != null)
function openEdit(t: InspectionTask) {
  if (edit.saving) return
  const sec = t.interval_sec
  const unit = sec % 3600 === 0 ? 3600 : sec % 60 === 0 ? 60 : 1
  Object.assign(edit, { visible: true, task: t, mode: t.cron ? 'cron' : 'interval', value: sec / unit, unit, cron: t.cron ?? '', enabled: t.enabled,
    clearLegacyAom: false, p90MinSamples: (t.params as { p90_min_samples?: number }).p90_min_samples ?? 5,
  })
}
async function saveEdit() {
  if (edit.saving || !edit.task || !canEdit.value) return
  const t = edit.task
  const body: { interval_sec?: number; cron?: string | null; enabled?: boolean; aom?: null; p90_min_samples?: number } = { enabled: edit.enabled }
  if (t.code === 'dgc_batch_instance') {
    if (!Number.isInteger(edit.p90MinSamples) || edit.p90MinSamples < 1 || edit.p90MinSamples > 10000) return ElMessage.warning('P90 最少样本数须为 1–10000 的整数')
    body.p90_min_samples = edit.p90MinSamples
  }
  if (hasLegacyAom.value && edit.clearLegacyAom) body.aom = null
  if (edit.mode === 'interval') {
    const sec = Math.round(edit.value * edit.unit)
    if (sec < t.min_interval) return ElMessage.warning(`周期不能小于 ${humanize(t.min_interval)}`)
    Object.assign(body, { interval_sec: sec, cron: '' })
  } else {
    if (!edit.cron.trim()) return ElMessage.warning('请填写 crontab 表达式')
    body.cron = edit.cron.trim()
  }
  edit.saving = true
  try {
    const r = await updateTask(t.code, body)
    ElMessage.success(r.changed ? '已保存，调度进程最迟 30 秒生效' : '没有变化')
    edit.visible = false
    await load()
  } finally { edit.saving = false }
}
const toggling = ref('')
async function toggle(t: InspectionTask, on: boolean) {
  if (!canEdit.value || toggling.value) return
  if (!on) await ElMessageBox.confirm(`停用后“${t.name}”不再自动执行，相关检查项不会产生新告警。确认停用？`, '停用任务', { type: 'warning' })
  toggling.value = t.code
  try {
    await updateTask(t.code, { enabled: on })
    ElMessage.success(on ? '已启用' : '已停用')
    await load()
  } finally { toggling.value = '' }
}

// ---- 定时报表 ----
const sched = reactive({ visible: false, cron: {} as Record<string, string>, enabled: {} as Record<string, boolean>, recipient_user_ids: [] as number[], saving: false })
function openSchedule() {
  const s = schedule.value!
  Object.assign(sched, {
    visible: true, cron: { ...s.cron }, recipient_user_ids: [...(s.recipient_user_ids ?? [])],
    enabled: Object.fromEntries(Object.keys(s.cron).map((k) => [k, s.enabled?.[k] !== false])),   // 只有日 / 周 / 月报定时生成；台账报表为手动生成
  })
}
async function saveSchedule() {
  if (sched.saving || !canReportConfig.value) return
  sched.saving = true
  try {
    schedule.value = await saveReportSchedule({ cron: sched.cron, enabled: sched.enabled, recipient_user_ids: [...sched.recipient_user_ids] })
    ElMessage.success('已保存，调度进程最迟 30 秒按新时间生成报表')
    sched.visible = false
  } finally { sched.saving = false }
}

/** 每个租户 / 项目最近一次；空项目是独立的历史未知范围。 */
function latestByTenant(t: InspectionTask): ProjectRun[] {
  const seen = new Map<string, ProjectRun>()
  for (const r of t.runs as ProjectRun[]) {
    const key = `${r.tenant_id ?? 'global'}:${r.project_ref ?? 'unknown'}`
    if (!seen.has(key)) seen.set(key, r)
  }
  return [...seen.values()].sort((a, b) => (a.tenant_id ?? 0) - (b.tenant_id ?? 0)
    || (a.project_ref ?? 0) - (b.project_ref ?? 0))
}
const secs = (r: TaskRun) => Math.max(0, Math.round((new Date(r.finished_at!).getTime() - new Date(r.started_at).getTime()) / 100) / 10)
function successRate(t: InspectionTask): number | null {
  const completed = t.runs.filter(r => !['RUNNING', 'SKIPPED'].includes(r.status))
  return completed.length ? Math.round(completed.filter(r => r.status === 'SUCCESS').length / completed.length * 100) : null
}

/** 各任务的附加统计（stats 字段，按任务类型解释） */
function statsText(code: string, r: TaskRun): string {
  const s = r.stats ?? {}
  if (code === 'ecs_status' && s.servers != null) {
    const src = s.metrics_source === 'AOM' ? historicalMetricsSourceLabel(s.metrics_source) : METRICS_SOURCE[s.metrics_source]?.label ?? s.metrics_source ?? '-'
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

/** 运行统计口径：台账同步的第二个数是“本次变更数”（不是异常），数据清理只有“清理条数” */
function cnt(code: string): { a: string; b: string; warn: boolean } {
  if (code === 'ledger_sync') return { a: '资源', b: '变更', warn: false }
  if (code === 'data_cleanup') return { a: '清理', b: '', warn: false }
  return { a: '检查', b: '异常', warn: true }
}

async function run(t: InspectionTask) {
  if (running.value) return
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
        `${TASK_STATUS[x.status]?.label ?? x.status} · ${cnt(t.code).a} ${x.resource_total}${cnt(t.code).b ? ` / ${cnt(t.code).b} ${x.abnormal_total}` : ''} · API ${x.api_calls} 次`,
        x.error_summary ? h('div', { style: 'color:var(--hi-p1);font-size:12px' }, x.error_summary) : null,
      ]))),
    })
    if (r.skipped?.length) ElMessage.info(`已跳过关闭采集的租户：${r.skipped.join('、')}`)
    else if (!r.runs.length) ElMessage.info('当前范围内没有启用的租户')
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
  .lt { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; font-size: 12px;
    > .text-3, .cnt { white-space: nowrap; }
    .cnt { color: var(--hi-text-2); }
  } }
.danger { color: var(--hi-p1); }
.tip { margin-top: 12px; font-size: 12px; }
.note { margin-top: 2px; font-size: 12px; line-height: 1.5; }
.off { color: var(--hi-text-3); text-decoration: line-through; }
.lt.skipped { padding-top: 2px; }
.hint { width: 100%; margin-top: 4px; font-size: 12px; line-height: 1.6; }
.hint-inline { margin-left: 10px; font-size: 12px; }
.presets { display: flex; flex-wrap: wrap; gap: 6px; width: 100%; margin-top: 8px; .el-button + .el-button { margin-left: 0; } }
.cron-in { width: 180px; margin-left: 12px; }
</style>
