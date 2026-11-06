<template>
  <div v-loading="loading" class="probe-panel">
    <div class="table-heading mb-12">
      <b>背压探针</b>
      <div class="table-actions"><el-button v-perm="'podevent:config'" type="primary" :icon="Plus" @click="open()">新增探针</el-button></div>
    </div>
    <el-collapse class="mb-12">
      <el-collapse-item title="探针说明" name="help">
        <p>通过 CCE K8s API 读取 Pod 的 Prometheus 指标，按标签筛选并聚合后与阈值比较。持续超阈值时生成背压事件，达到恢复时长后结束。</p>
        <p>K8s 没有原生背压事件；应用上报的事件包含背压关键字时，也会计入。事件过期不能确认恢复。</p>
        <p>探针仅 GET 读取以 /metrics 结尾的指标路径，应用方须确认该处理器没有写入副作用；需单独授权 get pods/proxy。</p>
      </el-collapse-item>
    </el-collapse>
    <el-table :data="probes" row-key="id">
      <el-table-column label="探针" min-width="180">
        <template #default="{ row }"><b>{{ row.name }}</b><div class="flex gap-6"><TenantTag :id="row.tenant_id" :name="row.tenant_name" />
          <span class="text-3">{{ clusterName(row.cluster_id, row.tenant_id) }} / {{ row.namespace }}</span></div></template>
      </el-table-column>
      <el-table-column label="采集目标" min-width="200">
        <template #default="{ row }"><div class="mono sm">{{ row.selector || '（命名空间内全部 Pod）' }}</div><div class="mono text-3 sm">:{{ row.port }}{{ row.path }}</div></template>
      </el-table-column>
      <el-table-column label="判定条件" min-width="300">
        <template #default="{ row }">
          <div class="mono sm cond">{{ row.agg }}({{ row.metric }}<template v-if="row.label_filter">{{ '{' }}{{ row.label_filter }}{{ '}' }}</template>) {{ row.op }} {{ row.threshold }}{{ row.unit }}</div>
          <div class="text-3 sm">持续 {{ row.for_minutes }} 分钟触发 · 恢复 {{ row.recover_minutes }} 分钟结束</div>
        </template>
      </el-table-column>
      <el-table-column label="最近一次" min-width="170">
        <template #default="{ row }">
          <PodProbeResult :result="row.last_result" :unit="row.unit" :enabled="row.enabled" />
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }"><el-switch :model-value="row.enabled" size="small" :disabled="!canConfig" @change="(v: string | number | boolean) => toggle(row as Probe, !!v)" /></template>
      </el-table-column>
      <el-table-column label="更新" width="120"><template #default="{ row }">{{ row.updated_by ?? '-' }}<div class="text-3 sm">{{ fromNow(row.updated_at) }}</div></template></el-table-column>
      <el-table-column v-if="canConfig" label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="open(row as Probe)">编辑</el-button>
          <el-button link type="danger" @click="remove(row as Probe)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty><el-empty description="还没有背压探针" :image-size="70" /></template>
    </el-table>

    <el-dialog v-model="dlg.visible" :title="dlg.id ? '编辑背压探针' : '新增背压探针'" width="min(760px, calc(100vw - 24px))" top="6vh">
      <el-form ref="formRef" :model="dlg.form" :rules="rules" label-position="top">
        <el-row :gutter="14">
          <el-col :xs="24" :sm="10"><el-form-item label="名称" prop="name"><el-input v-model="dlg.form.name" placeholder="如 Flink 背压（TaskManager）" /></el-form-item></el-col>
          <el-col :xs="24" :sm="6">
            <el-form-item label="租户" prop="tenant_id">
              <el-select v-model="dlg.form.tenant_id" :disabled="!!dlg.id" @change="dlg.form.cluster_id = '*'">
                <el-option v-for="t in app.tenants" :key="t.id" :value="t.id" :label="t.short_name" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-form-item label="集群">
              <el-select v-model="dlg.form.cluster_id">
                <el-option value="*" label="该租户全部 CCE 集群" />
                <el-option v-for="c in clustersOf(dlg.form.tenant_id)" :key="c.cluster_id" :value="c.cluster_id" :label="c.cluster_name" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="14">
          <el-col :xs="24" :sm="6">
            <el-form-item label="命名空间" prop="namespace">
              <el-select v-model="dlg.form.namespace" filterable allow-create default-first-option placeholder="如 flink">
                <el-option v-for="n in nsOptions" :key="n" :value="n" :label="n" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8"><el-form-item label="Pod 标签选择器"><el-input v-model="dlg.form.selector" class="mono-input" placeholder="component=taskmanager" /></el-form-item></el-col>
          <el-col :xs="24" :sm="4"><el-form-item label="端口"><el-input-number v-model="dlg.form.port" :min="1" :max="65535" controls-position="right" class="w-full" /></el-form-item></el-col>
          <el-col :xs="24" :sm="6"><el-form-item label="路径"><el-input v-model="dlg.form.path" class="mono-input" placeholder="/metrics" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="14">
          <el-col :xs="24" :sm="14"><el-form-item label="指标名（Prometheus 格式）" prop="metric"><el-input v-model="dlg.form.metric" class="mono-input" placeholder="flink_taskmanager_job_task_backPressuredTimeMsPerSecond" /></el-form-item></el-col>
          <el-col :xs="24" :sm="10"><el-form-item label="标签过滤（可选）"><el-input v-model="dlg.form.label_filter" class="mono-input" placeholder="job_name=rt_order_sync" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="14">
          <el-col :xs="24" :sm="5">
            <el-form-item label="聚合">
              <el-select v-model="dlg.form.agg"><el-option value="max" label="max 最大" /><el-option value="avg" label="avg 平均" /><el-option value="sum" label="sum 求和" /></el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="4">
            <el-form-item label="比较"><el-select v-model="dlg.form.op"><el-option v-for="o in ['>=', '>', '<=', '<']" :key="o" :value="o" :label="o" /></el-select></el-form-item>
          </el-col>
          <el-col :xs="24" :sm="5"><el-form-item label="阈值" prop="threshold"><el-input-number v-model="dlg.form.threshold" controls-position="right" class="w-full" /></el-form-item></el-col>
          <el-col :xs="24" :sm="4"><el-form-item label="单位"><el-input v-model="dlg.form.unit" placeholder=" ms/s" /></el-form-item></el-col>
          <el-col :xs="24" :sm="3"><el-form-item label="持续(分)"><el-input-number v-model="dlg.form.for_minutes" :min="1" :max="120" controls-position="right" class="w-full" /></el-form-item></el-col>
          <el-col :xs="24" :sm="3"><el-form-item label="恢复(分)"><el-input-number v-model="dlg.form.recover_minutes" :min="1" :max="240" controls-position="right" class="w-full" /></el-form-item></el-col>
        </el-row>
        <div class="preview">
          <span class="text-3">判定：</span>
          <span class="mono">{{ dlg.form.agg }}({{ dlg.form.metric || 'metric' }}<template v-if="dlg.form.label_filter">{{ '{' }}{{ dlg.form.label_filter }}{{ '}' }}</template>) {{ dlg.form.op }} {{ dlg.form.threshold }}{{ dlg.form.unit }}</span>
          <span class="text-3">连续 {{ dlg.form.for_minutes }} 分钟 → 生成「背压」事件；恢复 {{ dlg.form.recover_minutes }} 分钟 → 结束</span>
          <span class="flex-1" /><el-switch v-model="dlg.form.enabled" inline-prompt active-text="启用" inactive-text="停用" />
        </div>
      </el-form>
      <template #footer>
        <el-button @click="dlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { createProbe, deleteProbe, listProbes, updateProbe } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { PodEventOptions, Probe, ProbeIn } from '@/types'
import { fromNow } from '@/utils/format'
import { groupPodClusters, podNamespaces } from './cluster-options'
import PodProbeResult from './PodProbeResult.vue'

const props = defineProps<{ options: PodEventOptions | null }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()
const app = useAppStore()
const user = useUserStore()
const canConfig = computed(() => user.can('podevent:config'))
const loading = ref(false)
const probes = ref<Probe[]>([])

async function load() {
  loading.value = true
  try {
    probes.value = await listProbes()
    emit('count', probes.value.length)
  } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })

const clustersOf = (tenantId: number) => groupPodClusters(props.options?.clusters.filter(c => c.tenant_id === tenantId) ?? [])
const clusterName = (id: string, tenantId: number) => id === '*' ? '全部集群' : clustersOf(tenantId).find(c => c.cluster_id === id)?.cluster_name ?? id.slice(0, 8)

const formRef = ref<FormInstance>()
const blank = (): ProbeIn => ({
  tenant_id: app.currentTenant?.id ?? app.tenants[0]?.id ?? 0, name: '', cluster_id: '*', namespace: '', selector: '', port: 9249, path: '/metrics',
  metric: '', label_filter: '', agg: 'max', op: '>=', threshold: 500, unit: '', for_minutes: 3, recover_minutes: 5, enabled: true,
})
const dlg = reactive({ visible: false, saving: false, id: null as number | null, form: blank() })
const nsOptions = computed(() => {
  const o = props.options
  if (!o) return []
  return podNamespaces(o, dlg.form.tenant_id, dlg.form.cluster_id)
})
const rules: FormRules = {
  name: [{ required: true, message: '请填写名称' }],
  tenant_id: [{ required: true, message: '请选择租户' }],
  namespace: [{ required: true, message: '请填写命名空间' }],
  metric: [{ required: true, message: '请填写指标名' }, { pattern: /^[a-zA-Z_:][a-zA-Z0-9_:]*$/, message: '指标名只能包含字母、数字、下划线和冒号' }],
  threshold: [{ required: true, message: '请填写阈值' }],
}
function open(p?: Probe) {
  dlg.id = p?.id ?? null
  if (p) {
    const { id: _i, tenant_name: _t, updated_by: _u, updated_at: _a, last_result: _l, ...rest } = p
    dlg.form = { ...rest }
  } else dlg.form = blank()
  dlg.visible = true
  setTimeout(() => formRef.value?.clearValidate())
}
async function save() {
  if (!canConfig.value || dlg.saving) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (dlg.saving) return
  dlg.saving = true
  try {
    if (dlg.id) await updateProbe(dlg.id, dlg.form)
    else await createProbe(dlg.form)
    ElMessage.success('已保存，下一个采集周期（约 1 分钟）生效')
    dlg.visible = false
    await load()
  } finally { dlg.saving = false }
}
async function toggle(p: Probe, enabled: boolean) {
  const { id: _i, tenant_name: _t, updated_by: _u, updated_at: _a, last_result: _l, ...rest } = p
  await updateProbe(p.id, { ...rest, enabled })
  ElMessage.success(enabled ? '已启用' : '已停用')
  await load()
}
async function remove(p: Probe) {
  await ElMessageBox.confirm(`删除探针「${p.name}」？已产生的背压事件保留。`, '删除探针', { type: 'warning', confirmButtonText: '删除' })
  await deleteProbe(p.id)
  ElMessage.success('已删除')
  await load()
}
</script>

<style scoped lang="scss">
.intro { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 14px; padding: 10px 14px; border-radius: 6px; font-size: 12px; line-height: 1.8;
  color: var(--hi-text-2); background: var(--el-color-primary-light-9);
  > .el-icon { color: var(--el-color-primary); margin-top: 4px; } > div { flex: 1; } b { color: var(--hi-text-1); } }
.gap-6 { gap: 6px; margin-top: 2px; }
.sm { font-size: 12px; }
.cond { color: var(--hi-text-1); word-break: break-all; }
.hit { color: var(--hi-p2, #f57c00); }
.w-full { width: 100%; }
.mono-input :deep(input) { font-family: var(--hi-font-mono); font-size: 13px; }
.preview { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; border-radius: 6px; background: var(--hi-bg-soft); font-size: 13px;
  border: 1px dashed var(--hi-border); .text-3 { font-size: 12px; } }
.flex-1 { flex: 1; }
</style>
