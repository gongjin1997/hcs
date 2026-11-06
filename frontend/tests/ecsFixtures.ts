import type { EcsHistoryOut, EcsHistoryQuery, EcsInstance, EcsPolicyOut, EcsProjectQuota } from '../src/types/ecs'
import { blankEcsPolicy, ECS_PHASES } from '../src/views/ecs/ecs-policy'

export const ecsInstance = (id = 11, tenantId = 7): EcsInstance => ({ id, tenant_id: tenantId, tenant_name: `租户 ${tenantId}`,
  project_ref: 20, project_name: '项目甲', resource_id: `vm-${id}`, name: `服务器 ${id}`, ip: '192.0.2.1', status_raw: 'ACTIVE', health: 'OK',
  critical: true, monitored: true, included: true, metrics_enabled: true, inspection_enabled: true, metrics_source: 'PROMETHEUS',
  metrics_interval_sec: 300, inspection_interval_sec: 120, stale_after_sec: 900,
  latest: { cpu_util: { value: null, observed_at: null, collected_at: null, source: null, state: 'NO_DATA' } },
  diagnostic: { status: 'NO_DATA', message: '尚无实际采样', at: null },
})
export function ecsHistory(query: EcsHistoryQuery, id = 11, tenantId = 7): EcsHistoryOut {
  const start = Date.parse(query.start_at)
  const at = (index: number) => new Date(start + index * query.interval_sec * 1000).toISOString()
  return { instance: ecsInstance(id, tenantId), ...query, retention_days: 90, generated_at: query.end_at, timezone: 'UTC', warnings: [],
    series: [{ metric: 'cpu_util', label: 'CPU 使用率', source: 'AOM', unit: '%', points: [
      { at: at(0), average: 0, maximum: 0, minimum: 0, sample_count: 1 },
      { at: at(1), average: null, maximum: null, minimum: null, sample_count: 0 },
      { at: at(2), average: 55, maximum: 70, minimum: 40, sample_count: 2 },
    ] }, { metric: 'cpu_util', label: 'CPU 使用率', source: 'PROMETHEUS', unit: '%', points: [
      { at: at(0), average: null, maximum: null, minimum: null, sample_count: 0 },
      { at: at(1), average: null, maximum: null, minimum: null, sample_count: 0 },
      { at: at(2), average: 60, maximum: 80, minimum: 50, sample_count: 1 },
    ] }] }
}
export function ecsPolicy(tenantId = 7, master = true): EcsPolicyOut {
  const policy = blankEcsPolicy()
  const phase_status = Object.fromEntries(ECS_PHASES.map(({ key }) => [key, { configured_enabled: policy[key].enabled,
    master_enabled: master, task_code: `ecs_${key}`, task_enabled: true, supported: true, effective_enabled: master && policy[key].enabled }])) as EcsPolicyOut['phase_status']
  return { tenant_id: tenantId, resource_type: 'ecs_server', master_enabled: master, policy, phase_status }
}
export function ecsQuota(tenantId = 7): EcsProjectQuota {
  return { tenant_id: tenantId, tenant_name: '租户甲', project_ref: tenantId + 100, project_id: `project-${tenantId}`, project_name: '项目甲', region: 'region-local', enabled: true,
    source: 'ecs.native_limits', status: 'partial', fetched_at: '2026-11-01T00:00:00Z', source_observed_at: null, last_attempt_at: '2026-11-01T00:00:00Z', last_error: null, is_stale: false,
    quotas: [{ key: 'instances', label: '云服务器', unit: 'count', limit: 10, used: 0, remaining: 10, limit_status: 'known', usage_status: 'known', limit_field: 'maxTotalInstances', usage_field: 'totalInstancesUsed', note: null },
      { key: 'ram', label: '内存', unit: 'MB', limit: null, used: null, remaining: null, limit_status: 'unlimited', usage_status: 'not_reported', limit_field: 'maxTotalRAMSize', usage_field: null, note: null }] }
}
