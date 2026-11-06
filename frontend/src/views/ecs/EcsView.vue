<script setup lang="ts">
import { computed, reactive, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { monitoringListQuery, readMonitoringContext, resourceMonitoringLocation, resourceViewerScopeKey } from '@/config/resource-navigation'
import { Search } from '@element-plus/icons-vue'
import { listEcsInstances } from '@/api/ecs'
import PageHeader from '@/components/PageHeader.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { fmtTime } from '@/utils/format'
import { metricsSourceLabel, rawStatusType, statusText } from '@/utils/dict'
import type { EcsInstance } from '@/types/ecs'
import { ECS_METRICS, ECS_STATE_LABEL, ecsLatestReadings, ecsTimestampLabel, percentText } from './ecs-utils'
import { useEcsInstances } from './useEcsInstances'
import EcsTrendDrawer from './EcsTrendDrawer.vue'
import EcsPolicyDialog from './EcsPolicyDialog.vue'
import EcsQuotaPanel from './EcsQuotaPanel.vue'

const app = useAppStore()
const user = useUserStore()
const route = useRoute()
const router = useRouter()
const scopeKey = computed(() => resourceViewerScopeKey(user))
const linkError = shallowRef('')
const tab = shallowRef('instances')
const policyVisible = shallowRef(false)
const policyTenantId = shallowRef<number | null>(null)
const quotaPanel = shallowRef<InstanceType<typeof EcsQuotaPanel> | null>(null)
const selectedInstance = shallowRef<EcsInstance | null>(null)
const trend = reactive({ visible: false, instanceId: null as number | null, tenantId: null as number | null })
const trendVisible = computed({ get: () => trend.visible, set: value => { if (!value) void closeTrend() } })
const { items, total, error, filters, page, pageSize, loading, refresh, search, changePage, changePageSize, resetFilters } = useEcsInstances({ tenantId: () => app.tenantId, fetcher: listEcsInstances,
  canView: () => !!user.user && user.can('ledger:view'), scopeKey: () => scopeKey.value })
const rows = computed(() => items.value.map(instance => ({ ...instance, readings: ecsLatestReadings(instance) })))
const hasFilters = computed(() => !!(filters.keyword || filters.critical || filters.monitored))
const canPolicy = computed(() => user.canAny('ledger:view', 'task:view'))
const tenantName = (id: number | null) => app.tenants.find(tenant => tenant.id === id)?.short_name || app.tenants.find(tenant => tenant.id === id)?.name || `租户 #${id}`
const policyTenantName = computed(() => policyTenantId.value ? tenantName(policyTenantId.value) : '')
function openTrend(row: EcsInstance) {
  const location = resourceMonitoringLocation({ ...row, resource_type: 'ecs_server' }, user)
  if (!location) return
  selectedInstance.value = row
  return router.push({ path: '/ecs', query: { ...route.query, instance_id: String(row.id), tenant_id: String(row.tenant_id) } })
}
function closeTrend() { trend.visible = false; return router.replace({ query: monitoringListQuery(route.query) }) }
function openPolicy(tenantId = app.tenantId) {
  policyTenantId.value = tenantId ?? (app.tenants.length === 1 ? app.tenants[0].id : null)
  if (trend.visible) void closeTrend()
  policyVisible.value = true
}
async function refreshPage() { if (tab.value === 'quotas') await quotaPanel.value?.reload(); else await refresh() }
watch(() => [route.query.instance_id, route.query.tenant_id, app.tenantId, scopeKey.value] as const, async (_values, previous) => {
  Object.assign(trend, { visible: false, instanceId: null, tenantId: null })
  if (previous && (previous[2] !== app.tenantId || previous[3] !== scopeKey.value)) {
    policyVisible.value = false; selectedInstance.value = null; linkError.value = ''
    if (route.query.instance_id != null || route.query.tenant_id != null) await router.replace({ query: monitoringListQuery(route.query) })
    return
  }
  const { context, error } = readMonitoringContext(route.query, app.tenantId, user, 'ledger:view')
  linkError.value = error
  if (!context) { selectedInstance.value = null; return }
  if (selectedInstance.value?.id !== context.instanceId || selectedInstance.value.tenant_id !== context.tenantId) selectedInstance.value = null
  tab.value = 'instances'
  Object.assign(trend, { tenantId: context.tenantId, instanceId: context.instanceId, visible: true })
}, { immediate: true, flush: 'sync' })
</script>

<template>
  <div class="ecs-page">
    <PageHeader title="ECS 监控" :refresh="refreshPage" :refreshing="loading || !!quotaPanel?.busy" />
    <el-alert v-if="linkError" :title="linkError" type="error" :closable="false" show-icon />
    <section class="hi-card ecs-instances">
      <el-tabs v-model="tab">
        <el-tab-pane label="实例监控" name="instances">
          <div class="table-heading">
            <div class="table-title"><h2>ECS 实例</h2><span class="text-3">{{ app.tenantLabel }} · {{ total }} 台</span></div>
            <div class="table-actions"><el-button v-if="canPolicy" @click="openPolicy()">采集策略</el-button></div>
          </div>
          <el-form class="ecs-filters" @submit.prevent="search">
            <div class="filter-selects">
              <el-form-item label="核心资源"><el-select v-model="filters.critical" aria-label="核心资源" @change="search"><el-option value="" label="全部" /><el-option value="yes" label="核心" /><el-option value="no" label="非核心" /></el-select></el-form-item>
              <el-form-item label="台账纳管"><el-select v-model="filters.monitored" aria-label="台账纳管" @change="search"><el-option value="" label="全部" /><el-option value="yes" label="已纳入" /><el-option value="no" label="未纳入" /></el-select></el-form-item>
            </div>
            <div class="table-actions search-actions">
              <el-input v-model="filters.keyword" maxlength="200" clearable placeholder="名称 / IP / 资源 ID" aria-label="搜索 ECS 实例" :prefix-icon="Search" @clear="search" />
              <el-button type="primary" :loading="loading" native-type="submit">查询</el-button>
              <el-button v-if="hasFilters" @click="resetFilters">重置</el-button>
            </div>
          </el-form>
          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon><el-button link type="primary" @click="refresh">重试</el-button></el-alert>
          <el-table v-loading="loading" :data="rows" row-key="id" stripe>
            <el-table-column label="实例 / IP" min-width="190"><template #default="{ row }">
              <div class="instance-name"><el-button link type="primary" @click="openTrend(row as EcsInstance)">{{ row.name }}</el-button><el-tag v-if="row.critical" size="small" type="danger" effect="plain">核心</el-tag><el-tag v-if="row.scope_excluded" size="small" type="info" effect="plain" title="被采集范围规则排除，不再采集指标与巡检">范围外</el-tag></div>
              <div class="text-3 mono">{{ row.ip ?? '—' }}</div>
              <div class="text-3 mono resource-id" :title="row.resource_id">{{ row.resource_id }}</div>
            </template></el-table-column>
            <el-table-column label="租户 / 项目" min-width="150"><template #default="{ row }">
              <TenantTag :id="row.tenant_id" :name="row.tenant_name || tenantName(row.tenant_id)" />
              <div class="project-name">{{ row.project_name ?? '未关联项目' }}</div>
            </template></el-table-column>
            <el-table-column label="实例状态" width="85"><template #default="{ row }"><el-tag :type="rawStatusType(row.status_raw)" size="small">{{ statusText('ecs_server', row.status_raw) }}</el-tag></template></el-table-column>
            <el-table-column v-for="(item, index) in ECS_METRICS" :key="item.key" :label="item.label" min-width="150"><template #default="{ row }">
              <div class="metric-reading"><b class="metric-value">{{ percentText(row.readings[index].value) }}</b><span class="metric-state" :class="`state-${row.readings[index].state.toLowerCase()}`" :title="row.readings[index].state !== 'FRESH' ? row.diagnostic?.message || undefined : undefined">{{ ECS_STATE_LABEL[row.readings[index].state as keyof typeof ECS_STATE_LABEL] }}</span></div>
              <el-tooltip :content="`${ecsTimestampLabel(row.readings[index].timestamp_semantics)}；返回：${fmtTime(row.readings[index].observed_at, 'YYYY-MM-DD HH:mm:ss')}；入库：${fmtTime(row.readings[index].collected_at, 'YYYY-MM-DD HH:mm:ss')}`"><div class="text-3 sample-time">返回 {{ fmtTime(row.readings[index].observed_at) }}</div></el-tooltip>
              <div class="text-3 sample-source">{{ row.readings[index].source ? metricsSourceLabel(row.readings[index].source) : '无来源' }}</div>
            </template></el-table-column>
            <el-table-column label="操作" width="95" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openTrend(row as EcsInstance)">监控详情</el-button></template></el-table-column>
            <template #empty><el-empty :description="error ? '实例加载失败，可重试' : hasFilters ? '没有匹配的 ECS 实例，请调整筛选' : '当前范围暂无 ECS 实例'" :image-size="60" /></template>
          </el-table>
          <div class="list-footer"><span class="text-3">缺失值显示 —；时间和来源按各指标展示。</span><el-pagination :current-page="page" :page-size="pageSize" :page-sizes="[20, 50, 100]" :total="total" :pager-count="5" layout="total, sizes, prev, pager, next" @update:current-page="changePage" @update:page-size="changePageSize" /></div>
        </el-tab-pane>
        <el-tab-pane label="项目配额" name="quotas" lazy><EcsQuotaPanel v-if="tab === 'quotas'" ref="quotaPanel" :tenant-id="app.tenantId" /></el-tab-pane>
      </el-tabs>
    </section>
    <EcsTrendDrawer v-model="trendVisible" :instance-id="trend.instanceId" :tenant-id="trend.tenantId" :instance="selectedInstance" @policy="openPolicy" />
    <EcsPolicyDialog v-model="policyVisible" :tenant-id="policyTenantId" :tenant-name="policyTenantName" @saved="refresh" />
  </div>
</template>

<style scoped lang="scss">
.ecs-page { display: flex; flex-direction: column; gap: var(--hi-gap); min-width: 0; }
.ecs-instances { min-width: 0; }
.table-heading { margin: 4px 0 16px; }
.table-title { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: baseline; }
.table-title h2 { margin: 0; font-size: 15px; font-weight: 600; }
.table-title span, .list-footer > span { font-size: 12px; }
.ecs-filters, .filter-selects { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.ecs-filters { margin-bottom: 16px; }
.ecs-filters :deep(.el-form-item) { margin: 0; }
.filter-selects .el-select { width: 100px; }
.search-actions { margin-left: auto; flex-wrap: nowrap; }
.search-actions .el-input { width: 240px; }
.instance-name { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; }
.instance-name .el-button { max-width: 100%; height: auto; white-space: normal; text-align: left; overflow-wrap: anywhere; }
.project-name { margin-top: 4px; overflow-wrap: anywhere; }
.resource-id { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.metric-reading { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.metric-value { font-family: var(--hi-font-num); font-variant-numeric: tabular-nums; font-size: 17px; }
.metric-state { color: var(--hi-text-3); font-size: 12px; }
.state-fresh { color: var(--el-color-success); }
.state-stale, .state-source_unconfigured, .state-mapping_missing { color: var(--el-color-warning); }
.state-error { color: var(--el-color-danger); }
.sample-source, .sample-time, .resource-id { font-size: 12px; }
.sample-source { overflow-wrap: anywhere; }
.list-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-top: 16px; }
.list-footer .el-pagination { max-width: 100%; overflow-x: auto; }
@media (max-width: 767px) {
  .filter-selects { width: 100%; gap: 8px 16px; }
  .filter-selects :deep(.el-form-item) { flex: 1; min-width: 140px; }
  .filter-selects .el-select { width: 100%; min-width: 70px; }
  .search-actions { width: 100%; margin-left: 0; }
  .search-actions .el-input { flex: 1; width: auto; min-width: 0; }
  .list-footer { align-items: flex-start; }
}
</style>
