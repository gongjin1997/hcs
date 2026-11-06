<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import type { GaussPanelModel } from './useGaussPanel'
import { nativeText } from './gauss-utils'
import GaussNativeTable from './GaussNativeTable.vue'
import GaussNativeStatus from './GaussNativeStatus.vue'

const props = defineProps<{ model: GaussPanelModel; canRun: boolean }>()
const user = useUserStore()
const { panel, rows, overview, slowNodes, slowFilters, sessionFilters, slowRange, components, loading, loadingKind, exporting, canExport } = props.model
const advanced = shallowRef(false)
const isSlow = computed(() => panel.value === 'slow-sql')
const nodes = computed(() => (slowNodes.value?.items ?? []).filter(row => typeof row.node_id === 'string'))
const ready = computed(() => props.canRun && !loading.value && (isSlow.value ? slowFilters.node_ids.length > 0 : !!sessionFilters.componentKey))
</script>

<template>
  <div class="query-panel">
    <header class="table-heading"><h3>{{ isSlow ? '慢 SQL' : '实时会话' }}</h3><div class="table-actions"><el-button v-if="user.can('ledger:export')" :icon="Download" :disabled="!canExport" :loading="exporting" @click="model.exportExcel">导出 Excel</el-button><el-button v-if="exporting" @click="model.cancelExport">取消导出</el-button></div></header>
    <form v-if="isSlow" class="query-form" @submit.prevent="model.queryPanel">
      <div class="filter-field time-field"><label>时间范围</label><el-date-picker :model-value="slowRange" type="datetimerange" aria-label="慢 SQL 时间范围" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY-MM-DD HH:mm" @update:model-value="value => model.setRange('slow', value)" /></div>
      <div class="filter-field node-field"><label>节点</label><el-select v-model="slowFilters.node_ids" multiple collapse-tags collapse-tags-tooltip :multiple-limit="20" aria-label="慢 SQL 节点" placeholder="选择 CN / DN 节点" :disabled="!canRun || loadingKind === 'nodes'"><el-option v-for="node in nodes" :key="String(node.node_id)" :value="String(node.node_id)" :label="`${node.node_name || node.node_id} · ${nativeText(node.role)}`" /></el-select></div>
      <div class="filter-field threshold-field"><label>慢 SQL 阈值（秒）</label><el-input-number v-model="slowFilters.threshold" :min="1" :max="99" :precision="0" controls-position="right" aria-label="慢 SQL 阈值秒" /></div>
      <div class="filter-field action-field"><el-button native-type="submit" type="primary" :disabled="!ready" :loading="loadingKind === 'query'">查询</el-button><el-button link :disabled="!canRun || loading" @click="model.loadSlowNodes">重新加载节点</el-button><el-button link @click="advanced = !advanced">{{ advanced ? '收起筛选' : '更多筛选' }}</el-button></div>
      <div v-if="advanced" class="filter-field extra-field"><label>SQL ID</label><el-input v-model="slowFilters.sql_id" aria-label="SQL ID" :maxlength="255" clearable placeholder="可选" /></div>
    </form>
    <form v-else class="query-form" @submit.prevent="model.queryPanel">
      <div class="filter-field component-field"><label>节点 / 组件</label><el-select v-model="sessionFilters.componentKey" aria-label="会话组件" placeholder="选择 CN / DN 主备组件" :disabled="!canRun || loadingKind === 'overview'" filterable><el-option v-for="component in components" :key="model.componentKey(component.node_id, component.component_id)" :value="model.componentKey(component.node_id, component.component_id)" :label="`${component.node_name || component.node_id} · ${component.component_type} · ${component.component_id}`" /></el-select></div>
      <div class="filter-field"><label>数据库</label><el-input v-model="sessionFilters.database_name" aria-label="会话数据库" placeholder="全部数据库" clearable /></div>
      <div class="filter-field"><label>用户</label><el-input v-model="sessionFilters.user_name" aria-label="会话用户" placeholder="全部用户" clearable /></div>
      <div class="filter-field action-field"><el-button native-type="submit" type="primary" :disabled="!ready" :loading="loadingKind === 'query'">查询</el-button><el-button link :disabled="!canRun || loading" @click="model.queryOverview">重新加载组件</el-button><el-button link @click="advanced = !advanced">{{ advanced ? '收起筛选' : '更多筛选' }}</el-button></div>
      <template v-if="advanced">
        <div class="filter-field"><label>客户端 IP</label><el-input v-model="sessionFilters.client_ip" aria-label="会话客户端 IP" clearable /></div>
        <div class="filter-field"><label>状态</label><el-input v-model="sessionFilters.state" aria-label="会话状态" placeholder="原生状态值" clearable /></div>
        <div class="filter-field"><label>等待</label><el-select v-model="sessionFilters.wait" aria-label="会话等待"><el-option value="" label="全部" /><el-option value="t" label="等待" /><el-option value="f" label="未等待" /></el-select></div>
        <div class="filter-field"><label>返回上限</label><el-input-number v-model="sessionFilters.limit" :min="1" :max="200" :precision="0" aria-label="会话条数上限" controls-position="right" /></div>
      </template>
    </form>
    <GaussNativeStatus v-if="isSlow && slowNodes && slowNodes.status !== 'AVAILABLE'" :status="slowNodes.status" :warning="slowNodes.warning" />
    <GaussNativeStatus v-if="!isSlow && overview && overview.components_status !== 'AVAILABLE'" :status="overview.components_status" :warning="overview.components_warning" />
    <el-skeleton v-if="loadingKind === 'query'" :rows="6" animated />
    <GaussNativeTable v-else-if="rows" :result="rows" :kind="isSlow ? 'slow-sql' : 'sessions'" :busy="loading" :can-run="canRun" @detail="model.showDetail" />
    <el-empty v-else :description="isSlow ? '选择节点和时间范围查询慢 SQL' : '选择节点组件查询当前会话'" :image-size="60" />
  </div>
</template>

<style scoped>
.query-panel { min-width: 0; }h3 { margin: 0; font-size: 15px; }.table-heading { min-height: 32px; margin-bottom: 18px; }
.query-form { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 16px 14px; padding-bottom: 22px; margin-bottom: 20px; border-bottom: 1px solid var(--hi-border); }
.filter-field { width: 180px; min-width: 0; }.filter-field label { display: block; font-size: 12px; color: var(--hi-text-2); margin-bottom: 8px; }.filter-field .el-input-number,.filter-field .el-select { width: 100%; }
.time-field { width: 370px; }.time-field .el-date-editor { width: 100%; }.node-field { width: 270px; }.component-field { width: 340px; }.threshold-field { width: 145px; }.action-field { width: auto; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }.action-field .el-button { margin-left: 0; }.extra-field { width: 280px; }
@media (max-width: 700px) { .filter-field,.time-field,.node-field,.component-field { flex: 1 1 calc(50% - 14px); width: auto; }.time-field,.component-field,.action-field,.extra-field { flex-basis: 100%; }.time-field .el-date-editor { min-width: 0; }.filter-field label { line-height: 18px; } }
</style>
