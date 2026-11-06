import type { JobPanel, JobResourceType } from '../../types/job-reports'

export const SOURCE_LABELS: Record<JobResourceType, string> = {
  dgc_realtime_job: 'DGC 实时作业', dgc_batch_job: 'DGC 批作业', mrs_job: 'MRS 作业',
}

type StatusMeta = { label: string; color: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' }
export const JOB_STATUSES: Record<string, StatusMeta> = {
  RUNNING: { label: '运行中', color: '#1d63f0', type: 'primary' },
  QUEUED: { label: '排队中', color: '#b88230', type: 'warning' },
  WAITING: { label: '等待中', color: '#a07726', type: 'warning' },
  PENDING: { label: '待处理', color: '#a07726', type: 'warning' },
  SUBMITTED: { label: '已提交', color: '#2488a6', type: 'primary' },
  NEW: { label: '新建', color: '#697586', type: 'info' },
  STARTING: { label: '启动中', color: '#2488a6', type: 'primary' },
  STOPPING: { label: '停止中', color: '#a96f29', type: 'warning' },
  STOPPED: { label: '已停止', color: '#697586', type: 'info' },
  PAUSED: { label: '暂停', color: '#a07726', type: 'warning' },
  SKIPPED: { label: '已跳过', color: '#697586', type: 'info' },
  CANCELED: { label: '已取消', color: '#697586', type: 'info' },
  KILLED: { label: '已终止', color: '#697586', type: 'info' },
  KILLING: { label: '终止中', color: '#a96f29', type: 'warning' },
  SUCCEEDED: { label: '成功', color: '#2e7d32', type: 'success' },
  FAILED: { label: '失败', color: '#d93026', type: 'danger' },
  EXCEPTION: { label: '运行异常', color: '#c05b15', type: 'danger' },
  FINISHED: { label: '已结束（结果未知）', color: '#8b6c50', type: 'warning' },
  UNKNOWN: { label: '未知', color: '#86909c', type: 'info' },
  NO_DATA: { label: '无状态', color: '#aeb5bf', type: 'info' },
}

export const statusMeta = (status: string): StatusMeta => JOB_STATUSES[status] ?? {
  label: `未识别：${status.replace(/^UNRECOGNIZED:/, '')}`, color: '#86909c', type: 'info',
}

interface PanelConfig {
  title: string
  desc: string
  totalLabel: string
  focus: string[]
  statuses: string[]
}

const DGC_STATUSES = [
  'RUNNING', 'STARTING', 'WAITING', 'QUEUED', 'PENDING', 'SUBMITTED', 'STOPPING', 'STOPPED', 'PAUSED',
  'SUCCEEDED', 'FAILED', 'EXCEPTION', 'CANCELED', 'FINISHED', 'UNKNOWN', 'NO_DATA',
]
// DataArts 节点状态（数据治理中心 API 表 5-182）：waiting / running / success / fail / skip / pause / manual-stop
const MRS_STATUSES = ['WAITING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'PAUSED', 'CANCELED', 'UNKNOWN', 'NO_DATA']

export const PANEL_CONFIG: Record<JobPanel, PanelConfig> = {
  dgc: {
    title: 'DGC 作业报表', desc: 'DGC 实时与批作业', totalLabel: 'DGC 作业总数',
    focus: ['RUNNING', 'SUCCEEDED', 'EXCEPTION'], statuses: DGC_STATUSES,
  },
  flink: {
    title: 'Flink 作业报表', desc: 'Flink 作业状态与运行详情', totalLabel: '已识别 Flink 作业',
    focus: ['RUNNING', 'FAILED', 'EXCEPTION'], statuses: [...new Set([...DGC_STATUSES, ...MRS_STATUSES])],
  },
  realtime: {
    title: '实时作业报表', desc: 'DGC 实时作业 · 运行、启动、停止与节点状态', totalLabel: '实时作业总数',
    focus: ['RUNNING', 'STOPPED', 'EXCEPTION'], statuses: DGC_STATUSES,
  },
  batch: {
    title: '批作业报表', desc: 'DGC 批作业实例与执行结果', totalLabel: '批作业总数',
    focus: ['SUCCEEDED', 'FAILED', 'RUNNING'], statuses: DGC_STATUSES,
  },
  mrs: {
    title: 'MRS 作业报表', desc: 'DataArts 作业中的 MRS 节点 · 状态与运行时长', totalLabel: 'MRS 作业节点总数',
    focus: ['RUNNING', 'WAITING', 'FAILED'], statuses: MRS_STATUSES,
  },
}

export const SOURCE_FACTS: Record<JobResourceType, { title: string; fields: string[] }> = {
  dgc_realtime_job: { title: '运行时间', fields: ['start_time', 'end_time'] },
  dgc_batch_job: { title: '最新批作业实例', fields: ['instance_id', 'instance_type', 'plan_time', 'submitted_time', 'start_time', 'end_time', 'batch_baseline'] },
  mrs_job: { title: '最新 MRS 节点执行', fields: ['workspace', 'job_name', 'node_name', 'node_type', 'instance_id', 'job_status', 'node_status', 'plan_time', 'start_time', 'end_time', 'retry_times', 'log_path'] },
}

export const DETAIL_LABELS: Record<string, string> = {
  batch_baseline: '近24小时基线元信息', workspace: '工作空间', nodes: '节点状态', instance_id: '实例 ID', instance_type: '调度方式（原始值）',
  job_type: '作业类型', job_name: '所属作业', node_name: '节点名称', node_type: '节点类型', node_status: '节点状态',
  retry_times: '失败重试次数', log_path: '日志路径', job_status: '作业状态',
  instance_status: '实例执行状态', start_time: '实际开始时间', end_time: '实际结束时间',
  plan_time: '计划时间', submitted_time: '提交时间', engine: '引擎',
}

export const METRIC_LABELS: Record<string, string> = {
  p90_minutes: '近24小时成功实例 P90（分钟）', run_minutes: '运行时长（分钟）', retry_times: '失败重试次数', nodes: '节点数', bad_nodes: '异常节点数',
}
