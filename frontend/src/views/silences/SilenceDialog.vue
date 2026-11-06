<template>
  <el-dialog :model-value="visible" :title="title" width="860px" top="6vh" append-to-body destroy-on-close class="silence-dialog" :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @close="emit('update:visible', false)">
    <el-form ref="formRef" :model="f" :disabled="saving" label-width="96px" :rules="rules">
      <el-form-item label="类型">
        <el-radio-group v-model="f.kind">
          <el-radio-button v-for="(k, code) in SILENCE_KIND" :key="code" :value="code">{{ k.label }}</el-radio-button>
        </el-radio-group>
        <span class="text-3 hint">{{ SILENCE_KIND[f.kind].desc }}</span>
      </el-form-item>
      <el-form-item label="名称" prop="name">
        <el-input v-model="f.name" maxlength="128" show-word-limit :placeholder="f.kind === 'MAINTENANCE' ? '例如：ecs-batch-01 内核升级（变更单 CHG-…）' : '例如：redis-prod-01 内存扩容处理中'" />
      </el-form-item>

      <div class="block">
        <div class="block-title">匹配范围 <span class="text-3">（条件之间为“并且”；同一条件内多选为“或者”；至少填写一项）</span></div>
        <el-form-item v-if="app.tenants.length > 1" label="租户">
          <el-select v-model="m.tenant_ids" multiple clearable placeholder="不限（全部可见租户）" class="w-full">
            <el-option v-for="t in app.tenants" :key="t.id" :value="t.id" :label="t.name"><TenantTag :id="t.id" /> <span class="opt">{{ t.name }}</span></el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="服务">
          <el-select v-model="m.services" multiple clearable filterable placeholder="不限" class="w-full">
            <el-option v-for="(n, code) in serviceOptions" :key="code" :value="code" :label="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="资源类型">
          <el-select v-model="m.resource_types" multiple clearable filterable placeholder="不限" class="w-full">
            <el-option v-for="t in typeOptions" :key="t.code" :value="t.code" :label="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="级别">
          <el-checkbox-group v-model="m.severities">
            <el-checkbox v-for="s in SEVERITIES" :key="s" :value="s"><SeverityTag :level="s" short /></el-checkbox>
          </el-checkbox-group>
          <span class="text-3 hint">不选 = 全部级别；计划内变更建议只屏蔽 P3 / P4，保留 P1 / P2 通知</span>
        </el-form-item>
        <el-form-item label="指定资源">
          <el-select v-model="m.resource_refs" multiple filterable remote clearable reserve-keyword :remote-method="searchResource" :loading="searching"
                     placeholder="输入资源名称 / IP 搜索，可多选" class="w-full">
            <el-option v-for="r in resourceOptions" :key="r.id" :value="r.id" :label="r.name">
              <span>{{ r.name }}</span><span class="text-3 opt">{{ typeName(r.resource_type) }}<template v-if="r.ip"> · {{ r.ip }}</template></span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="规则">
          <el-select v-model="m.rule_ids" multiple filterable clearable placeholder="不限" class="w-full">
            <el-option v-for="r in ruleOptions" :key="r.id" :value="r.id" :label="`${r.id} ${r.name}`" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称包含">
          <el-input v-model="m.name_contains" clearable placeholder="资源名称包含（例如 ecs-batch-）" />
        </el-form-item>
        <el-form-item v-if="m.fingerprints.length" label="指定告警">
          <el-tag closable @close="m.fingerprints = []">{{ m.fingerprints.length }} 条（来自告警详情的快速静默）</el-tag>
        </el-form-item>
      </div>

      <div class="block">
        <div class="block-title">时间</div>
        <el-form-item label="方式">
          <el-radio-group v-model="mode">
            <el-radio value="once">一次性</el-radio>
            <el-radio value="weekly">每周重复（例行变更窗口）</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="mode === 'once'">
          <el-form-item label="起止时间">
            <el-date-picker v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" format="YYYY-MM-DD HH:mm" :clearable="false" />
            <div class="presets">
              <el-button v-for="p in PRESETS" :key="p.label" size="small" @click="p.apply()">{{ p.label }}</el-button>
            </div>
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="每周">
            <el-checkbox-group v-model="weekly.weekdays">
              <el-checkbox-button v-for="d in WEEKDAYS" :key="d.value" :value="d.value">{{ d.label }}</el-checkbox-button>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="时段">
            <el-time-select v-model="weekly.start" start="00:00" step="00:30" end="23:30" placeholder="开始" style="width: 120px" />
            <span class="to">至</span>
            <el-time-select v-model="weekly.end" start="00:00" step="00:30" end="23:30" placeholder="结束" style="width: 120px" />
            <span v-if="crossMidnight" class="text-3 hint">跨午夜：结束时间为次日</span>
          </el-form-item>
          <el-form-item label="有效期">
            <el-date-picker v-model="valid" type="daterange" start-placeholder="开始日期" end-placeholder="结束日期" :clearable="false" />
            <span class="text-3 hint">最长 366 天</span>
          </el-form-item>
        </template>
        <div class="summary"><el-icon><Clock /></el-icon>{{ windowText }}</div>
      </div>

      <el-form-item label="原因" prop="reason">
        <el-input v-model="f.reason" type="textarea" :rows="2" maxlength="500" show-word-limit placeholder="变更单号 / 处理说明（会写入告警时间线和审计）" />
      </el-form-item>
    </el-form>

    <!-- 命中预览：保存前确认不会误伤核心告警 -->
    <div class="preview" :class="{ warn: danger }">
      <div class="p-head">
        <el-icon><View /></el-icon>
        <template v-if="!hasMatcher"><span class="text-3">填写匹配条件后，这里会显示当前会被命中的活动告警</span></template>
        <template v-else-if="pv">
          <span>当前会命中 <b class="num">{{ pv.total }}</b> 条活动告警</span>
          <span v-for="s in SEVERITIES" v-show="pv.by_severity[s]" :key="s" class="sev"><SeverityTag :level="s" short /> {{ pv.by_severity[s] }}</span>
          <el-tag v-if="pv.critical" type="danger" size="small" effect="plain">含核心资源 {{ pv.critical }} 条</el-tag>
          <span v-if="danger" class="danger-text">包含 P1 告警或核心资源，请确认范围是否过大</span>
        </template>
        <span v-else-if="pvError" class="danger-text">{{ pvError }}</span>
        <span v-else class="text-3">计算中…</span>
      </div>
      <el-table v-if="pv?.items.length" :data="pv.items.slice(0, 8)" size="small" max-height="220">
        <el-table-column width="70"><template #default="{ row }"><SeverityTag :level="row.severity" short /></template></el-table-column>
        <el-table-column v-if="app.multiTenant" width="90"><template #default="{ row }"><TenantTag :id="row.tenant_id" /></template></el-table-column>
        <el-table-column prop="resource_name" min-width="150" show-overflow-tooltip />
        <el-table-column prop="title" min-width="240" show-overflow-tooltip />
        <el-table-column width="80"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
      </el-table>
      <div v-if="pv && pv.total > 8" class="text-3 more">… 另有 {{ pv.total - 8 }} 条</div>
    </div>

    <template #footer>
      <span class="text-3 foot-tip">生效后命中的告警照常记录（状态“已抑制”），不发通知；结束后仍未恢复的告警会补发通知</span>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">{{ editing ? '保存修改' : f.kind === 'MAINTENANCE' ? '创建维护窗口' : '创建静默' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Clock, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { createSilence, getResource, listResources, listRules, previewSilence, updateSilence } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import type { Resource, Rule, Silence, SilenceIn, SilenceKind, SilenceMatchers, SilencePreview } from '@/types'
import { RESOURCE_TYPES, SERVICES, SEVERITIES, SILENCE_KIND, WEEKDAYS, typeName } from '@/utils/dict'

const props = defineProps<{ visible: boolean; silence?: Silence | null; copy?: boolean; preset?: Partial<SilenceIn> | null }>()
const emit = defineEmits<{ 'update:visible': [v: boolean]; saved: [s: Silence] }>()
const app = useAppStore()

const editing = computed(() => !!props.silence && !props.copy)
const title = computed(() => (editing.value ? `修改：${props.silence!.name}` : props.copy ? '复制为新的静默 / 维护窗口' : '新建静默 / 维护窗口'))
const formRef = ref<FormInstance>()
const f = reactive({ kind: 'MAINTENANCE' as SilenceKind, name: '', reason: '' })
const m = reactive({
  tenant_ids: [] as number[], services: [] as string[], resource_types: [] as string[], severities: [] as string[],
  rule_ids: [] as string[], resource_refs: [] as number[], fingerprints: [] as string[], name_contains: '',
})
const mode = ref<'once' | 'weekly'>('once')
const range = ref<[Date, Date]>([new Date(), dayjs().add(2, 'hour').toDate()])
const weekly = reactive({ weekdays: [6] as number[], start: '22:00', end: '02:00' })
const valid = ref<[Date, Date]>([new Date(), dayjs().add(90, 'day').toDate()])
const saving = ref(false)
const rules: FormRules = { name: [{ required: true, message: '请填写名称', trigger: 'blur' }] }

const serviceOptions = computed(() => Object.fromEntries(Object.entries(SERVICES).filter(([k]) => k !== 'ledger')))
const typeOptions = computed(() => Object.entries(RESOURCE_TYPES).map(([code, name]) => ({ code, name }))
  .filter((t) => !m.services.length || m.services.some((s) => t.code.startsWith(s === 'dataarts' ? 'dgc' : s === 'dms' ? 'kafka' : s === 'vpc' ? 'vpc' : s) || (s === 'vpc' && t.code === 'eip'))))
const ruleOptions = ref<Rule[]>([])
const resourceOptions = ref<Pick<Resource, 'id' | 'name' | 'resource_type' | 'ip'>[]>([])
const searchRequest = useLatestRequest()
const preloadRequest = useLatestRequest()
const searching = searchRequest.loading
let session = 0
let disposed = false

/** 快捷时间 */
const at = (d: dayjs.Dayjs, hm: string) => d.hour(Number(hm.slice(0, 2))).minute(Number(hm.slice(3))).second(0).millisecond(0)
const PRESETS = [
  { label: '1 小时', apply: () => (range.value = [new Date(), dayjs().add(1, 'hour').toDate()]) },
  { label: '2 小时', apply: () => (range.value = [new Date(), dayjs().add(2, 'hour').toDate()]) },
  { label: '4 小时', apply: () => (range.value = [new Date(), dayjs().add(4, 'hour').toDate()]) },
  { label: '今晚 22:00 – 次日 06:00', apply: () => { const s = at(dayjs(), '22:00'); range.value = [s.toDate(), s.add(8, 'hour').toDate()] } },
  { label: '明天 09:00 前', apply: () => (range.value = [new Date(), at(dayjs().add(1, 'day'), '09:00').toDate()]) },
]
const crossMidnight = computed(() => !!weekly.start && !!weekly.end && weekly.end <= weekly.start)
const windowText = computed(() => {
  if (mode.value === 'once') {
    const [s, e] = range.value
    const hours = dayjs(e).diff(dayjs(s), 'minute') / 60
    return `${dayjs(s).format('MM-DD HH:mm')} ~ ${dayjs(e).format('MM-DD HH:mm')}，共 ${hours >= 48 ? `${(hours / 24).toFixed(1)} 天` : `${hours.toFixed(1)} 小时`}（单次最长 31 天）`
  }
  const days = [...weekly.weekdays].sort().map((d) => WEEKDAYS[d - 1]?.label).join('、') || '（未选择星期）'
  return `每${days} ${weekly.start}–${crossMidnight.value ? '次日 ' : ''}${weekly.end}，${dayjs(valid.value[0]).format('YYYY-MM-DD')} 至 ${dayjs(valid.value[1]).format('YYYY-MM-DD')} 有效`
})

const matchers = computed<SilenceMatchers>(() => {
  const out: SilenceMatchers = {}
  for (const k of ['tenant_ids', 'services', 'resource_types', 'severities', 'rule_ids', 'resource_refs', 'fingerprints'] as const) {
    if (m[k].length) (out as Record<string, unknown>)[k] = [...m[k]]
  }
  if (m.name_contains.trim()) out.name_contains = m.name_contains.trim()
  return out
})
const hasMatcher = computed(() => Object.keys(matchers.value).length > 0)

// ---- 命中预览（防抖）----
const pv = ref<SilencePreview | null>(null)
const pvError = ref('')
const danger = computed(() => !!pv.value && (pv.value.by_severity.P1 > 0 || pv.value.critical > 0))
const previewRequest = useLatestRequest()
let timer: ReturnType<typeof setTimeout> | undefined
watch([matchers, () => props.visible], () => {
  clearTimeout(timer)
  previewRequest.invalidate()
  pv.value = null
  pvError.value = ''
  if (!hasMatcher.value || !props.visible) return
  const snapshot = structuredClone(matchers.value)
  timer = setTimeout(() => {
    void previewRequest.run(() => previewSilence(snapshot), value => { pv.value = value })
      .catch(error => { pvError.value = error instanceof Error ? error.message : '影响预估失败' })
  }, 350)
}, { deep: true, flush: 'sync' })
onScopeDispose(() => { disposed = true; session++; clearTimeout(timer) })

async function searchResource(q: string) {
  searchRequest.invalidate()
  if (!q.trim()) return
  const keyword = q.trim()
  return searchRequest.run(() => listResources({ keyword, page: 1, page_size: 20 }), page => {
    const chosen = resourceOptions.value.filter(resource => m.resource_refs.includes(resource.id))
    resourceOptions.value = [...chosen, ...page.items.filter(resource => !m.resource_refs.includes(resource.id))]
  }).catch(() => undefined)
}

function reset() {
  searchRequest.invalidate()
  preloadRequest.invalidate()
  resourceOptions.value = []
  const s = props.silence
  const p = props.preset
  f.kind = s?.kind ?? (p?.kind as SilenceKind) ?? 'MAINTENANCE'
  f.name = s ? (props.copy ? `${s.name}（副本）` : s.name) : p?.name ?? ''
  f.reason = s?.reason ?? p?.reason ?? ''
  const src: SilenceMatchers = s?.matchers ?? p?.matchers ?? (app.currentTenant ? { tenant_ids: [app.currentTenant.id] } : {})
  m.tenant_ids = [...(src.tenant_ids ?? [])]; m.services = [...(src.services ?? [])]; m.resource_types = [...(src.resource_types ?? [])]
  m.severities = [...(src.severities ?? [])]; m.rule_ids = [...(src.rule_ids ?? [])]; m.resource_refs = [...(src.resource_refs ?? [])]
  m.fingerprints = [...(src.fingerprints ?? [])]; m.name_contains = src.name_contains ?? ''
  const rec = s?.recurrence
  if (rec?.weekdays?.length) {
    mode.value = 'weekly'
    Object.assign(weekly, { weekdays: [...rec.weekdays], start: rec.start ?? '22:00', end: rec.end ?? '02:00' })
    valid.value = [new Date(s!.starts_at), new Date(s!.ends_at)]
  } else {
    mode.value = 'once'
    range.value = s && !props.copy ? [new Date(s.starts_at), new Date(s.ends_at)] : [new Date(), dayjs().add(2, 'hour').toDate()]
  }
  pv.value = null
  if (m.resource_refs.length) {
    void preloadRequest.run(() => Promise.all(m.resource_refs.slice(0, 20).map(id => getResource(id).then(detail => detail.resource).catch(() => null))),
      rows => { resourceOptions.value = rows.filter((resource): resource is Resource => !!resource) }).catch(() => undefined)
  }
}
watch(() => props.visible, async (v) => {
  session++
  if (!v) {
    clearTimeout(timer)
    previewRequest.invalidate()
    searchRequest.invalidate()
    preloadRequest.invalidate()
    return
  }
  reset()
  if (!ruleOptions.value.length) {
    const current = session
    try { const rows = await listRules(); if (!disposed && current === session) ruleOptions.value = rows } catch { /* 请求层提示。 */ }
  }
}, { immediate: true })

async function submit() {
  if (saving.value || !props.visible || disposed) return
  const current = session
  const id = editing.value ? props.silence!.id : null
  const tenantId = app.tenantId
  saving.value = true
  clearTimeout(timer)
  previewRequest.invalidate()
  try {
    if (!await formRef.value?.validate().catch(() => false)) return
    if (!hasMatcher.value) return ElMessage.warning('至少需要一个匹配条件')
    let body: SilenceIn
    if (mode.value === 'once') {
      const [start, end] = range.value
      if (!dayjs(end).isAfter(dayjs(start))) return ElMessage.warning('结束时间需要晚于开始时间')
      body = { name: f.name.trim(), kind: f.kind, matchers: matchers.value, reason: f.reason,
        starts_at: dayjs(start).format(), ends_at: dayjs(end).format(), recurrence: null }
    } else {
      if (!weekly.weekdays.length || !weekly.start || !weekly.end) return ElMessage.warning('请选择星期和时段')
      body = { name: f.name.trim(), kind: f.kind, matchers: matchers.value, reason: f.reason,
        starts_at: dayjs(valid.value[0]).startOf('day').format(), ends_at: dayjs(valid.value[1]).endOf('day').format(),
        recurrence: { weekdays: [...weekly.weekdays].sort(), start: weekly.start, end: weekly.end } }
    }
    const impact = await previewSilence(body.matchers)
    if (disposed || session !== current || !props.visible || tenantId !== app.tenantId) return
    pv.value = impact
    if (impact.by_severity.P1 > 0 || impact.critical > 0) {
      await ElMessageBox.confirm(`当前会命中 ${impact.total} 条活动告警，其中包含 P1 告警或核心资源。确认继续？`, '范围确认',
        { type: 'warning', confirmButtonText: id ? '确认保存' : '确认创建', cancelButtonText: '再检查一下' })
    }
    if (disposed || session !== current || !props.visible || tenantId !== app.tenantId) return
    const res = id == null ? await createSilence(body) : await updateSilence(id, body)
    if (disposed || session !== current) return
    const extra = 'released' in res && res.released ? `，${res.released} 条告警已解除抑制并补发通知` : ''
    ElMessage.success(`${id == null ? '已创建' : '已保存'}${res.suppressed ? `，立即抑制 ${res.suppressed} 条告警` : ''}${extra}`)
    emit('saved', res)
    emit('update:visible', false)
  } catch (error) {
    if (!disposed && session === current && error !== 'cancel' && error !== 'close') {
      pvError.value = error instanceof Error ? error.message : '保存失败'
    }
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.hint { margin-left: 12px; }
.opt { margin-left: 8px; }
.block { margin: 4px 0 14px; padding: 12px 16px 2px; border-radius: var(--hi-radius-sm); background: var(--hi-bg-soft); border: 1px solid var(--hi-border-light);
  .block-title { margin-bottom: 10px; font-weight: 600; font-size: 13px; } }
.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-left: 12px; .el-button + .el-button { margin-left: 0; } }
.to { margin: 0 8px; color: var(--hi-text-3); }
.summary { display: flex; align-items: center; gap: 6px; margin: -4px 0 12px 96px; font-size: 13px; color: var(--el-color-primary); }
.preview { padding: 10px 14px; border-radius: var(--hi-radius-sm); border: 1px dashed var(--hi-border);
  &.warn { border-color: var(--hi-p2); background: #fff8f0; }
  .p-head { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 13px; margin-bottom: 6px; b { font-size: 16px; } }
  .sev { display: inline-flex; align-items: center; gap: 4px; }
  .danger-text { color: var(--hi-p1); font-size: 12px; }
  .more { margin-top: 6px; } }
.foot-tip { float: left; line-height: 32px; }
</style>
