import type { Tenant, TenantIn, TenantProject } from '@/types'

/** 新建租户的默认值：IAM 地址按 HCS 8.5.1 常见形态 iam-apigateway-proxy.{外部域名} 预填 */
export const blankProject = (domain = ''): TenantProject => ({
  region_id: '', project_id: '', project_name: '', external_domain: domain, env: 'PROD', workspaces: [], enabled: true,
})
export const blankTenant = (): TenantIn => ({
  code: '', name: '', short_name: '', color: '#1d63f0', iam_endpoint: 'https://iam-apigateway-proxy.', domain_name: '', domain_id: null,
  auth_type: 'PASSWORD', username: '', access_key: '', secret: '', endpoints: {}, enabled: true, report_mail_to: [], remark: '',
  metrics_source: 'AUTO', prometheus_url: '', projects: [blankProject()],
})

/** 已有租户 → 表单（密码 / SK、AK 不回显：留空表示不修改） */
export function toTenantIn(t: Tenant): TenantIn {
  return {
    code: t.code, name: t.name, short_name: t.short_name, color: t.color, iam_endpoint: t.iam_endpoint, domain_name: t.domain_name,
    domain_id: t.domain_id, auth_type: t.auth_type, username: t.username, access_key: null, secret: null,
    endpoints: { ...t.endpoints }, enabled: t.enabled, report_mail_to: [...t.report_mail_to], remark: t.remark,
    metrics_source: t.metrics_source, prometheus_url: t.prometheus_url,
    projects: t.projects.map((p) => ({ ...p, workspaces: [...p.workspaces] })),
  }
}

/** 提交前清理：空字符串 → null，按认证方式去掉无关字段 */
export function cleanTenantIn(f: TenantIn): TenantIn {
  const blank = (v: string | null | undefined) => (v && v.trim() ? v.trim() : null)
  return {
    ...f,
    short_name: f.short_name.trim() || f.name.trim(),
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
export const ENDPOINT_SERVICES = ['ecs', 'evs', 'vpc', 'elb', 'cce', 'cce_k8s', 'rds', 'gaussdb', 'dcs', 'dms', 'mrs', 'dws', 'css', 'dataarts', 'ces']
/** 默认模板（与后端 core/config.py hcs_endpoints 一致），用作占位提示 */
export const DEFAULT_ENDPOINTS: Record<string, string> = {
  dataarts: 'dayu-dlf.{region}.{domain}', mrs: 'mrs.{region}.{domain}', gaussdb: 'gaussdb-opengauss.{region}.{domain}',
  dcs: 'dcs.{region}.{domain}', ces: 'ces.{region}.{domain}', cce_k8s: '{cluster_id}.cce.{region}.{domain}',
}
export const endpointHint = (svc: string) => DEFAULT_ENDPOINTS[svc] ?? `${svc}.{region}.{domain}`
export const TENANT_COLORS = ['#1d63f0', '#7a5af8', '#0e9f8f', '#f08a24', '#e0457b', '#2e7d32', '#00838f', '#6d4c41']
