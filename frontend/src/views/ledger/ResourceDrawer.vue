<template>
  <el-drawer :model-value="modelValue" size="min(780px, 100vw)" destroy-on-close class="res-drawer" @update:model-value="$emit('update:modelValue', $event)">
    <template #header>
      <div v-if="r" class="d-head">
        <div class="t1">
          <b class="name">{{ r.name }}</b>
          <el-tag :type="rawStatusType(r.status_raw)" size="small" effect="light">{{ statusText(r.resource_type, r.status_raw) }}</el-tag>
          <el-tag v-if="r.lifecycle === 'DELETED'" type="info" size="small" effect="dark">已释放</el-tag>
          <el-tag v-if="r.critical" type="danger" size="small" effect="plain">核心</el-tag>
          <HealthBadge v-if="inspected" :value="r.health" />
        </div>
        <div class="t2">
          <TenantTag v-if="data?.tenant" :id="data.tenant.id" :name="data.tenant.short_name" :color="data.tenant.color" />
          <span class="chip">{{ typeName(r.resource_type) }}</span>
          <span class="mono">{{ r.resource_id }}</span>
          <el-button link :icon="CopyDocument" @click="copy(r.resource_id)" />
        </div>
      </div>
    </template>

    <div v-loading="loading" class="d-body">
      <template v-if="data && r">
        <el-tabs v-model="tab">
          <el-tab-pane label="台账信息" name="info">
            <!-- 管理信息：运维维护，修改会写入变更记录 -->
            <div class="section-title">管理信息<span class="sub">由运维维护；修改记入变更记录（来源：人工维护）</span></div>
            <el-form class="mgmt" label-width="84px" :disabled="r.lifecycle === 'DELETED' || !canEdit">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12"><el-form-item label="负责人">
                  <el-select v-model="edit.owner_user_id" filterable clearable :value-on-clear="null" :disabled="ownerOptionsFailed || loading"
                             :placeholder="historicalOwner && !ownerCleared ? historicalOwner : '选择用户管理中已启用的人员'">
                    <el-option v-if="historicalOwner && r.owner_user_id != null" :key="r.owner_user_id" :label="historicalOwner" :value="r.owner_user_id" disabled />
                    <el-option v-for="o in ownerOptions" :key="o.id" :label="`${o.display_name}（${o.username}）`" :value="o.id" />
                  </el-select>
                  <div v-if="historicalOwner && !ownerCleared" class="text-3 owner-tip">当前负责人：{{ historicalOwner }}；未重新选择时保留历史值。
                    <el-button v-if="r.owner_user_id == null" link type="primary" @click="clearHistoricalOwner">清空历史负责人</el-button>
                  </div>
                  <div v-if="ownerOptionsFailed" class="text-danger owner-tip">人员选项加载失败，请重新打开详情后重试；其他管理字段仍可保存。</div>
                  </el-form-item></el-col>
                <el-col :xs="24" :sm="12"><el-form-item label="业务系统">
                  <el-select v-model="edit.business" filterable :allow-create="!enforce" clearable default-first-option :placeholder="enforce ? '从台账字典选择' : '未填写'">
                    <el-option v-for="o in options.business" :key="o" :label="o" :value="o" />
                  </el-select></el-form-item></el-col>
                <el-col :xs="24" :sm="12"><el-form-item label="环境">
                  <el-radio-group v-model="edit.env_type">
                    <el-radio-button v-for="(v, k) in ENV_TYPE" :key="k" :value="k">{{ v.label }}</el-radio-button>
                  </el-radio-group></el-form-item></el-col>
                <el-col :xs="24" :sm="12"><el-form-item label="核心资源">
                  <el-switch v-model="edit.critical" /><span class="text-3 hint">健康分权重 ×3，报表重点关注</span></el-form-item></el-col>
                <el-col v-if="inspected" :span="12"><el-form-item label="纳入巡检">
                  <el-switch v-model="edit.monitored" /><span class="text-3 hint">关闭后不产生新告警</span></el-form-item></el-col>
                <el-col v-if="hasExpectedRunning" :span="12"><el-form-item :label="isEcs ? '期望开机' : '期望运行'">
                  <el-switch v-model="edit.running" /><span class="text-3 hint">{{ isEcs ? (edit.running ? '关机即告警（R-ECS-002）' : '允许关机：关机不告警') : '期望停止时不告警' }}</span></el-form-item></el-col>
                <el-col :xs="24" :sm="24"><el-form-item label="备注">
                  <el-input v-model="edit.remark" type="textarea" :rows="2" maxlength="200" show-word-limit placeholder="用途、到期时间、待办事项等" /></el-form-item></el-col>
              </el-row>
              <div v-if="canEdit" class="mgmt-foot">
                <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">保存</el-button>
                <el-button :disabled="!dirty" @click="fill">撤销修改</el-button>
              </div>
            </el-form>

            <div class="section-title">基本信息<span class="sub">{{ isEcs ? '按租户 ECS 策略从 HCS 同步，不可手工修改' : '每小时从 HCS 同步，不可手工修改' }}</span></div>
            <div class="kv-grid">
              <div class="k">资源类型</div><div>{{ typeName(r.resource_type) }}</div>
              <div class="k">分类</div><div>{{ r.classification || '其他' }}<span class="text-3"> · {{ r.classification_source ? '映射规则' : '默认分类' }}</span></div>
              <div class="k">所属服务</div><div>{{ SERVICES[r.service] ?? r.service }}</div>
              <div class="k">区域 / AZ</div><div>{{ r.region ?? '-' }} / {{ r.az ?? '-' }}</div>
              <div class="k">IP 地址</div><div class="mono">{{ r.ip ?? '-' }}</div>
              <div class="k">规格</div><div>{{ r.spec ?? '-' }}</div>
              <div class="k">存储</div><div>{{ r.storage_gb != null ? `${r.storage_gb} GB` : '-' }}</div>
              <div class="k">创建时间</div><div>{{ fmtTime(r.created_time, 'YYYY-MM-DD HH:mm') }}</div>
              <div class="k">首次纳入台账</div><div>{{ fmtTime(r.first_seen, 'YYYY-MM-DD HH:mm') }}</div>
              <div class="k">最近同步</div><div>{{ fmtTime(r.last_seen, 'YYYY-MM-DD HH:mm') }}（{{ fromNow(r.last_seen) }}）</div>
              <div class="k">{{ r.lifecycle === 'DELETED' ? '释放时间' : '状态持续' }}</div>
              <div>{{ r.lifecycle === 'DELETED' ? fmtTime(r.deleted_at, 'YYYY-MM-DD HH:mm') : durationOf(r.status_since) }}</div>
            </div>

            <div class="section-title">扩展属性</div>
            <div class="kv-grid">
              <template v-for="(v, k) in plainAttrs" :key="k">
                <div class="k">{{ ATTR_LABEL[k] ?? k }}</div><div :class="{ mono: String(k).endsWith('_id') }">{{ fmtValue(String(k), v) }}</div>
              </template>
              <template v-if="Object.keys(plainAttrs).length % 2"><div class="k" /><div /></template>
            </div>

            <div v-if="monitoringLink" class="section-title">
              <router-link :to="monitoringLink">查看监控</router-link>
            </div>

            <template v-if="Object.keys(r.metrics).length">
              <div class="section-title">{{ isEcs ? '台账指标快照' : '最新指标' }}<span class="sub">{{ isEcs ? '最新值及采样时间请查看指标趋势' : '来自最近一次巡检' }}</span></div>
              <div class="metrics">
                <div v-for="(v, k) in r.metrics" :key="k" class="metric" :title="String(k)"><span>{{ METRIC_LABEL[k] ?? k }}</span>
                  <b class="num" :style="usageStyle(String(k), v)">{{ v ?? '-' }}</b></div>
              </div>
            </template>

            <template v-if="nodes.length">
              <div class="section-title">节点 / 子对象</div>
              <el-table :data="nodes" size="small" border>
                <el-table-column label="名称" prop="name" />
                <el-table-column v-if="nodes[0].role" label="角色" prop="role" width="100" />
                <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="rawStatusType(row.status)" size="small">{{ row.status }}</el-tag></template></el-table-column>
              </el-table>
            </template>
          </el-tab-pane>

          <el-tab-pane :label="`相关告警 ${data.alarms.length || ''}`" name="alarms">
            <el-table :data="data.alarms" size="small" @row-click="(a: Alarm) => $router.push(`/alarms?id=${a.id}`)">
              <el-table-column label="级别" width="90"><template #default="{ row }"><SeverityTag :level="row.severity" /></template></el-table-column>
              <el-table-column label="告警" prop="title" min-width="220" show-overflow-tooltip />
              <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
              <el-table-column label="首次发生" width="130"><template #default="{ row }">{{ fmtTime(row.first_seen) }}</template></el-table-column>
              <template #empty><el-empty :description="inspected ? '近期无告警' : '该类型资源只做台账管理，暂未配置巡检规则'" :image-size="60" /></template>
            </el-table>
          </el-tab-pane>

          <el-tab-pane v-if="isEcs || data.status_changes.length" :label="`状态变更 ${data.status_changes.length || ''}`" name="status">
            <el-timeline v-if="data.status_changes.length" class="timeline">
              <el-timeline-item v-for="c in data.status_changes" :key="c.id" :timestamp="fmtTime(c.detected_at, 'YYYY-MM-DD HH:mm')" placement="top"
                                :color="CHANGE_TYPE[c.change_type]?.color">
                <div class="ch-head"><el-tag :type="CHANGE_TYPE[c.change_type]?.type ?? 'info'" size="small" effect="light">{{ CHANGE_TYPE[c.change_type]?.label ?? c.change_type }}</el-tag>
                  <span class="text-3">{{ CHANGE_SOURCE[c.source] ?? c.source }} · {{ operatorText(c.operator, c.source) }}</span></div>
                <ul v-if="c.changes.length" class="ch-list"><li v-for="f in c.changes" :key="f.field">
                  <b>{{ f.label }}</b><span class="old">{{ statusVal(f.field, f.old) }}</span><el-icon><Right /></el-icon><span class="new">{{ statusVal(f.field, f.new) }}</span></li></ul>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else description="近期没有运行状态变化或重启" :image-size="60" />
          </el-tab-pane>

          <el-tab-pane :label="`变更记录 ${data.changes.length || ''}`" name="changes">
            <el-timeline v-if="data.changes.length" class="timeline">
              <el-timeline-item v-for="c in data.changes" :key="c.id" :timestamp="fmtTime(c.detected_at, 'YYYY-MM-DD HH:mm')" placement="top"
                                :color="CHANGE_TYPE[c.change_type]?.color">
                <div class="ch-head"><el-tag :type="CHANGE_TYPE[c.change_type]?.type ?? 'info'" size="small" effect="light">{{ CHANGE_TYPE[c.change_type]?.label ?? c.change_type }}</el-tag>
                  <span class="text-3">{{ CHANGE_SOURCE[c.source] ?? c.source }} · {{ operatorText(c.operator, c.source) }}</span></div>
                <ul v-if="c.changes.length" class="ch-list"><li v-for="f in c.changes" :key="f.field">
                  <b>{{ f.label }}</b><span class="old">{{ fmtValue('', f.old) }}</span><el-icon><Right /></el-icon><span class="new">{{ fmtValue('', f.new) }}</span></li></ul>
                <div v-else class="text-3">{{ c.change_type === 'CREATED' ? '同步发现新资源，纳入台账' : '同步时在 HCS 中已不存在，标记为已释放' }}</div>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else description="纳入台账以来无变更" :image-size="60" />
          </el-tab-pane>
          <el-tab-pane label="指标与使用指南" name="guide" lazy>
            <ResourceGuidePanel :resource-type="r.resource_type" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { CopyDocument, Right } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { getResource, listUserOptions, patchResource } from '@/api'
import HealthBadge from '@/components/HealthBadge.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import { resourceMonitoringLocation } from '@/config/resource-navigation'
import type { Alarm, ResourceDetail, ResourcePatch, UserOption } from '@/types'
import { CHANGE_SOURCE, CHANGE_TYPE, ENV_TYPE, SERVICES, rawStatusType, statusText, typeName, usageColor } from '@/utils/dict'
import { durationOf, fmtTime, fromNow } from '@/utils/format'
import { ATTR_LABEL, METRIC_LABEL, fmtValue } from './columns'
import { copyText } from '@/utils/clipboard'
import ResourceGuidePanel from './ResourceGuidePanel.vue'

const props = defineProps<{ modelValue: boolean; resourceId: number | null; inspectedTypes: string[]; options: { business: string[] }; enforce?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; saved: [] }>()

const data = ref<ResourceDetail | null>(null)
const ownerOptions = ref<UserOption[]>([])
const ownerOptionsFailed = ref(false)
const ownerCleared = ref(false)
const detailRequest = useLatestRequest()
const loading = detailRequest.loading
const saving = ref(false)
const tab = ref('info')
const r = computed(() => data.value?.resource)
const monitoringLink = computed(() => resourceMonitoringLocation(r.value, useUserStore()))
const inspected = computed(() => !!r.value && props.inspectedTypes.includes(r.value.resource_type))
const isEcs = computed(() => r.value?.resource_type === 'ecs_server')
const hasExpectedRunning = computed(() => ['dgc_realtime_job', 'dcs_instance', 'ecs_server'].includes(r.value?.resource_type ?? ''))
const user = useUserStore()
const canEdit = computed(() => user.can('ledger:edit'))
/** 使用率类指标按水位着色 */
const usageStyle = (k: string, v: unknown) => (/_(util|usage)$/.test(k) && typeof v === 'number' ? { color: usageColor(v) } : undefined)
const operatorText = (op: string, source: string) => (op === 'system' ? (source === 'INSPECT' ? '巡检发现' : '自动同步') : op)
/** 状态变更：status_raw 显示中文状态，boot_time 显示本地时间 */
function statusVal(field: string, v: unknown) {
  if (v == null || v === '') return '-'
  if (field === 'status_raw') return statusText(r.value?.resource_type ?? '', String(v))
  if (field === 'boot_time') return fmtTime(String(v), 'MM-DD HH:mm:ss')
  return fmtValue(field, v)
}
const plainAttrs = computed(() => Object.fromEntries(Object.entries(r.value?.attrs ?? {}).filter(([, v]) => typeof v !== 'object' || Array.isArray(v))))
const nodes = computed(() => (r.value?.attrs.nodes ?? []) as { name: string; status: string; role?: string }[])

const historicalOwner = computed(() => {
  const x = r.value
  if (!x || (x.owner_user_id != null && ownerOptions.value.some(o => o.id === x.owner_user_id))) return ''
  if (x.owner_user_id != null) return `${x.owner || `#${x.owner_user_id}`}（当前不可选）`
  return x.owner ? `${x.owner}（历史未关联）` : ''
})
const edit = reactive({ owner_user_id: null as number | null, business: '', env_type: '', remark: '', critical: false, monitored: true, running: true })
const origin = ref('')
const snapshot = () => JSON.stringify({ ...edit, ownerCleared: ownerCleared.value })
const dirty = computed(() => snapshot() !== origin.value)
function fill() {
  const x = r.value
  if (!x) return
  ownerCleared.value = false
  Object.assign(edit, { owner_user_id: x.owner_user_id ?? null, business: x.business ?? '', env_type: x.env_type ?? '', remark: x.remark ?? '',
    critical: x.critical, monitored: x.monitored, running: x.expected_state.running !== false })
  origin.value = snapshot()
}
function clearHistoricalOwner() { if (canEdit.value) { edit.owner_user_id = null; ownerCleared.value = true } }

async function load(id: number) {
  return detailRequest.run(async () => {
    const detail = await getResource(id)
    let owners: UserOption[] = []
    let failed = false
    if (canEdit.value && props.modelValue && props.resourceId === id) {
      try { owners = await listUserOptions(detail.resource.tenant_id) }
      catch { failed = true }
    }
    return { detail, owners, failed }
  }, ({ detail, owners, failed }) => {
    data.value = detail
    ownerOptions.value = owners.filter(o => o.enabled)
    ownerOptionsFailed.value = failed
    fill()
  })
}
watch(() => [props.modelValue, props.resourceId] as const, ([visible, id]) => {
  detailRequest.invalidate()
  data.value = null
  ownerOptions.value = []
  ownerOptionsFailed.value = false
  if (visible && id) { tab.value = 'info'; void load(id).catch(() => undefined) }
}, { immediate: true })

async function save() {
  if (saving.value || loading.value || !canEdit.value || !r.value || !props.modelValue) return
  const x = r.value
  const body: ResourcePatch = {
    business: edit.business || null, env_type: edit.env_type || null, remark: edit.remark || null,
    critical: edit.critical, monitored: edit.monitored,
  }
  // 不重发历史负责人：未关联 / 已停用 / 范围变化的旧值不能因编辑备注被清空。
  if (ownerCleared.value || edit.owner_user_id !== (x.owner_user_id ?? null)) {
    if (edit.owner_user_id != null && !ownerOptions.value.some(o => o.id === edit.owner_user_id)) {
      return ElMessage.warning('请从当前租户已启用的人员中选择负责人')
    }
    body.owner_user_id = edit.owner_user_id
  }
  if (hasExpectedRunning.value) body.expected_state = { ...x.expected_state, running: edit.running }
  saving.value = true
  try {
    await patchResource(x.id, body)
    ElMessage.success('已保存')
    if (props.modelValue && props.resourceId === x.id) await load(x.id)
    emit('saved')
  } finally { saving.value = false }
}
async function copy(text: string) {
  if (await copyText(text)) ElMessage.success('已复制资源 ID')
  else ElMessage.warning('复制失败，请手动选择资源 ID')
}
</script>

<style scoped lang="scss">
.d-head { display: flex; flex-direction: column; gap: 6px; min-width: 0;
  .t1 { display: flex; align-items: center; gap: 8px; .name { font-size: 17px; color: var(--hi-text-1); } }
  .t2 { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--hi-text-3); font-weight: 400;
    .chip { padding: 1px 8px; border-radius: 4px; background: var(--hi-bg-soft); color: var(--hi-text-2); border: 1px solid var(--hi-border-light); } } }
.d-body { min-height: 300px; :deep(.el-tabs__header) { margin-bottom: 4px; } }
.mgmt { padding: 16px 16px 4px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: var(--hi-bg-soft);
  .el-select { width: 100%; } .hint { margin-left: 8px; }
  .owner-tip { width: 100%; font-size: 12px; line-height: 1.6; margin-top: 4px; }
  .mgmt-foot { padding: 0 0 12px 84px; } }
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  .metric { display: flex; justify-content: space-between; padding: 8px 12px; border: 1px solid var(--hi-border-light); border-radius: 6px; span { color: var(--hi-text-3); } } }
.timeline { padding: 12px 4px 0;
  .ch-head { display: flex; align-items: center; gap: 8px; }
  .ch-list { margin: 8px 0 0; padding: 0; list-style: none; font-size: 13px;
    li { display: flex; align-items: center; gap: 6px; padding: 3px 0; b { font-weight: 500; min-width: 72px; color: var(--hi-text-2); }
      .old { color: var(--hi-text-3); text-decoration: line-through; } .new { color: var(--hi-text-1); font-weight: 500; } } } }
:deep(.el-table__row) { cursor: pointer; }
</style>
