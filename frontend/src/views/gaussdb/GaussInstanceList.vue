<script setup lang="ts">
import { computed } from 'vue'
import { Search } from '@element-plus/icons-vue'
import TenantTag from '@/components/TenantTag.vue'
import HealthBadge from '@/components/HealthBadge.vue'
import { useAppStore } from '@/stores/app'
import { fmtTime } from '@/utils/format'
import { rawStatusType, statusText } from '@/utils/dict'
import type { GaussInstance, GaussProjectOption, GaussStatusGroup } from '@/types/gaussdb'
import type { GaussPanelModel } from './useGaussPanel'
import { DISK_HIGH_PERCENT, FRESHNESS_TEXT, STATUS_GROUPS, componentHealth, diskLevel, diskText, nativeObject, quantityText } from './gauss-utils'

/** GaussDB 实例总览：概况卡片 + 按状态分组的实例表；全部来自本地台账与最近一次巡检，不请求 HCS。 */
const props = defineProps<{ model: GaussPanelModel; projects: GaussProjectOption[]; projectsError: string }>()
const emit = defineEmits<{ open: [instance: GaussInstance]; retryProjects: [] }>()
const app = useAppStore()
const { items, total, page, pageSize, listFilters, listLoading, listError, summary, summaryError } = props.model
const groupValue = computed(() => listFilters.status_group ?? 'all')
const cards = computed(() => {
  const data = summary.value
  return [
    { key: 'disk', label: `磁盘 ≥ ${DISK_HIGH_PERCENT}%`, value: data?.disk_high, alert: !!data?.disk_high },
    { key: 'components', label: '组件宕机', value: data?.components_down, alert: !!data?.components_down },
    { key: 'alarms', label: '活动告警', value: data?.active_alarms, alert: !!data?.active_alarms },
  ]
})
function openRow(value: unknown) {
  const row = nativeObject(value)
  const instance = items.value.find(item => item.id === row?.id && item.tenant_id === row?.tenant_id)
  if (instance) emit('open', instance)
}
function changeGroup(value: string | number | boolean | undefined) {
  const group = STATUS_GROUPS.some(item => item.key === value) ? value as GaussStatusGroup : null
  void props.model.setStatusGroup(group)
}
const groupCount = (key: GaussStatusGroup) => summary.value?.[key]
const lastCollected = (row: GaussInstance) => row.disk_observed_at ?? row.components?.observed_at ?? null
const lastFreshness = (row: GaussInstance) => row.disk_observed_at ? row.disk_freshness : row.components?.freshness
</script>

<template>
  <section class="hi-card instance-list" aria-label="GaussDB 实例列表">
    <header class="table-heading">
      <div><h2>数据库实例 <span class="instance-count">{{ summary?.total ?? total }}</span></h2><p class="text-3">{{ app.tenantLabel }} · 本地台账与最近一次巡检<span v-if="summary?.last_collected_at">，最近采集 {{ fmtTime(summary.last_collected_at, 'MM-DD HH:mm:ss') }}</span></p></div>
      <form class="table-actions instance-filters" aria-label="实例筛选" @submit.prevent="model.search">
        <el-select v-model="listFilters.project_ref" aria-label="GaussDB 项目" placeholder="全部项目" clearable filterable :value-on-clear="null">
          <el-option v-for="project in projects" :key="project.project_ref" :value="project.project_ref" :label="`${project.project_name}${app.multiTenant ? ` · ${project.tenant_name}` : ''}`" />
        </el-select>
        <el-input v-model="listFilters.keyword" aria-label="实例关键字" placeholder="名称 / 实例 ID" :maxlength="200" clearable :prefix-icon="Search" />
        <el-button native-type="submit" type="primary" :loading="listLoading">查询</el-button>
      </form>
    </header>
    <div class="fleet-summary" aria-label="实例概况">
      <el-radio-group :model-value="groupValue" aria-label="实例状态分组" class="group-filter" @update:model-value="changeGroup">
        <el-radio-button value="all">全部 <span class="count">{{ summary?.total ?? '—' }}</span></el-radio-button>
        <el-radio-button v-for="group in STATUS_GROUPS" :key="group.key" :value="group.key" :class="`group-${group.key}`">{{ group.label }} <span class="count">{{ groupCount(group.key) ?? '—' }}</span></el-radio-button>
      </el-radio-group>
      <div class="fleet-cards">
        <div v-for="card in cards" :key="card.key" class="fleet-card" :class="{ alert: card.alert }"><span class="text-3">{{ card.label }}</span><strong class="num">{{ card.value ?? '—' }}</strong></div>
      </div>
    </div>
    <el-alert v-if="listError" :title="listError" type="error" :closable="false" show-icon />
    <div v-if="summaryError" class="filter-error" role="status">{{ summaryError }}</div>
    <div v-if="projectsError" class="filter-error" role="status">{{ projectsError }}<el-button link @click="$emit('retryProjects')">重试</el-button></div>
    <el-table v-loading="listLoading" :data="items" row-key="id">
      <el-table-column label="实例名称" min-width="220"><template #default="{ row }"><el-button class="instance-name" link type="primary" @click="openRow(row)">{{ row.name }}</el-button><el-tag v-if="row.scope_excluded" size="small" type="info" effect="plain" title="被采集范围规则排除，不再采集">范围外</el-tag><div class="instance-id mono text-3" :title="row.resource_id">{{ row.resource_id }}</div></template></el-table-column>
      <el-table-column v-if="app.multiTenant" label="租户" min-width="120"><template #default="{ row }"><TenantTag :id="row.tenant_id" :name="row.tenant_name" /></template></el-table-column>
      <el-table-column label="项目 / 区域" min-width="150"><template #default="{ row }">{{ row.project_name || '未关联项目' }}<div class="text-3">{{ row.region || '—' }}</div></template></el-table-column>
      <el-table-column label="运行状态" min-width="130"><template #default="{ row }"><el-tag :type="rawStatusType(row.status_raw)" effect="plain" size="small" :title="row.status_raw || undefined">{{ statusText('gaussdb_instance', row.status_raw) }}</el-tag><div v-if="row.status_since" class="text-3">自 {{ fmtTime(row.status_since, 'MM-DD HH:mm') }}</div></template></el-table-column>
      <el-table-column label="部署 / 配置" min-width="150"><template #default="{ row }">{{ row.deploy_type || '—' }}<div class="text-3">{{ quantityText(row.vcpus, 'vCPU') }} · {{ quantityText(row.memory_size, row.memory_unit) }}</div></template></el-table-column>
      <el-table-column label="磁盘使用" min-width="150"><template #default="{ row }"><el-tag size="small" :type="diskLevel(row.disk_usage)" effect="plain">{{ diskText(row) }}</el-tag><div v-if="row.disk_total_gb != null" class="text-3">{{ quantityText(row.disk_used_gb ?? null, 'GB') }} / {{ quantityText(row.disk_total_gb, 'GB') }}</div></template></el-table-column>
      <el-table-column label="组件" min-width="140"><template #default="{ row }"><el-tag size="small" :type="componentHealth(row.components).type" effect="plain">{{ componentHealth(row.components).text }}</el-tag></template></el-table-column>
      <el-table-column label="告警 / 巡检" min-width="130"><template #default="{ row }"><span v-if="row.scope_excluded" class="text-3">范围外</span><HealthBadge v-else :value="row.health" /><div v-if="row.active_alarms" class="text-3">{{ row.active_alarms }} 条活动告警</div></template></el-table-column>
      <el-table-column label="最近采集" min-width="140"><template #default="{ row }"><span :title="fmtTime(lastCollected(row), 'YYYY-MM-DD HH:mm:ss')">{{ lastCollected(row) ? fmtTime(lastCollected(row), 'MM-DD HH:mm:ss') : '—' }}</span><div v-if="lastCollected(row) && lastFreshness(row)" class="text-3">{{ FRESHNESS_TEXT[lastFreshness(row)!] }}</div></template></el-table-column>
      <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openRow(row)">查看</el-button></template></el-table-column>
      <template #empty><el-empty :description="listError ? '实例查询失败，可在顶栏重试' : listLoading ? '加载中' : '当前范围没有 GaussDB 实例'" :image-size="54" /></template>
    </el-table>
    <div class="pager"><el-pagination :current-page="page" :page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" @update:current-page="model.changePage" @update:page-size="model.changePageSize" /></div>
  </section>
</template>

<style scoped>
.instance-list { min-width: 0; }.table-heading { align-items: flex-start; margin-bottom: 16px; }
h2 { margin: 0; font-size: 18px; }.instance-count { color: var(--hi-text-3); font-size: 14px; font-weight: 400; margin-left: 6px; }
p { margin: 7px 0 0; font-size: 12px; }
.instance-filters { margin-left: auto; }.instance-filters .el-select { width: 210px; }.instance-filters .el-input { width: 235px; }
.fleet-summary { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
.group-filter .count { margin-left: 4px; color: var(--hi-text-3); }
.group-filter .group-abnormal .count { color: var(--el-color-danger); font-weight: 600; }
.fleet-cards { display: flex; gap: 10px; flex-wrap: wrap; }
.fleet-card { display: flex; align-items: baseline; gap: 8px; padding: 6px 12px; border: 1px solid var(--hi-border-light); border-radius: 6px; font-size: 12px; }
.fleet-card strong { font-size: 16px; }.fleet-card.alert { border-color: var(--el-color-danger-light-5); }.fleet-card.alert strong { color: var(--el-color-danger); }
.instance-name { height: auto; font-weight: 600; white-space: normal; text-align: left; }
.instance-id { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; margin-top: 5px; }
.filter-error { font-size: 13px; margin-bottom: 12px; color: var(--el-color-danger); }.pager { overflow-x: auto; }
@media (max-width: 800px) { .instance-filters { width: 100%; margin: 10px 0 0; }.instance-filters .el-input,.instance-filters .el-select { flex: 1; min-width: 160px; }.table-heading { flex-wrap: wrap; }.group-filter { display: flex; flex-wrap: wrap; } }
</style>
