import { effectiveMetricsSource } from '../../utils/dict'
import type { Resource } from '@/types'

/** 扩展属性中文名（详情抽屉“扩展属性”用；台账列表的类型专属列由后端 /resources/types 下发） */
export const ATTR_LABEL: Record<string, string> = {
  os: '操作系统', flavor: '规格编码', eip: '弹性公网 IP', vpc_id: '所属 VPC ID', charging: '计费模式', version: '版本',
  node_count: '节点数', cluster_type: '集群类型', volume_type: '磁盘类型', attached_to: '挂载云服务器', bootable: '系统盘',
  encrypted: '加密', device: '挂载点', cidr: '网段', subnet_count: '子网数', description: '描述', gateway: '网关', dhcp: 'DHCP',
  vpc_name: '所属 VPC', bandwidth: '带宽(Mbit/s)', share_type: '带宽类型', bandwidth_name: '带宽名称', bound_to: '绑定对象',
  listeners: '监听器数', members: '后端成员数', engine: '引擎', ha: '部署方式', type: '部署形态', engine_version: '引擎版本',
  port: '端口', capacity: '容量(GB)', max_clients: '最大连接数', spec_code: '规格编码', specification: '规格',
  partition_num: '分区数', storage_usage: '存储使用率(%)', components: '组件', az: '可用区', node_type: '节点规格',
  volume_gb: '存储(GB)', workspace: '工作空间', path: '作业路径', priority: '优先级', restarts_24h: '24h 重启次数',
  owner: '作业负责人', schedule: '调度周期', instance_id: '实例 ID', cluster: '所属集群', job_type: '作业类型',
  job_state: '作业状态', job_result: '作业结果', queue: '队列', user: '提交用户',
  metrics_source: '指标来源', task_state: '任务状态', power_state: '电源状态', cluster_id: '集群 ID', namespace: '命名空间',
  kind: '工作负载类型', workload: '工作负载', pods: 'Pod 数', ready_pods: '就绪 Pod', image: '镜像', status_raw: 'HCS 原始状态',
}
/** 监控指标中文名（详情抽屉“监控指标”用） */
export const METRIC_LABEL: Record<string, string> = {
  cpu_util: 'CPU 使用率(%)', mem_util: '内存使用率(%)', disk_util: '磁盘使用率(%)', disk_mount: '最满分区', reboots_24h: '24h 重启次数',
  cpu_usage: 'CPU 使用率(%)', mem_usage: '内存使用率(%)', disk_usage: '磁盘使用率(%)', memory_usage: '内存使用率(%)',
  used_memory_mb: '已用内存(MB)', connected_clients: '连接数', pods: 'Pod 数', ready_pods: '就绪 Pod', restarts_24h: '24h 重启次数',
  backpressure_min: '背压持续(分钟)', crashloop_pods: 'CrashLoop Pod', nodes: '节点数', bad_nodes: '异常节点', run_minutes: '本次耗时(分钟)',
  p90_minutes: 'P90 基线(分钟)', elapsed_min: '运行耗时(分钟)',
}
export const CHARGING: Record<string, string> = { '0': '按需', '1': '包周期' }
const METRICS_SRC: Record<string, string> = { CES: '云监控 CES', PROMETHEUS: 'Prometheus', NONE: '仅服务原生' }

export function fmtValue(key: string, v: unknown): string {
  if (v === null || v === undefined || v === '') return '-'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (key === 'charging') return CHARGING[String(v)] ?? String(v)
  if (key === 'metrics_source') return METRICS_SRC[effectiveMetricsSource(String(v))] ?? String(v)
  if (Array.isArray(v)) return v.join(', ')
  return String(v)
}
/** 取 "attrs.os" 这类路径的值 */
export const pick = (r: Resource, path: string): unknown =>
  path.split('.').reduce<any>((o, k) => (o == null ? undefined : o[k]), r)

/** 巡检对象的监控列（使用率进度条等）；台账通用列与类型专属列之外追加 */
export interface MonitorCol { label: string; width?: number; minWidth?: number; mono?: boolean; usage?: boolean; value: (r: Resource) => unknown; sub?: (r: Resource) => string }
export const MONITOR_COLUMNS: Record<string, MonitorCol[]> = {
  dgc_realtime_job: [
    { label: '节点（异常/总数）', width: 140, value: (r) => `${r.metrics.bad_nodes ?? 0} / ${r.metrics.nodes ?? 0}` },
    { label: '24h 重启', width: 90, value: (r) => r.attrs.restarts_24h ?? 0 },
    { label: '期望状态', width: 90, value: (r) => (r.expected_state.running === false ? '停止' : '运行') },
  ],
  dgc_batch_job: [
    { label: '本次 / P90 基线', width: 150, value: (r) => `${r.metrics.run_minutes ?? '-'} / ${r.metrics.p90_minutes ?? '-'} 分钟` },
    { label: '实例 ID', width: 100, mono: true, value: (r) => r.attrs.instance_id },
  ],
  mrs_job: [
    { label: '队列', width: 120, mono: true, value: (r) => r.attrs.queue },
    { label: '运行耗时', width: 96, value: (r) => (r.metrics.elapsed_min != null ? `${r.metrics.elapsed_min} 分钟` : '-') },
  ],
  gaussdb_instance: [
    { label: '磁盘使用率', width: 150, usage: true, value: (r) => r.metrics.disk_usage, sub: (r) => `存储 ${r.attrs.volume_gb ?? '-'} GB` },
    { label: 'CPU', width: 120, usage: true, value: (r) => r.metrics.cpu_usage },
    { label: '内存', width: 120, usage: true, value: (r) => r.metrics.mem_usage },
  ],
  ecs_server: [
    { label: 'CPU', width: 120, usage: true, value: (r) => r.metrics.cpu_util },
    { label: '内存', width: 120, usage: true, value: (r) => r.metrics.mem_util },
    { label: '磁盘（最满分区）', width: 150, usage: true, value: (r) => r.metrics.disk_util, sub: (r) => (r.metrics.disk_mount ? String(r.metrics.disk_mount) : '') },
    { label: '24h 重启', width: 84, value: (r) => r.metrics.reboots_24h ?? 0 },
    { label: '指标来源', width: 100, value: (r) => fmtValue('metrics_source', r.attrs.metrics_source) },
  ],
  cce_workload: [
    { label: 'Pod 就绪', width: 96, value: (r) => `${r.metrics.ready_pods ?? r.attrs.ready_pods ?? 0} / ${r.metrics.pods ?? r.attrs.pods ?? 0}` },
    { label: '24h 重启', width: 84, value: (r) => r.metrics.restarts_24h ?? 0 },
    { label: 'CrashLoop', width: 90, value: (r) => r.metrics.crashloop_pods ?? 0 },
    { label: '背压(分钟)', width: 96, value: (r) => r.metrics.backpressure_min ?? 0 },
    { label: '集群 / 命名空间', minWidth: 170, value: (r) => r.attrs.cluster ?? '-', sub: (r) => `${r.attrs.namespace ?? '-'} · ${r.attrs.kind ?? ''}` },
  ],
  dcs_instance: [
    { label: '内存使用率', width: 160, usage: true, value: (r) => r.metrics.memory_usage,
      sub: (r) => `${((r.metrics.used_memory_mb ?? 0) / 1024).toFixed(1)} / ${r.attrs.capacity ?? '-'} GB` },
    { label: 'CPU', width: 120, usage: true, value: (r) => r.metrics.cpu_usage },
    { label: '连接数', width: 96, value: (r) => r.metrics.connected_clients, sub: (r) => `上限 ${r.attrs.max_clients ?? '-'}` },
  ],
}

/** 字段变更的展示文本：old → new */
export const changeText = (c: { label: string; old: unknown; new: unknown }) =>
  `${c.label}：${fmtValue('', c.old)} → ${fmtValue('', c.new)}`
