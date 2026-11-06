import type { PodEventOptions } from '../../types'

type Cluster = PodEventOptions['clusters'][number]
export function groupPodClusters(clusters: Cluster[]): (Cluster & { scopeCount: number })[] {
  const groups = new Map<string, Cluster[]>()
  for (const cluster of clusters) {
    const rows = groups.get(cluster.cluster_id) ?? []
    if (!rows.some(row => row.tenant_id === cluster.tenant_id && row.project_ref === cluster.project_ref)) rows.push(cluster)
    groups.set(cluster.cluster_id, rows)
  }
  return [...groups.values()].map(rows => {
    const names = [...new Set(rows.map(row => row.cluster_name))].join(' / ')
    const tenants = [...new Set(rows.map(row => row.tenant_name).filter(Boolean))]
    const suffix = rows.length < 2 ? '' : tenants.length > 1 ? `（${tenants.join('、')}）` : `（${rows.length} 个项目）`
    return { ...rows[0], cluster_name: names + suffix, scopeCount: rows.length }
  })
}

export function podNamespaces(options: PodEventOptions, tenantId: number, clusterId: string): string[] {
  if (options.clusters.some(row => row.namespaces !== undefined)) {
    return [...new Set(options.clusters.filter(row => row.tenant_id === tenantId
      && (clusterId === '*' || row.cluster_id === clusterId)).flatMap(row => row.namespaces ?? []))].sort()
  }
  if (options.workloads.some(row => row.tenant_id !== undefined)) {
    return [...new Set(options.workloads.filter(row => row.tenant_id === tenantId
      && (clusterId === '*' || row.cluster_id === clusterId)).map(row => row.namespace))].sort()
  }
  const ids = options.clusters.filter(row => row.tenant_id === tenantId)
    .map(row => row.cluster_id).filter(id => clusterId === '*' || id === clusterId)
  return [...new Set(ids.flatMap(id => options.namespaces_by_cluster[id] ?? []))].sort()
}
