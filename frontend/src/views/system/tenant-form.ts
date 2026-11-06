import type { Tenant, TenantIn, TenantProject } from '@/types'
import { METRICS_SOURCE } from '../../utils/dict'

/** 租户设置选择外部通道，不决定 DCS、GaussDB 的原生指标来源。 */
export const TENANT_METRICS_SOURCES = {
  AUTO: METRICS_SOURCE.AUTO,
  CES: METRICS_SOURCE.CES,
  PROMETHEUS: METRICS_SOURCE.PROMETHEUS,
  NONE: { ...METRICS_SOURCE.NONE, desc: '保留服务接口原生字段；DCS、GaussDB 原生采集不受此选项影响' },
}

/** 新建租户的默认值：IAM 地址按 HCS 8.5.1 常见形态 iam-apigateway-proxy.{外部域名} 预填 */
export const blankProject = (domain = ''): TenantProject => ({
  region_id: '', project_id: '', project_name: '', external_domain: domain, env: 'PROD', workspaces: [], enabled: true,
})
export const blankTenant = (): TenantIn => ({
  code: '', name: '', short_name: '', color: '#1d63f0', iam_endpoint: 'https://iam-apigateway-proxy.', domain_name: '', domain_id: null,
  auth_type: 'PASSWORD', username: '', access_key: '', secret: '', endpoints: {}, enabled: true, report_user_ids: [], remark: '',
  metrics_source: 'AUTO', prometheus_url: '', projects: [blankProject()],
})

/** 已有租户 → 表单（密码 / SK、AK 不回显：留空表示不修改） */
export function toTenantIn(t: Tenant): TenantIn {
  return {
    code: t.code, name: t.name, short_name: t.short_name, color: t.color, iam_endpoint: t.iam_endpoint, domain_name: t.domain_name,
    domain_id: t.domain_id, auth_type: t.auth_type, username: t.username, access_key: null, secret: null,
    endpoints: { ...t.endpoints }, enabled: t.enabled, report_user_ids: [...(t.report_user_ids ?? [])], remark: t.remark,
    metrics_source: t.metrics_source === 'AOM' ? 'NONE' : t.metrics_source, prometheus_url: t.prometheus_url,
    projects: t.projects.map((p) => ({ ...p, workspaces: [...p.workspaces] })),
  }
}

/** 提交前清理：空字符串 → null，按认证方式去掉无关字段 */
export function cleanTenantIn(f: TenantIn): TenantIn {
  const blank = (v: string | null | undefined) => (v && v.trim() ? v.trim() : null)
  return {
    ...f,
    metrics_source: f.metrics_source === 'AOM' ? 'NONE' : f.metrics_source,
    short_name: f.short_name.trim() || f.name.trim(),
    domain_name: f.domain_name.trim(),
    domain_id: blank(f.domain_id),
    username: f.auth_type === 'PASSWORD' ? blank(f.username) : null,
    access_key: f.auth_type === 'AKSK' ? blank(f.access_key) : null,
    secret: blank(f.secret),
    remark: blank(f.remark),
    prometheus_url: blank(f.prometheus_url),
    endpoints: Object.fromEntries(Object.entries(f.endpoints).filter(([k, v]) => k && v.trim()).map(([k, v]) => [k, v.trim()])),
    projects: f.projects.map((p) => ({ ...p, project_name: blank(p.project_name) })),
  }
}

/** 可覆盖地址的服务（与后端 hcs/endpoints.py 的服务编码一致） */
export const ENDPOINT_SERVICES = ['ecs', 'evs', 'vpc', 'elb', 'cce', 'cce_k8s', 'gaussdb', 'dcs', 'mrs', 'dws', 'dataarts', 'ces']
/** 默认模板（与后端 core/config.py hcs_endpoints 一致），用作占位提示 */
export const DEFAULT_ENDPOINTS: Record<string, string> = {
  dataarts: 'dayu-dlf.{region}.{domain}', mrs: 'mrs.{region}.{domain}', gaussdb: 'gaussdb-opengauss.{region}.{domain}',
  dcs: 'dcs.{region}.{domain}', elb: 'vpc.{region}.{domain}', cce_k8s: '{cluster_id}.cce.{region}.{domain}',
}
export const endpointHint = (svc: string) => svc === 'aom' ? 'AOM 已停用，旧地址不再使用'
  : svc === 'ces' ? '已有 CES Endpoint（旧配置）' : DEFAULT_ENDPOINTS[svc] ?? `${svc}.{region}.{domain}`
export const TENANT_COLORS = ['#1d63f0', '#7a5af8', '#0e9f8f', '#f08a24', '#e0457b', '#2e7d32', '#00838f', '#6d4c41']
