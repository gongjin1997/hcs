<script setup lang="ts">
import { computed, onMounted, onScopeDispose, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { exportCceCluster, getCceCluster, getCceSettings, listCceClusters, runTask, saveCceSettings, updateTask } from '@/api'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { CceSettings, CceSettingsOut, CceSnapshot } from '@/types/cce'
import { fmtTime } from '@/utils/format'
import { cceCondition, cceState } from './cce-display'
import CceNativePanel from './CceNativePanel.vue'

const app = useAppStore()
const rows = ref<CceSnapshot[]>([])
const total = ref(0)
const page = ref(1)
const selected = ref('')
const detail = ref<CceSnapshot | null>(null)
const config = ref<CceSettingsOut | null>(null)
const configTenantId = ref<number | null>(null)
const showConfig = ref(false)
const saving = ref(false)
const taskSaving = ref(false)
const collecting = ref(false)
const downloading = ref(false)
const request = useLatestRequest()
const detailRequest = useLatestRequest()
const configRequest = useLatestRequest()
const loading = request.loading
const form = reactive<CceSettings>({ enabled: false, resolve_owners: false, resource_metrics: false })
let downloadController: AbortController | undefined
const tenantId = computed(() => app.tenantId)
const user = useUserStore()
const canManageConfig = computed(() => user.can('collect:manage'))
const canEditTask = computed(() => !!user.user && user.user.tenant_ids == null && user.can('task:edit'))
const inactiveReasons = computed(() => {
  if (!config.value) return []
  const reasons: string[] = []
  if (!config.value.settings.enabled) reasons.push('本租户节点健康采集未开启')
  if (!config.value.master_enabled) reasons.push('本租户 CCE 资源开关已关闭')
  if (!config.value.task_enabled) reasons.push('全局巡检任务已停用')
  return reasons
})

async function loadDetail(key: string) {
  detailRequest.invalidate()
  selected.value = key
  detail.value = null
  if (!key) return
  return detailRequest.run(() => getCceCluster(key), value => { detail.value = value })
}
async function reload() {
  const loaded = await request.run(() => listCceClusters({ page: page.value, page_size: 50 }), value => {
    rows.value = value.items
    total.value = value.total
    selected.value = value.items.some(row => row.key === selected.value) ? selected.value : value.items[0]?.key ?? ''
  })
  if (loaded) await loadDetail(selected.value)
}
async function loadConfig() {
  const id = configTenantId.value
  config.value = null
  if (!id) return
  return configRequest.run(() => getCceSettings(id), value => {
    config.value = value
    Object.assign(form, value.settings)
  })
}
async function openConfig() {
  configTenantId.value = tenantId.value ?? app.tenants[0]?.id ?? null
  if (!configTenantId.value) return
  showConfig.value = true
  await loadConfig()
}
watch(configTenantId, () => { if (showConfig.value) void loadConfig() })
async function save() {
  const id = configTenantId.value
  if (!id || !config.value || saving.value || !canManageConfig.value) return
  saving.value = true
  try {
    const result = await saveCceSettings(id, { ...form })
    if (configTenantId.value !== id) return
    config.value = result
    showConfig.value = false
    ElMessage.success('已保存')
  } finally { saving.value = false }
}
async function toggleTask(enabled: boolean) {
  if (!config.value || taskSaving.value || !canEditTask.value) return
  taskSaving.value = true
  try {
    const updated = await updateTask('cce_cluster_health', { enabled })
    if (config.value) {
      config.value.task_enabled = updated.enabled
      config.value.effective_enabled = config.value.settings.enabled && config.value.master_enabled && updated.enabled
    }
    ElMessage.success(updated.enabled ? 'CCE 巡检任务已启用' : 'CCE 巡检任务已停用')
  } finally { taskSaving.value = false }
}
async function collect() {
  if (collecting.value) return
  const id = tenantId.value
  collecting.value = true
  try {
    const result = await runTask('cce_cluster_health', { silent: true })
    if (tenantId.value !== id) return
    if (result.status === 'SUCCESS') ElMessage.success('采集完成')
    else if (result.status === 'SKIPPED') ElMessage.info('未执行，请检查租户配置、任务与采集开关')
    else ElMessage.warning('部分数据未采集成功，详见任务日志')
    await reload()
  } catch (error) {
    const failure = error as { code?: unknown; rawMessage?: unknown; message?: unknown }
    const message = String(failure.rawMessage ?? failure.message ?? '')
    if (failure.code === 40901 && message.includes('都已关闭该任务的采集')) {
      ElMessage.info('未执行：当前范围没有开启 CCE 采集的租户，已显示开关状态')
      await openConfig()
      return
    }
    throw error
  } finally { collecting.value = false }
}
async function exportData() {
  if (!detail.value || downloading.value) return
  downloading.value = true
  downloadController = new AbortController()
  const controller = downloadController
  try { await exportCceCluster(detail.value.key, { signal: controller.signal }) }
  catch (error) { if (!controller.signal.aborted) throw error }
  finally { downloading.value = false }
}
function reset() {
  request.invalidate(); detailRequest.invalidate(); configRequest.invalidate()
  downloadController?.abort()
  rows.value = []; detail.value = null; selected.value = ''; total.value = 0; page.value = 1
  config.value = null; showConfig.value = false; configTenantId.value = tenantId.value ?? app.tenants[0]?.id ?? null
}
watch(tenantId, () => { reset(); void reload() }, { flush: 'sync' })
onMounted(reload)
onScopeDispose(() => downloadController?.abort())
defineExpose({ reload })
</script>

<template>
  <section v-loading="loading" class="cce-health">
    <div class="table-heading">
      <b>集群与节点</b>
      <div class="table-actions">
        <el-button v-if="user.canAny('collect:manage', 'task:edit')" :disabled="!app.tenants.length" @click="openConfig">采集配置</el-button>
        <el-button v-perm="'task:run'" :loading="collecting" @click="collect">立即采集</el-button>
        <el-button :disabled="!detail" :loading="downloading" @click="exportData">导出 Excel</el-button>
      </div>
    </div>
    <p class="text-3">节点健康使用独立采集任务。检查时间超过 5 分钟时，状态标记为未知。</p>
    <el-empty v-if="!rows.length && !loading" description="暂无集群健康快照，请选择租户配置采集" :image-size="72" />
    <template v-else>
      <el-select :model-value="selected" class="cluster-select" aria-label="健康集群" @change="loadDetail">
        <el-option v-for="row in rows" :key="row.key" :value="row.key"
                   :label="`${row.tenant_name} / ${row.project_name} / ${row.cluster.name}`" />
      </el-select>
      <el-pagination v-if="total > 50" v-model:current-page="page" :page-size="50" :total="total"
                     layout="prev, pager, next" @current-change="reload" />
      <div v-if="detail" v-loading="detailRequest.loading.value" class="details">
        <div class="scope-line">
          <b>{{ detail.cluster.name }}</b>
          <el-tag :type="detail.freshness === 'fresh' && !detail.missing ? 'success' : 'info'">
            {{ detail.missing ? '最新清单中已不存在' : cceState(detail.freshness) }}
          </el-tag>
          <span class="text-3">采集：{{ fmtTime(detail.last_seen) }}</span>
        </div>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="集群状态">{{ cceState(detail.cluster.phase) }}</el-descriptions-item>
          <el-descriptions-item label="版本">{{ detail.cluster.version ?? '未知' }}</el-descriptions-item>
          <el-descriptions-item label="节点总数 / 正常节点">{{ detail.cluster.capacity.total_nodes ?? '未知' }} / {{ detail.cluster.capacity.active_nodes ?? '未知' }}</el-descriptions-item>
          <el-descriptions-item label="CPU / 内存容量（原值）">{{ detail.cluster.capacity.total_cpu_raw ?? '未知' }} / {{ detail.cluster.capacity.total_memory_raw ?? '未知' }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.cluster.reason || detail.cluster.message" label="异常原因" :span="2">
            {{ detail.cluster.reason }} {{ detail.cluster.message }}
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.cluster.job_id" label="关联任务 ID" :span="2">{{ detail.cluster.job_id }}</el-descriptions-item>
          <el-descriptions-item label="访问地址" :span="2">
            <div v-for="endpoint in detail.cluster.endpoints" :key="endpoint.url" class="endpoint">{{ endpoint.type }} · {{ endpoint.url }}</div>
            <span v-if="!detail.cluster.endpoints.length">未知</span>
          </el-descriptions-item>
        </el-descriptions>
        <p class="text-3">容量不代表使用率；内存原值单位待环境确认。访问地址仅供查看。</p>
        <p v-if="detail.cluster.capacity.nodes_consistent === false || detail.cluster.capacity.active_count_consistent === false" class="text-3">
          容量汇总与节点清单暂未一致，请结合采集时间查看。
        </p>
        <p class="text-3">已安装插件：<span v-if="detail.cluster.addons == null">未知</span><span v-else-if="!detail.cluster.addons.length">无</span>
          <span v-for="(addon, index) in detail.cluster.addons ?? []" :key="index" class="condition">{{ addon.name }} {{ addon.version }} · {{ addon.status ?? '未知' }}</span>
        </p>
        <h4>节点 <span class="text-3">{{ cceState(detail.sources.nodes?.state) }} · {{ cceState(detail.sources.nodes?.freshness) }}</span></h4>
        <el-table :data="detail.nodes" max-height="420" row-key="id">
          <el-table-column prop="name" label="节点" min-width="150" />
          <el-table-column label="状态" width="110"><template #default="{ row }">{{ cceState(row.phase) }}</template></el-table-column>
          <el-table-column label="当前健康" width="100"><template #default="{ row }">{{ cceState(row.health) }}</template></el-table-column>
          <el-table-column label="时效" width="90"><template #default="{ row }">{{ cceState(row.freshness) }}</template></el-table-column>
          <el-table-column label="上游检查时间" width="180"><template #default="{ row }">{{ fmtTime(row.last_probe_at) }}</template></el-table-column>
          <el-table-column prop="private_ip" label="私网 IP" min-width="130" />
          <el-table-column label="云服务器" min-width="180"><template #default="{ row }">
            <router-link v-if="row.ecs_resource_ref" :to="{ path: '/ledger', query: { id: row.ecs_resource_ref } }">{{ row.server_id }}</router-link>
            <span v-else>{{ row.server_id ?? '未知' }}</span>
          </template></el-table-column>
        </el-table>
        <h4>节点池 <span class="text-3">{{ cceState(detail.sources.nodepools?.state) }} · {{ cceState(detail.sources.nodepools?.freshness) }}</span></h4>
        <el-table :data="detail.nodepools" max-height="360" row-key="id">
          <el-table-column prop="name" label="节点池" min-width="160" />
          <el-table-column label="当前节点" width="100"><template #default="{ row }">{{ row.current_nodes ?? '未知' }}</template></el-table-column>
          <el-table-column label="创建 / 删除中" width="135"><template #default="{ row }">{{ row.creating_nodes ?? '未知' }} / {{ row.deleting_nodes ?? '未知' }}</template></el-table-column>
          <el-table-column label="条件" min-width="260"><template #default="{ row }">
            <span v-for="(condition, index) in row.conditions" :key="index" class="condition">{{ cceCondition(condition.type, condition.status) }}</span>
            <span v-if="!row.conditions.length">未知</span>
          </template></el-table-column>
        </el-table>
        <p class="text-3">当前节点数不包含删除中的节点。“允许扩容：否”本身不等于故障。</p>
        <CceNativePanel :native="detail.native" />
      </div>
    </template>
    <el-dialog v-model="showConfig" title="CCE 采集配置" width="520px" destroy-on-close>
      <el-form v-if="config" label-width="150px">
        <el-form-item v-if="app.tenants.length > 1" label="租户">
          <el-select v-model="configTenantId" :disabled="saving || taskSaving" aria-label="CCE 配置租户">
            <el-option v-for="tenant in app.tenants" :key="tenant.id" :value="tenant.id" :label="tenant.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="节点健康采集"><el-switch v-model="form.enabled" :disabled="!canManageConfig" /></el-form-item>
        <el-form-item label="工作负载归属"><el-switch v-model="form.resolve_owners" :disabled="!canManageConfig" /></el-form-item>
        <el-form-item label="原生 CPU / 内存"><el-switch v-model="form.resource_metrics" :disabled="!canManageConfig" /></el-form-item>
        <p class="text-3">仅当前租户生效。原生能力需集群只读权限，缺少组件或权限时显示不可用。</p>
        <el-form-item label="CCE 资源开关">
          <el-tag :type="config.master_enabled ? 'success' : 'info'">{{ config.master_enabled ? '已开启' : '已关闭' }}</el-tag>
          <router-link v-if="user.canAny('task:view', 'collect:manage')" class="config-link" :to="{ path: '/system', query: { tab: 'switches' } }" @click="showConfig = false">租户资源开关</router-link>
        </el-form-item>
        <el-form-item label="全局巡检任务">
          <el-switch v-if="canEditTask" :model-value="config.task_enabled" :disabled="taskSaving" @change="(value: string | number | boolean) => toggleTask(!!value)" />
          <el-tag v-else :type="config.task_enabled ? 'success' : 'info'">{{ config.task_enabled ? '已启用' : '已停用' }}</el-tag>
          <span class="text-3">已启用租户 · {{ config.interval_sec }} 秒</span>
          <router-link v-if="user.can('task:view')" class="config-link" :to="{ path: '/system', query: { tab: 'tasks' } }" @click="showConfig = false">巡检任务</router-link>
        </el-form-item>
        <el-alert v-if="!config.effective_enabled" type="warning" :closable="false" :title="`当前未执行：${inactiveReasons.join('；')}`" />
        <p v-if="!canEditTask" class="text-3">启停全局任务需要全部租户范围和“任务编辑”权限。</p>
      </el-form>
      <template #footer><el-button @click="showConfig = false">关闭</el-button><el-button type="primary" :disabled="!config || !canManageConfig" :loading="saving" @click="save">保存租户配置</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.cluster-select { width: min(100%, 680px); margin-bottom: 16px; }
.details { margin-top: 12px; }
.scope-line { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
.endpoint { overflow-wrap: anywhere; }
.config-link { margin-left: 8px; }
.condition { display: inline-block; margin-right: 12px; }
h4 { margin: 24px 0 12px; }
h4 .text-3 { margin-left: 10px; font-size: 12px; font-weight: normal; }
</style>
