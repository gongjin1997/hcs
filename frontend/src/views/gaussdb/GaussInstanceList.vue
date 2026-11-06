<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import TenantTag from '@/components/TenantTag.vue'
import HealthBadge from '@/components/HealthBadge.vue'
import { useAppStore } from '@/stores/app'
import { fmtTime } from '@/utils/format'
import { rawStatusType, statusText } from '@/utils/dict'
import type { GaussInstance, GaussProjectOption } from '@/types/gaussdb'
import type { GaussPanelModel } from './useGaussPanel'
import { nativeObject, quantityText } from './gauss-utils'

const props = defineProps<{ model: GaussPanelModel; projects: GaussProjectOption[]; projectsError: string }>()
const emit = defineEmits<{ open: [instance: GaussInstance]; retryProjects: [] }>()
const app = useAppStore()
const { items, total, page, pageSize, listFilters, listLoading, listError } = props.model
function openRow(value: unknown) {
  const row = nativeObject(value)
  const instance = items.value.find(item => item.id === row?.id && item.tenant_id === row?.tenant_id)
  if (instance) emit('open', instance)
}
</script>

<template>
  <section class="hi-card instance-list" aria-label="GaussDB 实例列表">
    <header class="table-heading">
      <div><h2>数据库实例 <span class="instance-count">{{ total }}</span></h2><p class="text-3">{{ app.tenantLabel }}</p></div>
      <form class="table-actions instance-filters" aria-label="实例筛选" @submit.prevent="model.search">
        <el-select v-model="listFilters.project_ref" aria-label="GaussDB 项目" placeholder="全部项目" clearable filterable :value-on-clear="null">
          <el-option v-for="project in projects" :key="project.project_ref" :value="project.project_ref" :label="`${project.project_name}${app.multiTenant ? ` · ${project.tenant_name}` : ''}`" />
        </el-select>
        <el-input v-model="listFilters.keyword" aria-label="实例关键字" placeholder="名称 / 实例 ID" :maxlength="200" clearable :prefix-icon="Search" />
        <el-button native-type="submit" type="primary" :loading="listLoading">查询</el-button>
      </form>
    </header>
    <el-alert v-if="listError" :title="listError" type="error" :closable="false" show-icon />
    <div v-if="projectsError" class="filter-error" role="status">{{ projectsError }}<el-button link @click="$emit('retryProjects')">重试</el-button></div>
    <el-table v-loading="listLoading" :data="items" row-key="id">
      <el-table-column label="实例名称" min-width="230"><template #default="{ row }"><el-button class="instance-name" link type="primary" @click="openRow(row)">{{ row.name }}</el-button><el-tag v-if="row.scope_excluded" size="small" type="info" effect="plain" title="被采集范围规则排除，不再采集">范围外</el-tag><div class="instance-id mono text-3" :title="row.resource_id">{{ row.resource_id }}</div></template></el-table-column>
      <el-table-column v-if="app.multiTenant" label="租户" min-width="120"><template #default="{ row }"><TenantTag :id="row.tenant_id" :name="row.tenant_name" /></template></el-table-column>
      <el-table-column label="项目 / 区域" min-width="160"><template #default="{ row }">{{ row.project_name || '未关联项目' }}<div class="text-3">{{ row.region || '—' }}</div></template></el-table-column>
      <el-table-column label="运行状态" min-width="120"><template #default="{ row }"><el-tag :type="rawStatusType(row.status_raw)" effect="plain" size="small" :title="row.status_raw || undefined">{{ statusText('gaussdb_instance', row.status_raw) }}</el-tag></template></el-table-column>
      <el-table-column label="巡检状态" min-width="100"><template #default="{ row }"><span v-if="row.scope_excluded" class="text-3">范围外</span><HealthBadge v-else :value="row.health" /></template></el-table-column>
      <el-table-column label="配置" min-width="150"><template #default="{ row }"><span>{{ quantityText(row.vcpus, 'vCPU') }}</span><div class="text-3">{{ quantityText(row.memory_size, row.memory_unit) }}</div></template></el-table-column>
      <el-table-column label="台账更新" min-width="145"><template #default="{ row }"><span :title="fmtTime(row.snapshot_at, 'YYYY-MM-DD HH:mm:ss')">{{ fmtTime(row.snapshot_at, 'MM-DD HH:mm:ss') }}</span></template></el-table-column>
      <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openRow(row)">查看</el-button></template></el-table-column>
      <template #empty><el-empty :description="listError ? '实例查询失败，可在顶栏重试' : listLoading ? '加载中' : '当前范围没有 GaussDB 实例'" :image-size="54" /></template>
    </el-table>
    <div class="pager"><el-pagination :current-page="page" :page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" @update:current-page="model.changePage" @update:page-size="model.changePageSize" /></div>
  </section>
</template>

<style scoped>
.instance-list { min-width: 0; }.table-heading { align-items: flex-start; margin-bottom: 20px; }
h2 { margin: 0; font-size: 18px; }.instance-count { color: var(--hi-text-3); font-size: 14px; font-weight: 400; margin-left: 6px; }
p { margin: 7px 0 0; font-size: 12px; }
.instance-filters { margin-left: auto; }.instance-filters .el-select { width: 210px; }.instance-filters .el-input { width: 235px; }
.instance-name { height: auto; font-weight: 600; white-space: normal; text-align: left; }
.instance-id { max-width: 230px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; margin-top: 5px; }
.filter-error { font-size: 13px; margin-bottom: 12px; color: var(--el-color-danger); }.pager { overflow-x: auto; }
@media (max-width: 800px) { .instance-filters { width: 100%; margin: 10px 0 0; }.instance-filters .el-input,.instance-filters .el-select { flex: 1; min-width: 160px; }.table-heading { flex-wrap: wrap; } }
</style>
