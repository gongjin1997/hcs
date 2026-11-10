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

    <div class="workspace-toolbar"><el-radio-group v-model="workspaceView" aria-label="任务工作区"><el-radio-button value="definitions">任务与配置</el-radio-button><el-radio-button value="executions">执行工作区</el-radio-button></el-radio-group><span class="text-3">定义 {{ tasks.length }} 项 · 活跃 {{ activeExecutionCount }} 条</span></div>
    <el-alert v-if="definitionError" :title="definitionError" type="warning" :closable="false" class="mb-12"><el-button link @click="load">重新读取任务</el-button></el-alert>
    <template v-if="workspaceView === 'definitions'">
    <div class="task-filters" role="search" aria-label="筛选巡检任务">
      <el-select v-model="filters.service" aria-label="按服务筛选" placeholder="全部服务" class="service-filter">
        <el-option label="全部服务" value="" />
        <el-option v-for="service in serviceOptions" :key="service.value" :value="service.value" :label="service.label" />
      </el-select>
      <el-select v-model="filters.enabled" aria-label="按启停筛选" class="enabled-filter">
        <el-option label="全部启停状态" value="all" /><el-option label="已启用" value="enabled" /><el-option label="已停用" value="disabled" />
      </el-select>
      <el-checkbox v-model="filters.abnormal" title="按各租户 / 项目最近一次执行判断；运行中和跳过不计异常">最近异常</el-checkbox>
      <el-input v-model="filters.keyword" clearable aria-label="搜索巡检任务" placeholder="搜索任务、检查项或执行步骤" class="task-search" />
      <el-button @click="resetFilters">重置</el-button>
      <span class="text-3 filter-count" role="status" aria-live="polite">显示 {{ filteredTasks.length }} / {{ tasks.length }} 项</span>
    </div>
    <div v-if="filters.abnormal" class="text-3 filter-hint">异常按各租户 / 项目最近一次执行判断：失败、超时、部分成功、检查异常或详情失败；运行中和跳过不计入。</div>
    <el-empty v-if="!filteredTasks.length" :description="tasks.length ? '无匹配任务，请调整筛选条件' : '暂无巡检任务'" :image-size="72" />
    <div class="task-grid" aria-label="任务定义与配置">
      <TaskDefinitionCard v-for="task in filteredTasks" :key="task.code" :task="task" :service-name="serviceLabel(task.service)"
        :cadence="task.cron ? cronText(task.cron) : `每 ${humanize(task.interval_sec)}`" :scope-label="task.code === 'data_cleanup' ? '平台全局内部任务' : definitionScope"
        :resource-label="task.types.map(typeName).join('、') || '平台任务'" :next-run-label="task.next_run ? nextText(task.next_run) : '后端未上报'"
        :checks="task.details.checks" :steps="task.details.steps" :checks-provided="task.details.provided"
        :can-edit="canEdit" :can-run="user.can('task:run') && (task.code !== 'data_cleanup' || globalConfigScope)" :submitting="executions.pendingTasks.has(task.code)"
        :toggling="toggling === task.code" :active="(task.active_execution_count ?? 0) > 0" :timeout-label="taskTimeout(task)"
        :has-ecs-policy="ECS_POLICY_TASKS.includes(task.code)" :has-job-selection="!!DATAARTS_SELECTION_TASKS[task.code] && user.can('collect:manage')"
        @detail="selectedTaskCode = task.code" @run="run(task)" @configure="openEdit(task)" @toggle="toggle(task, $event)"
        @ecs="openEcsPolicy" @jobs="openJobSelection(task.code)" @scope="scopeTask = task" @select-targets="executions.chooseTargets(task.code)">
        <template #latest>
          <div class="latest">
            <div v-if="task.latest_execution" class="lt"><el-tag :type="taskStatus(task.latest_execution.status).type" size="small">{{ taskStatus(task.latest_execution.status).label }}</el-tag><span>{{ task.latest_execution.phase_label }}</span><el-button link type="primary" @click="executions.open(task.latest_execution.execution_id)">打开最近执行</el-button></div>
            <div v-for="r in latestByTenant(task).slice(0, 2)" :key="`${r.tenant_id ?? 'global'}:${r.project_ref ?? 'unknown'}`" class="lt">
              <TenantTag :id="r.tenant_id" :name="r.tenant_name" /><span class="text-3">{{ projectLabel(r) }}</span>
              <el-tag :type="taskStatus(r.status).type ?? 'info'" size="small" effect="plain">{{ taskStatus(r.status).label }}</el-tag>
              <span class="text-3">{{ fromNow(r.started_at) }}</span>
            </div>
            <span v-if="!task.runs.length" class="text-3">暂无执行记录</span>
            <span v-if="latestByTenant(task).length > 2" class="text-3">另有 {{ latestByTenant(task).length - 2 }} 个租户 / 项目，打开详情查看</span>
            <span v-if="task.skipped_tenants?.length" class="text-3">已关闭采集：{{ task.skipped_tenants.join('、') }}</span>
          </div>
        </template>
      </TaskDefinitionCard>
    </div>
    </template>
    <section v-else class="execution-workspace">
      <div class="execution-filters"><el-select v-model="executions.taskCode.value" aria-label="筛选执行任务" placeholder="全部任务" clearable><el-option v-for="task in tasks" :key="task.code" :value="task.code" :label="`${serviceLabel(task.service)} · ${task.name}`" /></el-select><el-select v-model="executions.projectRef.value" aria-label="筛选执行项目" placeholder="全部项目" clearable @clear="executions.projectRef.value = null"><el-option v-for="project in executionProjects" :key="project.ref" :value="project.ref" :label="project.label" /></el-select></div>
      <TaskExecutionList :items="executions.feed.data.value?.items ?? []" :mode="executions.mode.value" :queue-status="executions.queueStatus.value"
        :total="executions.feed.data.value?.total ?? 0" :page="executions.page.value" :page-size="executions.pageSize" :loading="executions.feed.loading.value" :error="executions.feedError.value"
        :can-cancel="user.can('task:run')" :can-retry="user.can('task:run')" :pending-actions="executions.pendingActions"
        @update:mode="executions.mode.value = $event" @update:queue-status="executions.queueStatus.value = $event" @update:page="executions.page.value = $event"
        @open="executions.open($event.execution_id)" @cancel="executions.cancel" @retry="executions.retry" @refresh="executions.feed.refresh" />
    </section>
    <TaskExecutionDrawer :model-value="!!executions.selectedId.value" :execution="executions.detail.data.value" :loading="executions.detail.loading.value" :error="executions.detailError.value"
      :can-cancel="user.can('task:run')" :can-retry="user.can('task:run')" :pending-action="executions.pendingActions[executions.selectedId.value]"
      @update:model-value="!$event && (executions.selectedId.value = '')" @refresh="executions.detail.refresh"
      @cancel="executions.detail.data.value && executions.cancel(executions.detail.data.value)" @retry="executions.detail.data.value && executions.retry(executions.detail.data.value)" />
    <TaskRunTargetDialog :model-value="!!executions.targetTaskCode.value" :task="executionTargetTask" :targets="executions.targets.data.value" :loading="executions.targets.loading.value"
      :submitting="executions.pendingTasks.has(executions.targetTaskCode.value)" :error="executions.targetError.value" :can-run="user.can('task:run')"
      @update:model-value="!$event && (executions.targetTaskCode.value = '')" @submit="executionTargetTask && run(executionTargetTask, $event)" @refresh="executions.targets.refresh" @existing="executions.open" />
    <TaskDefinitionDrawer :model-value="!!selectedTask" :task="selectedTask" :service-name="selectedTask ? serviceLabel(selectedTask.service) : ''"
      :cadence="selectedTask ? selectedTask.cron ? cronText(selectedTask.cron) : `每 ${humanize(selectedTask.interval_sec)}` : ''"
      :scope-label="selectedTask?.code === 'data_cleanup' ? '平台全局内部任务' : definitionScope" :checks="selectedTask?.details.checks ?? []" :steps="selectedTask?.details.steps ?? []" :checks-provided="selectedTask?.details.provided" :can-edit="canEdit" :can-configure-cce="user.can('podevent:view')" :timeout-label="selectedTask ? taskTimeout(selectedTask) : ''"
      @update:model-value="!$event && (selectedTaskCode = '')" @configure="selectedTask && openEdit(selectedTask)" @scope="scopeTask = selectedTask">
      <template #history><TaskRunHistory v-if="selectedTask" :task="selectedTask" :describe-run="statsText" :status="taskStatus" :project-label="projectLabel" @open-execution="executions.open" /></template>
    </TaskDefinitionDrawer>
    <TaskScopeDialog :model-value="!!scopeTask" :task="scopeTask" :tenant-id="app.currentTenant?.id ?? null" :can-manage="user.can('collect:manage')"
      @update:model-value="!$event && (scopeTask = null)" @saved="load" />
    <div class="tip text-3">巡检 / 台账同步 / 容器事件采集由调度进程（scheduler）按周期执行，单个租户认证或接口失败只影响该租户；修改周期后调度进程最迟 30 秒生效；
      手动执行范围跟随顶部租户切换{{ app.currentTenant ? `（当前：${app.currentTenant.name}）` : '（当前：全部租户）' }}。</div>

    <!-- ECS 租户采集策略：顶部选了租户时直接编辑该租户，否则在对话框里选择租户 -->
    <EcsPolicyDialog v-model="ecsPolicy.visible" :tenant-id="ecsPolicy.tenantId" :tenant-name="ecsPolicy.tenantName" @saved="load" />
    <JobSelectionDialog v-model="jobSelection.visible" :resource-type="jobSelection.type" :tenant-id="jobSelection.tenantId" @saved="load" />

    <!-- 修改任务周期 -->
    <el-dialog v-model="edit.visible" :title="`修改任务：${edit.task?.name ?? ''}`" width="min(560px, calc(100vw - 24px))" append-to-body>
      <el-form v-if="edit.task" label-width="92px" @submit.prevent>
        <el-form-item label="执行方式">
          <el-radio-group v-model="edit.mode">
            <el-radio value="interval" :disabled="edit.task.code === 'data_cleanup'">按周期</el-radio>
            <el-radio value="cron">crontab 定时</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="edit.mode === 'interval'" label="周期">
          <el-input-number v-model="edit.value" :min="1" :step="1" controls-position="right" class="interval-value" />
          <el-select v-model="edit.unit" class="interval-unit">
            <el-option :value="1" label="秒" /><el-option :value="60" label="分钟" /><el-option :value="3600" label="小时" />
          </el-select>
          <div class="text-3 hint">最短 {{ humanize(edit.task.min_interval) }}；= {{ humanize(edit.value * edit.unit) }}。周期越短 HCS API 调用越多，建议 ≥ 1 分钟</div>
        </el-form-item>
        <el-form-item v-else label="crontab">
          <el-input v-model="edit.cron" class="mono cron-expression" placeholder="分 时 日 月 周，例如 30 3 * * *" />
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
        <div v-if="isGaussTask(edit.task.code)" class="text-3 hint">GaussDB 实例状态、磁盘容量、组件状态分别启停并设置周期；以本任务检查项为准。使用原生只读接口，不使用 AOM 映射；慢 SQL、会话、巡检报告在 GaussDB 页面按需查询。</div>
        <TaskParameterFields v-if="edit.task.parameter_schema?.properties" v-model="edit.parameters" :schema="edit.task.parameter_schema" :disabled="!canEdit || edit.saving" :exclude="['p90_min_samples']" />
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
    <el-dialog v-model="sched.visible" title="定时报表设置" width="min(640px, calc(100vw - 24px))" append-to-body>
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
import { Calendar, Edit } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { type InspectionTask, type ReportSchedule, type TaskRun, reportSchedule, saveReportSchedule } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import RecipientSelect from '@/components/RecipientSelect.vue'
import EcsPolicyDialog from '@/views/ecs/EcsPolicyDialog.vue'
import JobSelectionDialog from './JobSelectionDialog.vue'
import TaskDefinitionCard from './TaskDefinitionCard.vue'
import TaskDefinitionDrawer from './TaskDefinitionDrawer.vue'
import TaskRunHistory from './TaskRunHistory.vue'
import TaskScopeDialog from './TaskScopeDialog.vue'
import TaskExecutionList from './TaskExecutionList.vue'
import TaskExecutionDrawer from './TaskExecutionDrawer.vue'
import TaskRunTargetDialog from './TaskRunTargetDialog.vue'
import TaskParameterFields from './TaskParameterFields.vue'
import * as taskApi from '@/api/task-executions'
import type { TaskDefinition, TaskDefinitionPatch, TaskParameterValue } from '@/types/task-executions'
import { useTaskExecutions } from './composables/useTaskExecutions'
import { executionStatus } from './composables/taskExecutionPresentation'
import { taskParameterErrors } from './composables/taskParameters'
import { newRequestId } from '@/utils/request-id'
import type { JobSelectionType } from '@/api/job-selection'
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
let loadVersion = 0
let disposed = false
const requestScope = () => JSON.stringify([app.tenantId ?? app.currentTenant?.id ?? null, app.tenantSeq ?? 0, user.token ?? null, user.user?.id ?? null, user.user?.tenant_ids ?? null, user.user?.permissions ?? []])
let definitionTimer: ReturnType<typeof setTimeout> | undefined
let definitionController: AbortController | undefined
onBeforeUnmount(() => { disposed = true; ++loadVersion; clearTimeout(definitionTimer); definitionController?.abort() })
/** 这些任务按租户 ECS 采集策略调度（collection_policy.TASK_PHASES）；策略原先放在“租户接入”卡片上 */
const ECS_POLICY_TASKS = ['ecs_status', 'ecs_metrics', 'ecs_quota']
const DATAARTS_SELECTION_TASKS: Record<string, JobSelectionType> = {
  dgc_realtime_status: 'dgc_realtime_job', dgc_batch_instance: 'dgc_batch_job',
}
const jobSelection = reactive({ visible: false, type: 'dgc_realtime_job' as JobSelectionType, tenantId: null as number | null })
function openJobSelection(code: string) {
  const type = DATAARTS_SELECTION_TASKS[code]
  if (!type || !user.can('collect:manage')) return
  Object.assign(jobSelection, { visible: true, type, tenantId: app.currentTenant?.id ?? null })
}
const ecsPolicy = reactive({ visible: false, tenantId: null as number | null, tenantName: '' })
function openEcsPolicy() {
  const tenant = app.currentTenant
  Object.assign(ecsPolicy, { visible: true, tenantId: tenant?.id ?? null, tenantName: tenant ? tenant.short_name || tenant.name : '' })
}
const tasks = ref<TaskDefinition[]>([])
const workspaceView = ref<'definitions' | 'executions'>('definitions')
const definitionError = ref('')
const activeExecutionCount = computed(() => tasks.value.reduce((total, task) => total + (task.active_execution_count ?? 0), 0))
const taskTimeout = (task: TaskDefinition) => {
  const seconds = task.effective_params?.timeout_sec ?? field(task.params, 'timeout_sec')
  return typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0 ? `${seconds} 秒` : ''
}
const selectedTaskCode = ref('')
const selectedTask = computed(() => taskRows.value.find(task => task.code === selectedTaskCode.value) ?? null)
const scopeTask = ref<InspectionTask | null>(null)
const definitionScope = computed(() => `${app.currentTenant?.name ?? '可见租户'} · 各已启用项目`)
// 仅解释白名单字段，未知 code 按纯文本显示，不从对象原型取字典项。
const own = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const field = (value: unknown, key: string): unknown => record(value) && own(value, key) ? value[key] : undefined
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : []
const serviceLabel = (code: string) => own(SERVICES, code) ? SERVICES[code]! : code
const taskStatus = (code: string) => own(TASK_STATUS, code) ? TASK_STATUS[code]! : executionStatus(code)
const CHECK_LABELS = new Map<string, string>([
  ['job_status', '作业状态'], ['node_status', '节点状态'], ['instance_status', '实例状态'], ['cluster_status', '集群状态'],
  ['cpu', 'CPU 使用率'], ['memory', '内存使用率'], ['disk', '磁盘使用率'], ['components', '组件状态'], ['nodes', '逻辑节点状态'], ['reboot', '重启检测'],
  ['pod_restart', '容器重启'], ['pod_oom', '容器内存溢出'], ['pod_crashloop', '容器循环崩溃'], ['pod_backpressure', '容器背压'], ['pod_evicted', '容器驱逐'],
])
function taskDetails(task: InspectionTask) {
  const checks = field(task, 'checks')
  const codes = strings(checks)
  return { codes, provided: Array.isArray(checks), checks: codes.map(code => CHECK_LABELS.get(code) ?? code), steps: strings(field(task, 'steps')) }
}
const GAUSS_TASKS = new Set(['gaussdb_status', 'gaussdb_storage', 'gaussdb_components'])
const isGaussTask = (code: string) => GAUSS_TASKS.has(code)
const filters = reactive({ service: '', enabled: 'all', abnormal: false, keyword: '' })
const serviceOptions = computed(() => [...new Set(tasks.value.map(task => task.service))].map(value => ({ value, label: serviceLabel(value) })))
const taskRows = computed(() => tasks.value.map(task => ({ ...task, details: taskDetails(task) })))
function hasRecentAbnormal(task: InspectionTask) {
  return latestByTenant(task).some(run => !['QUEUED', 'RUNNING', 'SKIPPED'].includes(run.status) && (
    ['FAILED', 'TIMEOUT', 'PARTIAL', 'INTERRUPTED'].includes(run.status)
    || (cnt(task.code).warn && run.abnormal_total > 0)
    || (isGaussTask(task.code) && validCount(field(field(run.stats, 'gaussdb'), 'failed')) > 0)
  ))
}
const filteredTasks = computed(() => {
  const terms = filters.keyword.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return taskRows.value.filter(task => {
    if (filters.service && task.service !== filters.service) return false
    if (filters.enabled === 'enabled' && !task.enabled || filters.enabled === 'disabled' && task.enabled) return false
    if (filters.abnormal && !hasRecentAbnormal(task)) return false
    const text = [task.name, task.code, task.service, serviceLabel(task.service), task.note, ...task.details.codes, ...task.details.checks, ...task.details.steps].join(' ').toLocaleLowerCase()
    return terms.every(term => text.includes(term))
  })
})
function resetFilters() { Object.assign(filters, { service: '', enabled: 'all', abnormal: false, keyword: '' }) }
// 缺失、非有限数、负数与数值字符串均不冒充有效统计。
function validCount(value: unknown): number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : -1 }
function gaussStatsText(code: string, stats: unknown): string {
  if (!isGaussTask(code)) return ''
  const gauss = field(stats, 'gaussdb')
  if (!record(gauss)) return ''
  const phaseCode = field(gauss, 'phase')
  const phase = typeof phaseCode === 'string' ? new Map([['status', '实例状态'], ['storage', '磁盘容量'], ['components', '组件状态']]).get(phaseCode) ?? '未知阶段' : '未知阶段'
  const count = (key: string) => {
    const value = validCount(field(gauss, key))
    return value >= 0 ? String(value) : '—'
  }
  const ms = (key: string) => {
    const value = field(gauss, key)
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${value} ms` : '—'
  }
  return [phase, `实例 ${count('instances')}`, `在线 ${count('online')}`, `已查询 ${count('queried')}`, `详情失败 ${count('failed')}`, `并发 ${count('concurrency')}`,
    `清单 ${ms('list_ms')}`, `明细 ${ms('detail_ms')}`, `详情累计 ${ms('detail_total_ms')}`, `单次详情最大 ${ms('detail_max_ms')}`, `墙钟 ${ms('wall_ms')}`].join(' · ')
}
const schedule = ref<ReportSchedule | null>(null)
const running = computed(() => [...executions.pendingTasks][0] ?? '')
const expanded = ref<string[]>([])
type ProjectRun = TaskRun & { project_ref?: number | null; project_scope?: string; project_label?: string }
const projectLabel = (r: ProjectRun) => r.project_label ?? (r.project_ref != null ? `项目#${r.project_ref}` : '历史未知项目（未记录）')

async function load() {
  if (disposed) return
  const request = ++loadVersion
  const scope = requestScope()
  const isCurrent = () => !disposed && request === loadVersion && scope === requestScope()
  clearTimeout(definitionTimer)
  definitionController?.abort()
  const controller = new AbortController()
  definitionController = controller
  loading.value = !tasks.value.length
  const [taskResult, scheduleResult] = await Promise.allSettled([
    taskApi.listTaskDefinitions({ tenantId: app.tenantId ?? null, signal: controller.signal }),
    user.can('report:view') ? reportSchedule() : Promise.resolve(null),
  ])
  if (!isCurrent()) return
  if (taskResult.status === 'fulfilled') {
    tasks.value = taskResult.value
    definitionError.value = ''
    if (!expanded.value.length) expanded.value = tasks.value.filter(hasRecentAbnormal).map(task => task.code).slice(0, 1)
  } else definitionError.value = '任务读取失败，当前保留上次后端结果；请重试。'
  if (scheduleResult.status === 'fulfilled') schedule.value = scheduleResult.value
  loading.value = false
  definitionTimer = setTimeout(() => { void load() }, 5000)
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
  if (d === '*' && w && own(WEEK, w.toLowerCase())) return `每周${WEEK[w.toLowerCase()]} ${t}`
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
const edit = reactive({ visible: false, task: null as TaskDefinition | null, mode: 'interval' as 'interval' | 'cron', value: 1, unit: 60, cron: '', enabled: true, saving: false,
  clearLegacyAom: false, p90MinSamples: 5, parameters: {} as Record<string, TaskParameterValue>,
})
const hasLegacyAom = computed(() => !!edit.task && Object.prototype.hasOwnProperty.call(edit.task.params ?? {}, 'aom')
  && (edit.task.params as { aom?: unknown }).aom != null)
function openEdit(t: TaskDefinition) {
  if (!canEdit.value || edit.saving) return
  const sec = t.interval_sec
  const unit = sec % 3600 === 0 ? 3600 : sec % 60 === 0 ? 60 : 1
  Object.assign(edit, { visible: true, task: t, mode: t.cron ? 'cron' : 'interval', value: sec / unit, unit, cron: t.cron ?? '', enabled: t.enabled,
    clearLegacyAom: false, p90MinSamples: t.effective_params?.p90_min_samples ?? (t.params as { p90_min_samples?: number }).p90_min_samples ?? 5,
    parameters: Object.fromEntries(Object.entries(t.parameter_schema?.properties ?? {}).flatMap(([key, spec]) => {
      const value = t.effective_params && own(t.effective_params, key) ? t.effective_params[key] : field(t.params, key) ?? spec.default
      return value === null || typeof value === 'number' || typeof value === 'string' ? [[key, value]] : []
    })),
  })
}
async function saveEdit() {
  if (edit.saving || !edit.task || !canEdit.value) return
  const t = edit.task
  const scope = requestScope()
  const body: TaskDefinitionPatch = { enabled: edit.enabled }
  const parameterErrors = t.parameter_schema ? taskParameterErrors(edit.parameters, t.parameter_schema) : []
  if (parameterErrors.length) return ElMessage.warning(parameterErrors.join('；'))
  if (typeof edit.parameters.timeout_sec === 'number') body.timeout_sec = edit.parameters.timeout_sec
  if (t.code === 'mrs_job_status' && t.parameter_schema?.properties?.query_mode) body.mrs_options = Object.fromEntries(Object.entries(edit.parameters).filter(([key]) => key !== 'timeout_sec' && key !== 'p90_min_samples'))
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
    const r = await taskApi.updateTaskDefinition(t.code, body, { tenantId: null })
    if (disposed || scope !== requestScope()) return
    ElMessage.success(r.changed ? '已保存，调度进程最迟 30 秒生效' : '没有变化')
    edit.visible = false
    await load()
  } catch (error) {
    if (!disposed && scope === requestScope()) ElMessage.error(error instanceof Error ? error.message : '任务配置保存失败，请重试')
  } finally { if (!disposed && scope === requestScope()) edit.saving = false }
}
const toggling = ref('')
async function toggle(t: InspectionTask, on: boolean) {
  if (!canEdit.value || toggling.value) return
  const scope = requestScope()
  if (!on) {
    try { await ElMessageBox.confirm(`停用后“${t.name}”不再自动执行，相关检查项不会产生新告警。确认停用？`, '停用任务', { type: 'warning' }) }
    catch { return }
  }
  if (disposed || scope !== requestScope() || !canEdit.value || toggling.value) return
  toggling.value = t.code
  try {
    await taskApi.updateTaskDefinition(t.code, { enabled: on }, { tenantId: null })
    if (disposed || scope !== requestScope()) return
    ElMessage.success(on ? '已启用' : '已停用')
    await load()
  } catch (error) {
    if (!disposed && scope === requestScope()) ElMessage.error(error instanceof Error ? error.message : '任务启停更新失败，请重试')
  } finally { if (!disposed && scope === requestScope()) toggling.value = '' }
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
  const scope = requestScope()
  sched.saving = true
  try {
    const saved = await saveReportSchedule({ cron: sched.cron, enabled: sched.enabled, recipient_user_ids: [...sched.recipient_user_ids] })
    if (disposed || scope !== requestScope()) return
    schedule.value = saved
    ElMessage.success('已保存，调度进程最迟 30 秒按新时间生成报表')
    sched.visible = false
  } finally { if (!disposed && scope === requestScope()) sched.saving = false }
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
const secs = (r: TaskRun) => {
  if (!r.started_at || !r.finished_at) return null
  const ms = Date.parse(r.finished_at) - Date.parse(r.started_at)
  return Number.isFinite(ms) && ms >= 0 ? Math.round(ms / 100) / 10 : null
}
function successRate(t: InspectionTask): number | null {
  const completed = t.runs.filter(r => ['SUCCESS', 'FAILED', 'TIMEOUT', 'PARTIAL', 'INTERRUPTED', 'CANCELLED'].includes(r.status))
  return completed.length ? Math.round(completed.filter(r => r.status === 'SUCCESS').length / completed.length * 100) : null
}

/** 各任务的附加统计（stats 字段，按任务类型解释） */
function statsText(code: string, r: TaskRun): string {
  const gauss = gaussStatsText(code, r.stats)
  if (gauss) return gauss
  const s = r.stats ?? {}
  if (['ecs_status', 'ecs_metrics'].includes(code) && (s.servers != null || s.metrics_source != null || s.metrics_diagnostic != null)) {
    const source = typeof s.metrics_source === 'string' ? s.metrics_source : ''
    const src = source === 'AOM' ? historicalMetricsSourceLabel(source)
      : Object.prototype.hasOwnProperty.call(METRICS_SOURCE, source) ? METRICS_SOURCE[source].label : '未知来源'
    const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : '—'
    const parts = [`指标来源 ${src}`, `${count(s.with_metrics)}/${count(s.servers ?? r.resource_total)} 台有采样`]
    if (code === 'ecs_metrics' || s.sample_points != null) parts.push(`采样点 ${count(s.sample_points)}`)
    // 诊断只解释已知代码；未知字段可能含内网地址、查询或其他敏感内容。
    const reasons = new Map(Object.entries({
      prometheus_not_configured: '未配置 Prometheus 数据源',
      source_not_configured: '未配置指标来源',
      aom_disabled: 'AOM 已停用',
      legacy_ces_disabled: 'CES 已关闭',
      metrics_source_disabled: '未启用外部指标来源',
      no_unambiguous_ip: '无可唯一匹配的 IP',
      no_matching_series: '未匹配序列',
      stale_samples: '只有过期样本',
      metric_query_missing: '缺少指标查询配置',
      invalid_prometheus_source: 'Prometheus 数据源配置无效',
      invalid_prometheus_mapping: 'Prometheus 指标映射无效',
      unverified_query_unit: '指标查询单位未确认',
      invalid_timestamp_query: '采样时间查询配置无效',
      invalid_query_window: '指标查询时间范围无效',
      scope_mismatch: '采集范围不匹配',
      legacy_metric_unsupported: '旧来源不支持该指标',
      query_failed: '指标查询失败',
      incomplete_fresh_coverage: '新鲜样本覆盖不完整',
      no_selected_resources: '无选中资源',
      no_source: '未配置指标来源',
      mapping_missing: '缺少指标映射',
      manageone_disabled: 'ManageOne 接入已关闭',
      manageone_not_configured: 'ManageOne 北向认证凭据未配置',
      no_manageone_mapping: 'ManageOne 资源未唯一映射',
      manageone_source_unverified: 'ManageOne 来源信息未核实',
      manageone_not_selected: '未选择 ManageOne 来源',
      manageone_metric_unsupported: 'ManageOne 不提供该指标',
      no_series: '无匹配时序',
      no_data: '无有效采样',
      partial: '指标采集不完整',
      error: '指标采集失败',
    }))
    const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
    const diagnostic: unknown = s.metrics_diagnostic
    if (record(diagnostic)) {
      const reasonText = (entry: Record<string, unknown>) => {
        if (typeof entry.reason === 'string' && entry.reason) return reasons.get(entry.reason) ?? '原因未知'
        if (entry.status === 'ready') return ''
        return typeof entry.status === 'string' ? reasons.get(entry.status) ?? '原因未知' : '原因未知'
      }
      const details: string[] = []
      const metrics = diagnostic.metrics
      if (record(metrics)) {
        for (const [key, label] of Object.entries({ cpu_util: 'CPU 使用率', mem_util: '内存使用率', disk_util: '磁盘使用率', boot_time: '开机时间' })) {
          if (!Object.prototype.hasOwnProperty.call(metrics, key) || !record(metrics[key])) continue
          const reason = reasonText(metrics[key])
          if (reason) details.push(`${label}：${reason}`)
        }
      }
      if (details.length) parts.push(details.join('；'))
      else if (diagnostic.status && diagnostic.status !== 'ready') parts.push(reasonText(diagnostic))
    }
    return parts.join(' · ')
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

const executions = useTaskExecutions({
  scopeKey: requestScope,
  tenantId: () => app.tenantId ?? null,
  enabled: () => workspaceView.value === 'executions' && user.can('task:view'),
  canRun: code => user.can('task:run') && (code !== 'data_cleanup' || globalConfigScope.value),
  makeRequestKey: newRequestId,
  confirmCancel: async execution => {
    try { await ElMessageBox.confirm(`仅停止“${execution.task_name}”的本地巡检；云端作业不会被终止。确认停止？`, '停止本地巡检', { type: 'warning' }); return true }
    catch { return false }
  },
  notice: (kind, message) => ElMessage[kind](message),
  changed: load,
  accepted: () => { workspaceView.value = 'executions' },
}, taskApi)
const executionTargetTask = computed(() => tasks.value.find(task => task.code === executions.targetTaskCode.value) ?? null)
const executionProjects = computed(() => {
  const options = new Map<number, string>()
  for (const target of executions.targets.data.value?.targets ?? []) if (target.project_ref !== null) options.set(target.project_ref, `${target.tenant_name ?? '平台'} · ${target.project_label}`)
  for (const item of executions.feed.data.value?.items ?? []) if (item.project_ref !== null) options.set(item.project_ref, `${item.tenant_name ?? '平台'} · ${item.project_label}`)
  return [...options].map(([ref, label]) => ({ ref, label }))
})
async function run(t: InspectionTask, projectRefs?: number[]) {
  if (!user.can('task:run') || !t.enabled) return
  return executions.run(t.code, projectRefs)
}

watch(requestScope, () => {
  ++loadVersion
  definitionError.value = ''
  tasks.value = []
  schedule.value = null
  selectedTaskCode.value = ''
  scopeTask.value = null
  edit.visible = false
  edit.saving = false
  toggling.value = ''
  sched.visible = false
  sched.saving = false
  ecsPolicy.visible = false
  jobSelection.visible = false
  void load()
}, { flush: 'sync' })
</script>

<style scoped lang="scss">
.task-panel { min-width: 0; max-width: 100%; }
.workspace-toolbar, .execution-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 16px; font-size: 12px; }
.execution-filters .el-select { width: 280px; max-width: 100%; }
.task-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 14px; }
.task-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px;
  .service-filter { width: 160px; }
  .enabled-filter { width: 150px; }
  .task-search { flex: 1 1 240px; min-width: 180px; }
  .filter-count { white-space: nowrap; }
  .el-button { margin-left: 0; }
}
.filter-hint { margin: 0 0 10px; font-size: 12px; }
.scroll-hint { display: none; margin-bottom: 8px; font-size: 12px; }
.task-table-scroll { min-width: 0; max-width: 100%; overflow-x: auto; }
.check-summary { margin-top: 5px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.task-description { margin-top: 5px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere;
  summary { color: var(--el-color-primary); cursor: pointer; }
}
.task-steps { margin: 6px 0; padding-left: 20px; }
.timing-note { margin-bottom: 8px; font-size: 12px; line-height: 1.6; }
.collection-detail { display: block; line-height: 1.6; overflow-wrap: anywhere; }
.interval-value { width: 140px; max-width: 100%; }
.interval-unit { width: 90px; margin-left: 8px; }
.cron-expression { width: 240px; max-width: 100%; }
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
@media (max-width: 600px) {
  .scroll-hint { display: block; }
  // 窄屏取消右侧吸附，避免操作列遮住任务名；列仍随表体内部横向滚动。
  .task-table :deep(.el-table-fixed-column--right) { position: relative !important; right: auto !important; }
  .task-table :deep(.el-table-fixed-column--right::before) { display: none; }
  .task-filters { .service-filter, .enabled-filter { flex: 1 1 140px; width: auto; } .task-search { flex-basis: 100%; } }
  .runs { padding: 4px 12px 12px; }
  .sched { overflow-wrap: anywhere; .recv, .sched-row.text-3 { padding-left: 0; } }
  .interval-value { width: 120px; }
  .interval-unit { width: 72px; margin-left: 4px; }
  .cron-in { width: 100%; margin: 6px 0 0; }
}
</style>
