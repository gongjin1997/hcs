<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { fetchMonitoringCoverage } from '@/api/monitoring'
import PageHeader from '@/components/PageHeader.vue'
import { monitoringEntryFor, workspaceById } from '@/config/workspace-navigation'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { CoverageState, MonitoringCoverageRow } from '@/types/monitoring'
import { fmtTime } from '@/utils/format'
import { useMonitoringCoverage } from './useMonitoringCoverage'

const app = useAppStore(), user = useUserStore(), router = useRouter()
const { data, error, loading, refresh } = useMonitoringCoverage({ tenantId: () => app.tenantId,
  permissionKey: () => ['ledger:view', 'report:view'].filter(permission => user.can(permission)).join(','), fetcher: fetchMonitoringCoverage })
const resourceType = shallowRef(''), attentionOnly = shallowRef(false)
const services = computed(() => workspaceById('monitoring').items.filter(item => item.resourceType
  && user.canAny(...(Array.isArray(item.perm) ? item.perm : [item.perm]))))
const rows = computed(() => (data.value?.rows ?? []).filter(row => (!resourceType.value || row.resource_type === resourceType.value)
  && (!attentionOnly.value || !['covered', 'no_resources'].includes(row.state))))
const totals = computed(() => rows.value.reduce((sum, row) => ({ resources: sum.resources + row.resources,
  eligible: sum.eligible + row.eligible_resources, inspected: sum.inspected + row.inspected_resources,
  unknown: sum.unknown + row.unknown_resources, abnormal: sum.abnormal + row.abnormal_resources,
}), { resources: 0, eligible: 0, inspected: 0, unknown: 0, abnormal: 0 }))
const stateType = (state: CoverageState) => state === 'covered' ? 'success'
  : state === 'configuration_invalid' ? 'danger' : ['partial', 'not_inspected', 'stale'].includes(state) ? 'warning' : 'info'
const sourceStatus = { collected: '已支持', derived: '按原值计算', not_collected: '当前未采集' }
const canSelectTenant = (row: MonitoringCoverageRow) => app.tenants.some(tenant => tenant.id === row.tenant_id)
function openMonitoring(row: MonitoringCoverageRow) {
  const entry = monitoringEntryFor(row.resource_type)
  if (!entry || !canSelectTenant(row) || !user.canAny(...(Array.isArray(entry.perm) ? entry.perm : [entry.perm]))) return
  app.setTenant(row.tenant_id)
  return router.push(entry.path)
}
function openLedger(row: MonitoringCoverageRow) {
  if (!user.can('ledger:view') || !canSelectTenant(row)) return
  app.setTenant(row.tenant_id)
  return router.push({ path: '/ledger', query: { type: row.resource_type } })
}
</script>

<template>
  <div class="monitoring-overview">
    <PageHeader title="监控覆盖" :refresh="refresh" :refreshing="loading" />
    <el-alert v-if="error" :title="data ? `刷新失败，保留上次结果：${error}` : error" type="error" show-icon :closable="false"><el-button link type="primary" @click="refresh">重试</el-button></el-alert>
    <section v-if="data" class="coverage-totals" aria-label="当前筛选巡检概况">
      <div class="hi-card"><span>资源数</span><strong>{{ totals.resources }}</strong><small>当前范围的活动资源</small></div>
      <div class="hi-card"><span>有效完整巡检</span><strong>{{ totals.inspected }}<em> / {{ totals.eligible }}</em></strong><small>分母为当前可巡检资源</small></div>
      <div class="hi-card"><span>健康未知</span><strong>{{ totals.unknown }}</strong><small>缺少有效巡检，不计为健康</small></div>
      <div class="hi-card"><span>已知异常</span><strong :class="{ danger: totals.abnormal }">{{ totals.abnormal }}</strong><small>过期异常仍保留，等待核查</small></div>
    </section>
    <section class="hi-card coverage-table">
      <div class="table-heading">
        <div class="table-title"><h2>采集与巡检</h2><span class="text-3">{{ app.tenantLabel }}</span></div>
        <div class="table-actions">
          <el-button v-if="user.can('collect:manage')" @click="router.push('/system?tab=switches')">采集开关</el-button>
          <el-button v-if="user.can('task:view')" @click="router.push('/system?tab=tasks')">巡检任务</el-button>
        </div>
      </div>
      <div class="coverage-filters">
        <el-select v-model="resourceType" aria-label="监控服务" clearable placeholder="全部服务"><el-option v-for="service in services" :key="service.path" :label="service.label" :value="service.resourceType!" /></el-select>
        <el-checkbox v-model="attentionOnly">仅看待处理</el-checkbox>
        <span v-if="data" class="evaluated-at text-3">状态核对 {{ fmtTime(data.evaluated_at) }}</span>
      </div>
      <el-table v-loading="loading" :data="rows" :row-key="(row: MonitoringCoverageRow) => `${row.tenant_id}:${row.resource_type}`" stripe>
        <el-table-column v-if="app.multiTenant" prop="tenant_name" label="租户" min-width="150" show-overflow-tooltip />
        <el-table-column prop="resource_name" label="资源" min-width="135" />
        <el-table-column label="状态" min-width="110"><template #default="{ row }"><el-tooltip :disabled="!row.blocker_labels?.length" :content="row.blocker_labels?.join('；')" placement="top"><el-tag :type="stateType(row.state)" size="small">{{ row.state_label }}</el-tag></el-tooltip></template></el-table-column>
        <el-table-column label="采集 / 巡检" min-width="145"><template #default="{ row }">
          <div>台账同步 <span :class="row.ledger_enabled ? 'enabled' : 'text-3'">{{ row.ledger_enabled ? '开启' : '关闭' }}</span></div>
          <div>巡检 <span :class="row.inspection_enabled ? 'enabled' : 'text-3'">{{ row.inspection_enabled ? '开启' : '关闭' }}</span><span class="text-3"> · 项目 {{ row.enabled_projects }}/{{ row.projects }}</span></div>
        </template></el-table-column>
        <el-table-column label="巡检覆盖" min-width="145"><template #default="{ row }">
          <b class="num">{{ row.inspected_resources }} / {{ row.eligible_resources }}</b><span class="text-3"> · 资源 {{ row.resources }}</span><span v-if="row.out_of_scope_resources" class="text-3" title="被采集范围规则排除，不计入覆盖率"> · 范围外 {{ row.out_of_scope_resources }}</span>
          <div v-if="row.partial_resources || row.stale_resources" class="text-3">不完整 {{ row.partial_resources }} · 过期 {{ row.stale_resources }}</div>
        </template></el-table-column>
        <el-table-column label="最近巡检" min-width="155"><template #default="{ row }">{{ fmtTime(row.last_inspected_at) }}</template></el-table-column>
        <el-table-column label="指标范围" width="100"><template #default="{ row }">
          <el-popover trigger="click" placement="bottom" :width="360">
            <template #reference><el-button link type="primary">查看指标</el-button></template>
            <div class="metric-scope"><b>{{ row.source_label }}</b><dl><template v-for="metric in row.metrics" :key="metric.key">
              <dt>{{ metric.label }}<span v-if="metric.unit" class="text-3">（{{ metric.unit }}）</span></dt><dd :class="{ unavailable: metric.availability === 'not_collected' }">{{ sourceStatus[metric.availability as keyof typeof sourceStatus] }}</dd>
            </template></dl><small class="text-3">此处为支持范围；数值和采样时间见实例监控。</small><p><router-link :to="{ path: '/guides', query: { type: row.resource_type } }">查看完整说明</router-link></p></div>
          </el-popover>
        </template></el-table-column>
        <el-table-column label="操作" width="130" fixed="right"><template #default="{ row }">
          <el-button link type="primary" :disabled="!canSelectTenant(row as MonitoringCoverageRow)" @click="openMonitoring(row as MonitoringCoverageRow)">监控</el-button>
          <el-button v-if="user.can('ledger:view')" link type="primary" :disabled="!canSelectTenant(row as MonitoringCoverageRow)" @click="openLedger(row as MonitoringCoverageRow)">台账</el-button>
        </template></el-table-column>
        <template #empty><el-empty :description="error ? '加载失败，请重试' : '当前范围没有匹配的监控项'" :image-size="60" /></template>
      </el-table>
      <p class="coverage-note text-3">巡检覆盖不代表所有指标可用；各项数值及采样时间见实例监控。</p>
    </section>
  </div>
</template>

<style scoped lang="scss">
.monitoring-overview { display: flex; flex-direction: column; gap: var(--hi-gap); min-width: 0; }
.coverage-totals { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--hi-gap); }
.coverage-totals .hi-card { display: flex; flex-direction: column; gap: 9px; }
.coverage-totals strong { font: 600 30px var(--hi-font-num); }
.coverage-totals em { font-size: 16px; font-style: normal; color: var(--hi-text-3); }
.coverage-totals small { color: var(--hi-text-3); line-height: 1.5; }
.coverage-table { min-width: 0; }
.table-title { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: baseline; }
.table-title h2 { margin: 0; font-size: 15px; }
.coverage-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; margin: 16px 0; }
.coverage-filters .el-select { width: 160px; }
.evaluated-at { margin-left: auto; font-size: 12px; }
.coverage-note { font-size: 12px; line-height: 1.7; margin: 16px 0 0; }
.enabled { color: var(--el-color-success); }
.metric-scope { font-size: 12px; line-height: 1.6; }
.metric-scope dl { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 12px; }
.metric-scope dt, .metric-scope dd { margin: 0; }
.metric-scope .unavailable { color: var(--el-color-warning); }
@media (max-width: 767px) {
  .coverage-totals { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .coverage-totals strong { font-size: 24px; }
  .coverage-totals .hi-card { padding: 14px; }
  .coverage-totals small { min-height: 36px; }
  .evaluated-at { margin-left: 0; width: 100%; }
}
</style>
