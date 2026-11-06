<script setup lang="ts">
import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { listEcsQuotas, refreshEcsQuota } from '@/api/ecs'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import { fmtTime } from '@/utils/format'
import type { EcsProjectQuota, EcsQuotaItem } from '@/types/ecs'
import { errorText } from './ecs-utils'

const props = defineProps<{ tenantId: number | null }>()
const user = useUserStore()
const request = useLatestRequest(), refreshRequest = useLatestRequest()
const items = shallowRef<EcsProjectQuota[]>([]), total = shallowRef(0), page = shallowRef(1)
const error = shallowRef(''), refreshError = shallowRef(''), refreshingProject = shallowRef<number | null>(null)
const keyword = shallowRef('')
const expandedProject = shallowRef('')
const loading = request.loading
const busy = computed(() => loading.value || refreshRequest.loading.value)
const canRefresh = computed(() => user.canAny('ledger:sync', 'task:run'))
let controller: AbortController | null = null, refreshController: AbortController | null = null
const filteredItems = computed(() => { const search = keyword.value.trim().toLowerCase(); return items.value.filter(item => !search || `${item.project_name} ${item.project_id} ${item.tenant_name} ${item.region}`.toLowerCase().includes(search)) })
const statusLabels = { not_collected: '未采集', available: '已采集', partial: '部分字段缺失', refresh_failed: '刷新失败' }
const errorLabels = { upstream_error: '上游请求失败', invalid_response: '返回数据无效', configuration_error: '服务配置不完整' }
const projectKey = (item: EcsProjectQuota) => `${item.tenant_id}:${item.project_ref}`
watch(filteredItems, rows => {
  if (!rows.some(item => projectKey(item) === expandedProject.value)) expandedProject.value = rows[0] ? projectKey(rows[0]) : ''
})
function cancel() { request.invalidate(); refreshRequest.invalidate(); controller?.abort(); refreshController?.abort(); refreshingProject.value = null }
async function reload() {
  if (refreshRequest.loading.value) return
  request.invalidate(); controller?.abort()
  error.value = ''; items.value = []; total.value = 0
  const tenantId = props.tenantId, local = new AbortController(); controller = local
  try { await request.run(() => listEcsQuotas({ page: page.value, page_size: 20 }, { tenantId, signal: local.signal }), value => { items.value = value.items; total.value = value.total }) }
  catch (cause) { if (!local.signal.aborted) error.value = errorText(cause, '项目配额加载失败') }
}
async function refreshProject(item: EcsProjectQuota) {
  if (!canRefresh.value || refreshRequest.loading.value || !item.enabled) return
  const tenantId = item.tenant_id, projectRef = item.project_ref, local = new AbortController()
  refreshingProject.value = projectRef; refreshError.value = ''; refreshController = local
  try {
    const applied = await refreshRequest.run(() => refreshEcsQuota(projectRef, { tenantId, signal: local.signal }), value => {
      items.value = items.value.map(row => row.project_ref === projectRef && row.tenant_id === tenantId ? value : row)
    })
    if (applied) ElMessage.success('项目配额已刷新')
  } catch (cause) {
    if (!local.signal.aborted) { refreshError.value = errorText(cause, '配额刷新失败'); await reload() }
  } finally { if (refreshingProject.value === projectRef) refreshingProject.value = null }
}
function limitText(item: EcsQuotaItem) { return item.limit_status === 'unlimited' ? '不限额' : item.limit_status === 'known' && item.limit != null ? item.limit.toLocaleString() : '未知' }
function usedText(item: EcsQuotaItem) { return item.usage_status === 'not_reported' ? '接口未提供' : item.usage_status === 'known' && item.used != null ? item.used.toLocaleString() : '未知' }
function ratio(item: EcsQuotaItem) { return item.limit_status === 'known' && item.usage_status === 'known' && item.limit != null && item.limit > 0 && item.used != null ? item.used / item.limit * 100 : null }
watch(() => props.tenantId, () => { cancel(); page.value = 1; keyword.value = ''; refreshError.value = ''; void reload() }, { immediate: true, flush: 'sync' })
onScopeDispose(cancel)
defineExpose({ reload, busy })
</script>

<template>
  <div class="quota-panel">
    <div class="table-heading"><div><b>项目实际配额</b><span class="text-3 scope-caption"> · {{ total }} 个项目</span></div><div class="table-actions quota-search"><el-input v-model="keyword" clearable placeholder="筛选当前页项目 / 租户 / 区域" aria-label="筛选项目配额" /></div></div>
    <p class="text-3 quota-hint">按项目展开查看。此处筛选当前页；页面刷新读取已采集配额，单项目刷新需相应权限。</p>
    <el-alert v-if="error || refreshError" :title="error || refreshError" type="error" :closable="false" show-icon><el-button v-if="error" link @click="reload">重试</el-button></el-alert>
    <el-skeleton v-if="loading" :rows="6" animated />
    <template v-else>
      <el-collapse v-if="filteredItems.length" v-model="expandedProject" accordion class="quota-projects">
      <el-collapse-item v-for="item in filteredItems" :key="projectKey(item)" :name="projectKey(item)" :title="`${item.project_name || item.project_id} · ${item.tenant_name} · ${item.region}`">
        <template #title><div class="project-summary"><div class="project-identity"><b>{{ item.project_name || item.project_id }}</b><span class="text-3">{{ item.tenant_name }} · {{ item.region }}</span></div><div class="project-state"><el-tag size="small" :type="item.status === 'refresh_failed' || item.is_stale ? 'warning' : 'info'">{{ item.is_stale ? '数据已过期' : statusLabels[item.status] }}</el-tag><el-tag v-if="!item.enabled" size="small" type="warning">采集已停用</el-tag><span class="text-3">获取 {{ fmtTime(item.fetched_at) }}</span></div></div></template>
        <section class="project-quota">
        <div class="table-heading">
          <div class="text-3 scope-caption">{{ item.project_id }} · {{ statusLabels[item.status] }}</div>
          <div class="table-actions"><el-button v-if="canRefresh" :loading="refreshingProject === item.project_ref" :disabled="busy || !item.enabled" @click="refreshProject(item)">刷新项目配额</el-button></div>
        </div>
        <el-alert v-if="item.is_stale || item.last_error" :title="`${item.last_error ? errorLabels[item.last_error] : '数据已过期'}；${item.fetched_at ? '保留上次成功值' : '尚无成功采集记录'}。最近尝试：${fmtTime(item.last_attempt_at)}`" type="warning" :closable="false" />
        <div class="text-3 source-caption">来源 {{ item.source }} · 获取时间 {{ fmtTime(item.fetched_at) }} · 源端未提供采样时间</div>
        <el-table :data="item.quotas" row-key="key" size="small" stripe>
          <el-table-column label="配额项" min-width="190"><template #default="{ row }">{{ row.label }} <span class="text-3">{{ row.unit === 'count' ? '个' : row.unit }}</span></template></el-table-column>
          <el-table-column label="已用" min-width="120"><template #default="{ row }">{{ usedText(row as EcsQuotaItem) }}</template></el-table-column>
          <el-table-column label="限额" min-width="100"><template #default="{ row }">{{ limitText(row as EcsQuotaItem) }}</template></el-table-column>
          <el-table-column label="剩余" min-width="100"><template #default="{ row }"><span :class="{ 'text-danger': row.remaining != null && row.remaining < 0 }">{{ row.remaining == null ? '—' : row.remaining.toLocaleString() }}</span></template></el-table-column>
          <el-table-column label="使用率" min-width="160"><template #default="{ row }"><div v-if="ratio(row as EcsQuotaItem) != null" class="quota-ratio"><el-progress :percentage="Math.min(100, ratio(row as EcsQuotaItem)!)" :show-text="false" :status="ratio(row as EcsQuotaItem)! >= 100 ? 'exception' : undefined" /><span>{{ ratio(row as EcsQuotaItem)!.toFixed(1) }}%</span></div><span v-else>—</span></template></el-table-column>
          <el-table-column label="字段与说明" min-width="220"><template #default="{ row }"><div class="text-3 mono">{{ row.limit_field }}<template v-if="row.usage_field"> / {{ row.usage_field }}</template></div><span class="text-3">{{ row.note }}</span></template></el-table-column>
          <template #empty><el-empty description="尚无实际配额数据" :image-size="40" /></template>
        </el-table>
        </section>
      </el-collapse-item>
      </el-collapse>
      <el-empty v-if="!filteredItems.length" :description="keyword ? '当前页没有匹配项目' : '当前范围暂无项目'" :image-size="60" />
    </template>
    <el-pagination :current-page="page" :page-size="20" :total="total" :pager-count="5" layout="total, prev, pager, next" @update:current-page="value => { page = value; reload() }" />
  </div>
</template>

<style scoped lang="scss">
.quota-panel { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.table-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.table-actions .el-input { width: 290px; max-width: 100%; }
.quota-hint { margin: 0; font-size: 12px; line-height: 1.7; }
.project-summary { display: flex; flex: 1; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding: 12px 12px 12px 0; min-width: 0; text-align: left; }
.project-identity, .project-state { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; min-width: 0; }
.project-identity b { overflow-wrap: anywhere; }
.project-identity .text-3, .project-state .text-3 { font-size: 12px; }
.quota-projects :deep(.el-collapse-item__header) { height: auto; min-height: 64px; line-height: 1.6; }
.quota-projects :deep(.el-collapse-item__content) { padding-bottom: 16px; }
.project-quota { padding: 0 0 0 12px; border-left: 2px solid var(--hi-border); min-width: 0; }
.scope-caption, .source-caption { font-size: 12px; overflow-wrap: anywhere; margin: 8px 0; }
.quota-ratio { display: flex; align-items: center; gap: 10px; }
.quota-ratio .el-progress { flex: 1; min-width: 40px; }
.quota-ratio span { min-width: 45px; font-size: 12px; }
.el-pagination { max-width: 100%; overflow-x: auto; }
@media (max-width: 767px) {
  .quota-search { width: 100%; }
  .quota-search .el-input { width: 100%; }
  .project-summary { gap: 6px; }
  .project-identity, .project-state { width: 100%; }
  .project-quota { padding-left: 8px; }
}
</style>
