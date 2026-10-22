// 业务字典：前端展示口径（与后端 services/catalog.py 对齐）
export const SEVERITY: Record<string, { label: string; color: string; bg: string }> = {
  P1: { label: '紧急', color: '#D93026', bg: '#FDECEA' },
  P2: { label: '重要', color: '#F57C00', bg: '#FFF3E0' },
  P3: { label: '次要', color: '#F9A825', bg: '#FFF8E1' },
  P4: { label: '提示', color: '#1976D2', bg: '#E3F2FD' },
}
export const SEVERITIES = ['P1', 'P2', 'P3', 'P4'] as const

type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'
export const ALARM_STATUS: Record<string, { label: string; type: TagType }> = {
  FIRING: { label: '告警中', type: 'danger' },
  ACKED: { label: '已认领', type: 'warning' },
  SUPPRESSED: { label: '已抑制', type: 'info' },
  RESOLVED: { label: '已恢复', type: 'success' },
  CLOSED: { label: '已关闭', type: 'info' },
}
export const HEALTH: Record<string, { label: string; color: string }> = {
  OK: { label: '健康', color: '#2E7D32' },
  INFO: { label: '提示', color: '#1976D2' },
  WARNING: { label: '次要', color: '#F9A825' },
  MAJOR: { label: '重要', color: '#F57C00' },
  CRITICAL: { label: '紧急', color: '#D93026' },
  UNKNOWN: { label: '未知', color: '#8C8C8C' },
}
export const SERVICES: Record<string, string> = {
  dataarts: 'DataArts Studio', mrs: 'MRS', gaussdb: 'GaussDB', dcs: 'DCS', ecs: 'ECS', evs: 'EVS', vpc: 'VPC', elb: 'ELB',
  rds: 'RDS', dms: 'DMS Kafka', cce: 'CCE', dws: 'DWS', css: 'CSS', ledger: '全部服务',
}
/** 资源类型名称（与后端 services/catalog.py RESOURCE_TYPES 一致；台账页以 /resources/types 接口为准） */
export const RESOURCE_TYPES: Record<string, string> = {
  ecs_server: '云服务器 ECS', cce_cluster: 'CCE 集群', evs_volume: '云硬盘 EVS', vpc: '虚拟私有云 VPC', vpc_subnet: '子网',
  eip: '弹性公网 IP', elb: '负载均衡 ELB', rds_instance: '云数据库 RDS', gaussdb_instance: 'GaussDB 实例', dcs_instance: 'DCS 缓存实例',
  kafka_instance: 'Kafka 实例', mrs_cluster: 'MRS 集群', dws_cluster: 'DWS 数据仓库', css_cluster: 'CSS 云搜索',
  dgc_realtime_job: '实时作业', dgc_batch_job: '批作业', mrs_job: 'MRS 作业', cce_workload: '容器工作负载',
}
export const typeName = (code?: string | null) => (code ? RESOURCE_TYPES[code] ?? code : '-')

// ---- 资源台账 ----
export const CHANGE_TYPE: Record<string, { label: string; type: TagType; color: string }> = {
  CREATED: { label: '新增', type: 'success', color: '#2E7D32' },
  UPDATED: { label: '变更', type: 'primary', color: '#1d63f0' },
  DELETED: { label: '释放', type: 'danger', color: '#D93026' },
  STATUS: { label: '状态变更', type: 'warning', color: '#F57C00' },
  REBOOT: { label: '重启', type: 'info', color: '#7B61FF' },
}
/** 台账变更（新增 / 变更 / 释放）与巡检发现的状态类变更（状态变更 / 重启）分开展示 */
export const LEDGER_CHANGE_TYPES = ['CREATED', 'UPDATED', 'DELETED'] as const
export const STATUS_CHANGE_TYPES = ['STATUS', 'REBOOT'] as const
export const CHANGE_SOURCE: Record<string, string> = { SYNC: '同步发现', MANUAL: '人工维护', INSPECT: '巡检发现' }
export const ENV_TYPE: Record<string, { label: string; type: TagType }> = {
  PROD: { label: '生产', type: 'danger' }, TEST: { label: '测试', type: 'warning' }, DEV: { label: '开发', type: 'info' },
}
export const CATEGORY_ICON: Record<string, string> = {
  compute: 'Cpu', storage: 'Files', network: 'Connection', database: 'Coin', bigdata: 'DataAnalysis', job: 'Operation',
}

// ---- 报表 ----
export const REPORT_KIND: Record<string, { label: string; desc: string; color: string }> = {
  DAILY: { label: '日报', desc: '昨日 00:00 ~ 今日 00:00', color: '#1d63f0' },
  WEEKLY: { label: '周报', desc: '上周一 ~ 本周一', color: '#7b61ff' },
  MONTHLY: { label: '月报', desc: '上月 1 日 ~ 本月 1 日', color: '#0f9d8f' },
  LEDGER: { label: '台账报表', desc: '当前资源快照 + 近 30 天变更', color: '#f57c00' },
}
export const TASK_STATUS: Record<string, { label: string; type: TagType }> = {
  SUCCESS: { label: '成功', type: 'success' }, PARTIAL: { label: '部分成功', type: 'warning' },
  FAILED: { label: '失败', type: 'danger' }, TIMEOUT: { label: '超时', type: 'danger' }, RUNNING: { label: '运行中', type: 'primary' },
}
export const NOTIFY_STATUS: Record<string, { label: string; type: TagType }> = {
  SENT: { label: '已发送', type: 'success' }, PENDING: { label: '待发送', type: 'primary' },
  RETRYING: { label: '重试中', type: 'warning' }, FAILED: { label: '失败', type: 'danger' }, MERGED: { label: '已合并', type: 'info' },
}
export const NOTIFY_KIND: Record<string, string> = {
  FIRING: '告警', UPGRADE: '级别升级', REPEAT: '重复', ESCALATE: '未处理升级', RESOLVED: '恢复', DIGEST: '汇总', REPORT: '报表',
}
export const LOG_ACTION: Record<string, { label: string; color: string }> = {
  FIRE: { label: '触发告警', color: '#D93026' }, UPGRADE: { label: '级别升级', color: '#F57C00' },
  DOWNGRADE: { label: '级别降低', color: '#1976D2' }, NOTIFY: { label: '发送通知', color: '#1d63f0' },
  ACK: { label: '认领', color: '#F9A825' }, SILENCE: { label: '静默 / 抑制', color: '#8C8C8C' },
  RESOLVE: { label: '自动恢复', color: '#2E7D32' }, REOPEN: { label: '重新打开', color: '#D93026' },
  CLOSE: { label: '关闭', color: '#8C8C8C' }, COMMENT: { label: '备注', color: '#4e5969' },
  ESCALATE: { label: '未处理升级', color: '#B3261E' },
}

// 资源原始状态 → 标签颜色（各服务状态枚举见架构文档“状态映射”章节）
const OK_STATES = ['NORMAL', 'RUNNING', 'ACTIVE', 'running', 'success', 'SUCCEEDED', 'in-use', 'ONLINE', 'AVAILABLE', 'Available', 'OK', '200']
const BAD_STATES = ['EXCEPTION', 'ERROR', 'FAILED', 'fail', 'STORAGE FULL', 'abnormal', 'failed', 'FROZEN', 'running-exception', 'Unavailable', '303']
const WAIT_STATES = ['STARTING', 'STOPPING', 'waiting', 'ACCEPTED', 'RESTARTING', 'EXTENDING', 'BUILD', 'creating', 'Creating',
  'REBOOT', 'HARD_REBOOT', 'RESIZE', 'VERIFY_RESIZE', 'REVERT_RESIZE', 'MIGRATING', 'REBUILD']
/** 台账里的“闲置”状态：未挂载云硬盘、未绑定 EIP、已关机云服务器 */
export const IDLE_STATES = ['available', 'DOWN', 'SHUTOFF']
export function rawStatusType(s?: string | null): TagType {
  if (!s) return 'info'
  if (OK_STATES.includes(s)) return 'success'
  if (BAD_STATES.includes(s)) return 'danger'
  if (WAIT_STATES.includes(s) || IDLE_STATES.includes(s)) return 'warning'
  return 'info'
}

export const scoreColor = (v: number) => (v >= 90 ? '#2E7D32' : v >= 75 ? '#1d63f0' : v >= 60 ? '#F57C00' : '#D93026')
export const usageColor = (v: number) => (v >= 90 ? '#D93026' : v >= 80 ? '#F57C00' : v >= 70 ? '#F9A825' : '#1d63f0')

// 规则试运行的示例事实（按资源类型）
export const SAMPLE_FACTS: Record<string, Record<string, unknown>> = {
  dcs_instance: { status: 'RUNNING', memory_usage: 92.3, cpu_usage: 46.2, connected_clients: 1840, max_clients: 10000, status_duration_min: 5, critical: true, expected_running: true },
  gaussdb_instance: { status: 'ACTIVE', disk_usage: 87.4, cpu_usage: 62.1, mem_usage: 71.3, status_duration_min: 5, critical: true },
  dgc_realtime_job: { status: 'EXCEPTION', job_status: 'NORMAL', node_status: 'EXCEPTION', status_duration_min: 12, critical: true, expected_running: true },
  dgc_batch_job: { last_status: 'fail', run_minutes: 42, p90_minutes: 30, critical: false },
  mrs_cluster: { cluster_state: 'abnormal', status_duration_min: 3 },
  mrs_job: { job_state: 'FAILED', job_result: 'FAILED', status_duration_min: 20, critical: false },
}

// 台账状态中文名（按资源类型覆盖；未列出的原样展示）
const STATUS_TEXT: Record<string, Record<string, string>> = {
  _: { ACTIVE: '运行中', RUNNING: '运行中', running: '运行中', AVAILABLE: '可用', Available: '可用', ONLINE: '正常', OK: '正常', ERROR: '故障' },
  ecs_server: {
    SHUTOFF: '已关机', BUILD: '创建中', REBOOT: '重启中', HARD_REBOOT: '强制重启中', RESIZE: '变更规格中', VERIFY_RESIZE: '规格待确认',
    REVERT_RESIZE: '规格回退中', MIGRATING: '迁移中', REBUILD: '重装中', DELETED: '已删除', SHELVED: '已搁置', SHELVED_OFFLOADED: '已搁置', UNKNOWN: '未知',
  },
  evs_volume: { 'in-use': '使用中', available: '未挂载', error: '故障' },
  eip: { ACTIVE: '已绑定', DOWN: '未绑定' },
  vpc_subnet: { ACTIVE: '正常' },
  gaussdb_instance: { 'STORAGE FULL': '存储满' },
}
const JOB_TYPES = ['dgc_realtime_job', 'dgc_batch_job', 'mrs_job']
export function statusText(type: string, raw?: string | null): string {
  if (!raw) return '-'
  if (JOB_TYPES.includes(type)) return raw
  return STATUS_TEXT[type]?.[raw] ?? STATUS_TEXT._[raw] ?? raw
}

// ---- 多租户 / 接入 ----
export const AUTH_TYPE: Record<string, { label: string; short: string; desc: string; color: string }> = {
  PASSWORD: { label: 'IAM 用户名 + 密码', short: '用户名密码', desc: '调用 IAM /v3/auth/tokens 换取 Token（有效期 24 小时，提前 1 小时刷新）', color: '#1d63f0' },
  AKSK: { label: 'AK/SK 签名', short: 'AK/SK', desc: '每个请求用 SK 做 SDK-HMAC-SHA256 签名，无需 Token；适合不允许创建 IAM 密码用户的租户', color: '#0e9f8f' },
}
export const METRICS_SOURCE: Record<string, { label: string; short: string; color: string; desc: string }> = {
  AUTO: { label: '自动探测', short: '自动', color: '#4e5969', desc: '先探测云监控 CES，不可用时降级为 Prometheus，都没有则只巡检状态' },
  CES: { label: '云监控 CES', short: 'CES', color: '#1d63f0', desc: 'HCS 8.5.1 云监控（可选组件），按 namespace / metric_name 批量查询' },
  PROMETHEUS: { label: 'Prometheus', short: 'Prometheus', color: '#e6522c', desc: '租户自建 Prometheus（node_exporter 等），PromQL 查询' },
  NONE: { label: '不采集指标', short: '仅状态', color: '#8c8c8c', desc: '只巡检资源状态（运行 / 关机 / 故障），不判断 CPU / 内存 / 磁盘' },
}
export const CAPABILITY: Record<string, string> = { ces: '云监控 CES', prometheus: 'Prometheus', cce_api: 'CCE K8s API' }
export const CHECK_KIND: Record<string, string> = { auth: '认证', service: '服务接口', capability: '能力探测' }
/** ECS 状态分组颜色（总览 / 报表）：运行中 / 关机 / 故障 / 其他 */
export const ECS_STATUS_COLOR: Record<string, string> = { ACTIVE: '#2E7D32', SHUTOFF: '#8C8C8C', ERROR: '#D93026', OTHER: '#F9A825' }

// ---- 容器事件 ----
export const POD_LEVEL: Record<string, { label: string; color: string; type: TagType }> = {
  CRITICAL: { label: '严重', color: '#D93026', type: 'danger' },
  WARNING: { label: '警告', color: '#F57C00', type: 'warning' },
  INFO: { label: '提示', color: '#1976D2', type: 'primary' },
}
export const POD_SOURCE: Record<string, string> = { K8S_EVENT: 'K8s 事件', POD_STATUS: 'Pod 状态', PROBE: '背压探针' }
export const POD_PHASE: Record<string, { label: string; type: TagType }> = {
  Running: { label: '运行中', type: 'success' }, Pending: { label: '等待调度', type: 'warning' }, Succeeded: { label: '已完成', type: 'info' },
  Failed: { label: '失败', type: 'danger' }, Unknown: { label: '未知', type: 'info' },
}

// ---- 审计 ----
export const AUDIT_RESULT: Record<string, { label: string; type: TagType }> = {
  SUCCESS: { label: '成功', type: 'success' }, FAILED: { label: '失败', type: 'danger' },
}
