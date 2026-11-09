import type { JobObjectLevel, JobPanel, JobReportRow, JobResourceType } from '../../types/job-reports'

export const SOURCE_LABELS: Record<JobResourceType, string> = {
  dgc_realtime_job: 'DGC 实时定义', dgc_batch_job: 'DGC 批定义', mrs_job: 'MRS 原生执行',
  dgc_mrs_node: 'DataArts MRS 节点',
}

type StatusMeta = { label: string; color: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' }
export const JOB_STATUSES: Record<string, StatusMeta> = {
  RUNNING: { label: '运行中', color: '#1d63f0', type: 'primary' },
  QUEUED: { label: '排队中', color: '#b88230', type: 'warning' },
  WAITING: { label: '等待中', color: '#a07726', type: 'warning' },
  PENDING: { label: '待处理', color: '#a07726', type: 'warning' },
  SUBMITTED: { label: '已提交', color: '#2488a6', type: 'primary' },
  ACCEPTED: { label: '已接受', color: '#2488a6', type: 'primary' },
  NEW_SAVING: { label: '新建保存中', color: '#697586', type: 'info' },
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
const NODE_STATUSES = ['WAITING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'PAUSED', 'CANCELED', 'UNKNOWN', 'NO_DATA']
const MRS_STATUSES = ['NEW', 'NEW_SAVING', 'SUBMITTED', 'ACCEPTED', 'RUNNING', 'FINISHED', 'SUCCEEDED', 'FAILED', 'KILLED', 'UNKNOWN', 'NO_DATA']

export const PANEL_CONFIG: Record<JobPanel, PanelConfig> = {
  dgc: {
    title: 'DGC 作业报表', desc: 'DataArts 父作业定义 · 实时与批，不含节点', totalLabel: 'DGC 定义数',
    focus: ['RUNNING', 'SUCCEEDED', 'EXCEPTION'], statuses: DGC_STATUSES,
  },
  flink: {
    title: 'Flink 作业报表', desc: '按引擎证据识别 · 默认 DGC 定义，可切换来源层级', totalLabel: 'Flink 定义数',
    focus: ['RUNNING', 'FAILED', 'EXCEPTION'], statuses: [...new Set([...DGC_STATUSES, ...MRS_STATUSES, ...NODE_STATUSES])],
  },
  realtime: {
    title: 'DGC 实时作业报表', desc: 'DataArts 实时父作业定义', totalLabel: 'DGC 实时定义数',
    focus: ['RUNNING', 'STOPPED', 'EXCEPTION'], statuses: DGC_STATUSES,
  },
  batch: {
    title: 'DGC 批作业报表', desc: 'DataArts 批父作业定义 · 无近期实例仍保留', totalLabel: 'DGC 批定义数',
    focus: ['SUCCEEDED', 'FAILED', 'RUNNING'], statuses: DGC_STATUSES,
  },
  mrs: {
    title: 'MRS 作业报表', desc: '默认 MRS 原生执行 · DataArts 节点需单独选择', totalLabel: 'MRS 原生执行数',
    focus: ['RUNNING', 'ACCEPTED', 'FAILED'], statuses: MRS_STATUSES,
  },
}

export const SOURCE_FACTS: Record<JobResourceType, { title: string; fields: string[] }> = {
  dgc_realtime_job: { title: '运行时间', fields: ['start_time', 'end_time'] },
  mrs_job: { title: 'MRS 原生执行', fields: ['cluster', 'cluster_id', 'queue', 'queue_name', 'job_name', 'job_id', 'submitted_time', 'start_time', 'end_time', 'elapsed_time', 'elapsed_time_unit', 'duration_basis', 'duration_observed_at'] },
  dgc_batch_job: { title: '定义与最近实例', fields: ['schedule_status', 'instance_query_status', 'window_start_ms', 'window_end_ms', 'listed_last_instance_status', 'listed_last_instance_end_time', 'instance_id', 'instance_type', 'plan_time', 'submitted_time', 'start_time', 'end_time', 'batch_baseline'] },
  dgc_mrs_node: { title: 'DataArts 节点执行', fields: ['workspace', 'job_name', 'node_name', 'node_type', 'instance_id', 'job_status', 'node_status', 'plan_time', 'start_time', 'end_time', 'retry_times', 'log_path'] },
}

export const DETAIL_LABELS: Record<string, string> = {
  cluster: '集群', cluster_id: '集群 ID', queue: '队列', queue_name: '队列名称', job_id: '执行 ID',
  duration_basis: '时长依据', duration_observed_at: '时长观测时间', job_state: '执行状态', job_result: '执行结果', elapsed_time: '原始耗时', elapsed_time_unit: '耗时单位',
  schedule_status: '调度状态', instance_query_status: '近24h实例查询', window_start_ms: '查询窗口开始', window_end_ms: '查询窗口结束',
  listed_last_instance_status: '列表末次实例提示（非当前证据）', listed_last_instance_end_time: '列表末次实例结束提示',
  definition: '定义引擎证据', definition_present: '定义已采集', data_object: '对象层级', engine_source: '引擎依据', engines: '引擎', single_node_type: '单节点类型', node_types: '定义节点类型',
  batch_baseline: '近24小时基线元信息', workspace: '工作空间', nodes: '节点状态', instance_id: '实例 ID', instance_type: '调度方式（原始值）',
  job_type: '作业类型', job_name: '所属作业', node_name: '节点名称', node_type: '节点类型', node_status: '节点状态',
  retry_times: '失败重试次数', log_path: '日志路径', job_status: '作业状态',
  instance_status: '实例执行状态', start_time: '实际开始时间', end_time: '实际结束时间',
  plan_time: '计划时间', submitted_time: '提交时间', engine: '引擎',
}

export const METRIC_LABELS: Record<string, string> = {
  p90_minutes: '近24小时成功实例 P90（分钟）', run_minutes: '运行时长（分钟）', retry_times: '失败重试次数', nodes: '节点数', bad_nodes: '异常节点数',
}


export const OBJECT_LEVEL_LABELS: Record<JobObjectLevel, string> = {
  DEFINITION: 'DGC 定义', EXECUTION: 'MRS 原生执行', NODE: 'DataArts MRS 节点', INSTANCE: 'DataArts 独立实例',
}
export const COLLECTION_SOURCE_LABELS: Record<string, string> = {
  dataarts_definition: 'DataArts 定义', mrs_job_execution: 'MRS 原生',
  dataarts_instance_detail: 'DataArts 实例节点', dataarts_instance: 'DataArts 独立实例',
}
export function objectLevelOptions(panel: JobPanel): JobObjectLevel[] {
  if (panel === 'mrs') return ['EXECUTION', 'NODE']
  if (panel === 'flink') return ['DEFINITION', 'EXECUTION', 'NODE', 'INSTANCE']
  return panel === 'realtime' ? ['DEFINITION'] : ['DEFINITION', 'INSTANCE']
}

export function jobTypeLabel(row: Pick<JobReportRow, 'object_level' | 'resource_type'>): string {
  return row.object_level === 'INSTANCE' ? OBJECT_LEVEL_LABELS.INSTANCE : SOURCE_LABELS[row.resource_type]
}
