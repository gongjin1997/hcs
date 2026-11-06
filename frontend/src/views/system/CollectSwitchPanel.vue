<template>
  <div v-loading="loading" class="switch-panel">
    <el-alert type="info" :closable="false" show-icon class="mb-12">
      <template #title>按租户和资源类型启停采集</template>
      <div class="rules">
        <span><b>关闭后</b>：台账同步跳过该类资源（已有台账保留，不会标记为“已释放”）；对应巡检任务对该租户不再执行；该类资源的活动告警自动关闭并留处理记录。</span>
        <span><b>重新开启</b>：下一次台账同步 / 巡检自动恢复。租户设置优先于全局默认；“跟随全局”= 使用全局开关。</span>
        <span><b>采集范围</b>：开关开启后，还可以只采集一部分对象（例如 CCE 只关注某些命名空间、DCS 屏蔽某个实例、屏蔽某个工作空间下的作业）；点单元格里的“设置范围”配置。</span>
      </div>
    </el-alert>

    <div class="toolbar">
      <el-segmented v-model="category" :options="categoryOptions" />
      <el-input v-model="keyword" clearable placeholder="资源类型 / 服务" :prefix-icon="Search" style="width: 200px" />
      <el-checkbox v-model="onlyOff">只看有关闭的</el-checkbox>
      <div class="ml-auto flex gap-8">
        <span class="text-3">{{ offSummary }}</span>
        <template v-if="canManage">
          <el-button :disabled="!dirtyCount" @click="discard">放弃修改</el-button>
          <el-button type="primary" :disabled="!dirtyCount" :loading="saving" @click="onSave">保存<template v-if="dirtyCount">（{{ dirtyCount }} 项）</template></el-button>
        </template>
      </div>
    </div>

    <el-table v-if="data" :data="rows" row-key="code" :span-method="spanMethod" border class="matrix" :row-class-name="rowClass">
      <el-table-column label="分类" width="104" class-name="cat-col">
        <template #default="{ row }"><span class="cat">{{ catName(row.category) }}</span></template>
      </el-table-column>
      <el-table-column label="资源类型" min-width="230">
        <template #default="{ row }">
          <div class="type">
            <b>{{ row.name }}</b>
            <el-tag v-if="row.ledger" size="small" effect="plain">台账</el-tag>
            <el-tooltip v-else content="作业 / 容器工作负载只做巡检，不进资源台账" placement="top"><el-tag size="small" type="info" effect="plain">只巡检</el-tag></el-tooltip>
          </div>
          <div class="text-3 sub">
            <span class="mono">{{ row.code }}</span> · {{ row.service_name }}
            <template v-if="row.tasks.length"> · 巡检：{{ row.tasks.map((t: string) => taskName(t)).join('、') }}</template>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="全局默认" width="120" align="center">
        <template #header>
          <el-tooltip content="租户没有单独设置时使用全局开关；修改全局开关需要全部租户的数据权限" placement="top">
            <span>全局默认 <el-icon class="q"><QuestionFilled /></el-icon></span>
          </el-tooltip>
        </template>
        <template #default="{ row }">
          <div class="cell-wrap" :class="{ dirty: isDirty(row.code, 0) }">
            <el-switch :model-value="globalOf(row as SwitchType)" :disabled="!canManage || !data.global_editable" inline-prompt active-text="开" inactive-text="关"
                       @change="(v: string | number | boolean) => stage(row.code, 0, !!v)" />
            <div v-if="scopes" class="scope-line">
              <el-tag size="small" effect="plain" :type="globalScope(row.code).tone === 'limited' ? 'warning' : 'info'">范围：{{ globalScope(row.code).text }}</el-tag>
              <el-button link type="primary" size="small" @click="openScope(0, '全局默认', row.code)">{{ canManage && data.global_editable ? '设置范围' : '查看范围' }}</el-button>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column v-for="t in data.tenants" :key="t.id" min-width="190" align="center">
        <template #header>
          <span class="t-head"><i :style="{ background: t.color }" />{{ t.short_name || t.name }}<el-tag v-if="!t.enabled" size="small" type="info">已停用</el-tag></span>
        </template>
        <template #default="{ row }">
          <div class="cell-wrap" :class="[{ dirty: isDirty(row.code, t.id) }, effOf(row as SwitchType, t.id) ? 'on' : 'off']">
            <el-select :model-value="settingKey(row as SwitchType, t.id)" size="small" class="sel" :disabled="!canManage"
                       @change="(v: string) => stage(row.code, t.id, v === 'follow' ? null : v === 'on')">
              <el-option value="follow" :label="`跟随全局（${globalOf(row as SwitchType) ? '开' : '关'}）`" />
              <el-option value="on" label="开启" />
              <el-option value="off" label="关闭" />
            </el-select>
            <el-button v-if="row.code === 'ecs_server' && user.canAny('ledger:view', 'task:view')" link type="primary" size="small" @click="openEcsPolicy(t.id, t.short_name || t.name)">ECS 采集策略</el-button>
            <div class="eff">
              <span class="dot" />{{ effOf(row as SwitchType, t.id) ? '采集中' : '已关闭' }}
              <span class="text-3">· 资源 {{ row.tenants[t.id]?.resources ?? 0 }}</span>
              <span v-if="row.tenants[t.id]?.active_alarms" class="alarms">· 告警 {{ row.tenants[t.id].active_alarms }}</span>
            </div>
            <div v-if="scopes" class="scope-line">
              <el-tag size="small" effect="plain" :type="tenantScope(row.code, t.id).tone === 'limited' ? 'warning' : 'info'">范围：{{ tenantScope(row.code, t.id).text }}</el-tag>
              <el-button link type="primary" size="small" @click="openScope(t.id, t.short_name || t.name, row.code)">{{ canManage ? '设置范围' : '查看范围' }}</el-button>
            </div>
          </div>
        </template>
      </el-table-column>
      <template #empty><div class="empty-dash">没有匹配的资源类型</div></template>
    </el-table>
    <div v-if="!canManage" class="text-3 mt-12">当前角色只能查看；修改采集开关需要“采集开关（按资源类型启停）”权限。</div>

    <EcsPolicyDialog v-model="ecsPolicy.visible" :tenant-id="ecsPolicy.tenantId" :tenant-name="ecsPolicy.tenantName" />
    <CollectScopeDialog v-if="scopes" v-model="scopeDialog.visible" :tenant-id="scopeDialog.tenantId" :tenant-name="scopeDialog.tenantName"
                        :type="scopeDialog.type" :can-manage="canManage && (scopeDialog.tenantId !== 0 || scopes.global_editable)"
                        :limits="scopes.limits" @saved="load(true)" />

    <!-- 保存前确认影响 -->
    <el-dialog v-model="confirm.visible" title="确认保存采集开关" width="600px" append-to-body>
      <div v-if="confirm.impact" class="impact">
        <div v-if="confirm.impact.stop.length" class="blk stop">
          <div class="h"><el-icon><VideoPause /></el-icon>将停止采集（{{ confirm.impact.stop.length }}）</div>
          <div class="tags"><el-tag v-for="x in confirm.impact.stop" :key="x" type="danger" effect="plain">{{ x }}</el-tag></div>
          <div v-if="confirm.impact.active_alarms" class="warn">这些资源当前有 <b>{{ confirm.impact.active_alarms }}</b> 条活动告警，保存后会自动关闭（写入处理记录）</div>
        </div>
        <div v-if="confirm.impact.start.length" class="blk start">
          <div class="h"><el-icon><VideoPlay /></el-icon>将恢复采集（{{ confirm.impact.start.length }}）</div>
          <div class="tags"><el-tag v-for="x in confirm.impact.start" :key="x" type="success" effect="plain">{{ x }}</el-tag></div>
          <div class="text-3">下一次台账同步 / 巡检开始采集；可在“巡检任务”里立即执行</div>
        </div>
        <div v-if="!confirm.impact.stop.length && !confirm.impact.start.length" class="text-3">本次仅更新设置，不改变当前采集状态。</div>
      </div>
      <template #footer>
        <el-button @click="confirm.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doSave">确认保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { QuestionFilled, Search, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { getCollectScopes, getCollectSwitches, listTasks, saveCollectSwitches, switchImpact } from '@/api'
import { useUserStore } from '@/stores/user'
import EcsPolicyDialog from '@/views/ecs/EcsPolicyDialog.vue'
import type { CollectScopes, CollectSwitches, ScopeType, SwitchImpact, SwitchItem, SwitchType } from '@/types'
import CollectScopeDialog from './CollectScopeDialog.vue'
import { scopeSummary } from './collect-scope'

const emit = defineEmits<{ changed: [] }>()
const user = useUserStore()
const canManage = computed(() => user.can('collect:manage'))
const loading = ref(false)
const saving = ref(false)
const data = ref<CollectSwitches | null>(null)
/** 采集范围（开关之下的细分）；读取失败只影响范围列，不影响开关本身 */
const scopes = ref<CollectScopes | null>(null)
const scopeDialog = reactive({ visible: false, tenantId: 0, tenantName: '', type: null as ScopeType | null })
const loadScopes = async () => { try { return await getCollectScopes() } catch { return null } }
const scopeType = (code: string) => scopes.value?.types.find((t) => t.code === code) ?? null
const globalScope = (code: string) => {
  const rules = scopeType(code)?.global.rules ?? []
  return scopeSummary({ source: rules.length ? 'tenant' : 'none', rules, excluded: 0 })
}
const tenantScope = (code: string, tid: number) => scopeSummary(scopeType(code)?.tenants[String(tid)])
function openScope(tenantId: number, tenantName: string, code: string) {
  const type = scopeType(code)
  if (type) Object.assign(scopeDialog, { visible: true, tenantId, tenantName, type })
}
const ecsPolicy = reactive({ visible: false, tenantId: null as number | null, tenantName: '' })
function openEcsPolicy(tenantId: number, tenantName: string) { Object.assign(ecsPolicy, { visible: true, tenantId, tenantName }) }
const taskNames = ref<Record<string, string>>({})
const category = ref('all')
const keyword = ref('')
const onlyOff = ref(false)
/** 暂存的修改：key = `${type}|${tenantId}`（tenantId 0 = 全局）；值 null = 跟随全局 */
const staged = reactive(new Map<string, boolean | null>())

async function load(force = false) {
  if (!force && (saving.value || confirm.visible)) return
  loading.value = true
  try {
    data.value = await getCollectSwitches()
    scopes.value = await loadScopes()
    staged.clear()
    if (!Object.keys(taskNames.value).length && user.can('task:view')) {
      listTasks().then((ts) => { taskNames.value = Object.fromEntries(ts.map((t) => [t.code, t.name])) }).catch(() => undefined)
    }
  } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load, busy: computed(() => saving.value || confirm.visible) })

const categoryOptions = computed(() => [{ value: 'all', label: '全部' },
  ...(data.value?.categories ?? []).map((c) => ({ value: c.code, label: c.name }))])
const catName = (c: string) => data.value?.categories.find((x) => x.code === c)?.name ?? c
const taskName = (c: string) => taskNames.value[c] ?? c
const key = (type: string, tid: number) => `${type}|${tid}`

// ---- 当前值（含暂存修改）----
const globalOf = (t: SwitchType) => {
  const k = key(t.code, 0)
  return staged.has(k) ? !!staged.get(k) : t.global
}
const settingOf = (t: SwitchType, tid: number): boolean | null => {
  const k = key(t.code, tid)
  return staged.has(k) ? staged.get(k)! : t.tenants[tid]?.setting ?? null
}
const settingKey = (t: SwitchType, tid: number) => { const v = settingOf(t, tid); return v === null ? 'follow' : v ? 'on' : 'off' }
const effOf = (t: SwitchType, tid: number) => { const v = settingOf(t, tid); return v === null ? globalOf(t) : v }
const isDirty = (type: string, tid: number) => staged.has(key(type, tid))
const dirtyCount = computed(() => staged.size)

function stage(type: string, tid: number, v: boolean | null) {
  if (!canManage.value || saving.value || confirm.visible) return
  const t = data.value!.types.find((x) => x.code === type)!
  const orig = tid === 0 ? t.global : t.tenants[tid]?.setting ?? null
  const k = key(type, tid)
  if (v === orig) staged.delete(k)
  else staged.set(k, v)
}
const discard = () => { if (!saving.value && !confirm.visible) staged.clear() }

// ---- 过滤与合并单元格 ----
const rows = computed(() => {
  const d = data.value
  if (!d) return []
  const kw = keyword.value.trim().toLowerCase()
  const order = d.categories.map((c) => c.code)
  return d.types
    .filter((t) => category.value === 'all' || t.category === category.value)
    .filter((t) => !kw || `${t.name} ${t.code} ${t.service_name}`.toLowerCase().includes(kw))
    .filter((t) => !onlyOff.value || !globalOf(t) || d.tenants.some((x) => !effOf(t, x.id)))
    .sort((a, b) => order.indexOf(a.category) - order.indexOf(b.category))
})
function spanMethod({ row, rowIndex, columnIndex }: { row: SwitchType; rowIndex: number; columnIndex: number }) {
  if (columnIndex !== 0) return { rowspan: 1, colspan: 1 }
  const list = rows.value
  if (rowIndex > 0 && list[rowIndex - 1].category === row.category) return { rowspan: 0, colspan: 0 }
  let n = 1
  while (rowIndex + n < list.length && list[rowIndex + n].category === row.category) n++
  return { rowspan: n, colspan: 1 }
}
const rowClass = ({ row }: { row: SwitchType }) => (data.value?.tenants.some((x) => !effOf(row, x.id)) ? 'has-off' : '')
const offSummary = computed(() => {
  const d = data.value
  if (!d) return ''
  const cells = d.types.flatMap((t) => d.tenants.map((x) => effOf(t, x.id)))
  const off = cells.filter((v) => !v).length
  return off ? `${off} 个“租户 × 资源类型”已关闭采集` : '全部资源类型均在采集'
})

// ---- 保存：先预估影响，确认后保存 ----
const confirm = reactive({ visible: false, impact: null as SwitchImpact | null, items: [] as SwitchItem[] })
const items = (): SwitchItem[] => [...staged.entries()].map(([k, v]) => {
  const [resource_type, tid] = k.split('|')
  return { resource_type, tenant_id: Number(tid), enabled: v }
})
async function onSave() {
  if (!canManage.value || saving.value || !staged.size) return
  const batch = items()
  saving.value = true
  try {
    const impact = await switchImpact(batch)
    if (JSON.stringify(items()) !== JSON.stringify(batch)) return ElMessage.warning('采集开关已变更，请重新预估影响')
    confirm.items = batch
    confirm.impact = impact
    confirm.visible = true
  } finally { saving.value = false }
}
async function doSave() {
  if (!canManage.value || saving.value || !confirm.visible) return
  if (JSON.stringify(items()) !== JSON.stringify(confirm.items)) {
    confirm.visible = false
    return ElMessage.warning('采集开关已变更，请重新预估影响')
  }
  const batch = confirm.items.map(item => ({ ...item }))
  saving.value = true
  try {
    const r = await saveCollectSwitches(batch)
    ElMessage.success(`已保存 ${r.changed.length} 项${r.closed_alarms ? `，自动关闭 ${r.closed_alarms} 条活动告警` : ''}；调度进程下一轮生效`)
    confirm.visible = false
    await load(true)
    emit('changed')
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.rules { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; font-size: 12px; line-height: 1.7; color: var(--hi-text-2); }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.matrix {
  :deep(.cat-col) { background: var(--hi-bg-soft); vertical-align: middle; }
  .cat { font-weight: 600; color: var(--hi-text-2); }
  .type { display: flex; align-items: center; gap: 6px; }
  .sub { margin-top: 2px; font-size: 12px; }
  .q { vertical-align: -2px; color: var(--hi-text-3); }
  .t-head { display: inline-flex; align-items: center; gap: 6px; i { width: 8px; height: 8px; border-radius: 50%; } }
  .cell-wrap { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 2px 0; border-radius: 6px;
    &.dirty { background: #fff7e6; box-shadow: 0 0 0 1px #ffd591 inset; }
    .sel { width: 150px; }
    .scope-line { display: flex; align-items: center; gap: 4px; font-size: 12px; }
    .eff { font-size: 12px; display: flex; align-items: center; gap: 4px; color: var(--hi-text-2);
      .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--hi-ok); }
      .alarms { color: var(--hi-p2); } }
    &.off .eff { color: var(--hi-text-3); .dot { background: var(--hi-unknown); } } }
  :deep(.has-off td:not(.cat-col)) { background: #fafbfc; }
}
.impact { display: flex; flex-direction: column; gap: 12px;
  .blk { padding: 10px 14px; border-radius: 8px; border: 1px solid var(--hi-border);
    &.stop { border-color: #ffccc7; background: #fff8f7; } &.start { border-color: #b7eb8f; background: #f6ffed; }
    .h { display: flex; align-items: center; gap: 6px; font-weight: 600; margin-bottom: 8px; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
    .warn { font-size: 13px; color: var(--hi-p1); } } }
</style>
