<template>
  <el-dialog :model-value="modelValue" width="min(1200px, 96vw)" top="5vh" class="rule-editor" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving"
             @update:model-value="(v: boolean) => { if (!saving) emit('update:modelValue', v) }" @open="init">
    <template #header>
      <div class="dlg-title">
        <b>{{ !rule ? '新建自定义规则' : canWrite ? '编辑规则' : '查看规则' }}</b><span v-if="rule" class="mono text-3">{{ rule.id }} · v{{ rule?.version }}</span>
        <el-tag size="small" effect="plain">{{ RESOURCE_TYPES[form.resource_type] ?? '请选择目标' }}</el-tag>
        <el-tag size="small" :type="form.kind === 'threshold' ? 'primary' : 'info'">{{ form.kind === 'threshold' ? '阈值规则' : '状态规则' }}</el-tag>
      </div>
    </template>

    <div class="body">
      <!-- 左：规则定义 -->
      <el-form ref="formRef" :model="form" label-position="top" class="left" :disabled="!canWrite || saving">
        <template v-if="!rule">
          <el-form-item label="规则 ID" required><el-input v-model="form.id" maxlength="64" show-word-limit placeholder="如 CUSTOM_ECS_CPU" /></el-form-item>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8"><el-form-item label="目标服务" required>
              <el-select v-model="form.service" class="w-full" placeholder="选择服务" @change="serviceChanged">
                <el-option v-for="service in services" :key="service" :value="service" :label="service.toUpperCase()" />
              </el-select>
            </el-form-item></el-col>
            <el-col :xs="24" :sm="8"><el-form-item label="资源类型" required>
              <el-select v-model="form.resource_type" class="w-full" :disabled="!form.service" @change="resourceChanged">
                <el-option v-for="target in resourceTypes" :key="target.resource_type" :value="target.resource_type" :label="target.name" />
              </el-select>
            </el-form-item></el-col>
            <el-col :xs="24" :sm="8"><el-form-item label="检查项" required>
              <el-select v-model="form.check_item" class="w-full" :disabled="!form.resource_type" @change="checkChanged">
                <el-option v-for="target in checkItems" :key="target.check_item" :value="target.check_item"
                  :label="target.collection_available === false ? `${target.check_item}（未采集）` : target.check_item"
                  :disabled="target.collection_available === false" />
              </el-select>
            </el-form-item></el-col>
          </el-row>
        </template>
        <el-alert v-if="selectedTarget" :title="`${selectedTarget.service} / ${selectedTarget.resource_type} / ${selectedTarget.check_item}`" type="info" :closable="false" class="mb-12" />
        <el-alert v-if="selectedTarget?.collection_available === false" title="此检查项未采集，历史规则仅保留查看或停用。" type="warning" :closable="false" class="mb-12" />
        <el-row :gutter="16">
          <el-col :xs="24" :sm="16"><el-form-item label="规则名称" required><el-input v-model="form.name" maxlength="128" show-word-limit /></el-form-item></el-col>
          <el-col :xs="24" :sm="8"><el-form-item label="启用"><el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" inline-prompt /></el-form-item></el-col>
        </el-row>

        <el-form-item label="规则类型">
          <el-radio-group v-model="form.kind" @change="kindChanged"><el-radio-button value="state">状态规则</el-radio-button><el-radio-button value="threshold">阈值规则</el-radio-button></el-radio-group>
        </el-form-item>
        <template v-if="form.kind === 'threshold'">
          <el-row :gutter="16">
            <el-col :xs="24" :sm="12"><el-form-item label="指标表达式"><el-input v-model="form.expr.value" maxlength="500" class="mono-input" placeholder="如 memory_usage" /></el-form-item></el-col>
            <el-col :xs="24" :sm="4"><el-form-item label="单位"><el-input v-model="form.expr.unit" maxlength="32" /></el-form-item></el-col>
            <el-col :xs="24" :sm="8"><el-form-item label="恢复阈值（低于即恢复）"><el-input-number v-model="form.recover.below" controls-position="right" class="w-full" /></el-form-item></el-col>
          </el-row>
          <el-form-item v-if="form.recover.when" label="自定义恢复条件（清空后按恢复阈值）"><el-input v-model="form.recover.when" maxlength="500" class="mono-input" /></el-form-item>
          <el-form-item label="告警阈值（优先匹配最高级别）">
            <div class="levels">
              <div v-for="(l, i) in form.expr.levels" :key="i" class="level" :style="{ '--c': SEVERITY[l.severity].color }">
                <el-select v-model="l.severity" class="sev"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" :disabled="form.expr.levels.some((level, index) => index !== i && level.severity === s)" /></el-select>
                <span class="mono text-2">{{ form.expr.value || 'value' }}</span>
                <el-select v-model="l.op" class="op"><el-option v-for="o in OPS" :key="o" :value="o" :label="o" /></el-select>
                <el-input-number v-model="l.threshold" :step="1" controls-position="right" />
                <span class="text-3">{{ form.expr.unit }}</span>
                <el-button text type="danger" :icon="Delete" :disabled="!canWrite || saving || form.expr.levels.length <= 1" aria-label="删除阈值级别" @click="form.expr.levels.splice(i, 1)" />
              </div>
              <el-button text type="primary" :icon="Plus" :disabled="!canWrite || saving || form.expr.levels.length >= 4" @click="addLevel">添加级别</el-button>
            </div>
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="触发条件">
            <el-input v-model="form.expr.when" maxlength="500" show-word-limit type="textarea" :rows="3" class="mono-input" />
            <div class="fields">可用字段：<button v-for="f in fields" :key="f" type="button" class="field" :disabled="!canWrite || saving" @click="insertField(f)">{{ f }}</button></div>
          </el-form-item>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8"><el-form-item label="告警级别"><el-select v-model="form.expr.severity"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" /></el-select></el-form-item></el-col>
            <el-col :xs="24" :sm="8"><el-form-item label="核心资源级别"><el-select v-model="form.expr.critical_severity" clearable placeholder="同左"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" /></el-select></el-form-item></el-col>
            <el-col :xs="24" :sm="8"><el-form-item label="展示值字段"><el-input v-model="form.expr.value_field" maxlength="64" class="mono-input" placeholder="如 status" /></el-form-item></el-col>
          </el-row>
          <el-form-item label="恢复条件"><el-input v-model="form.recover.when" maxlength="500" class="mono-input" placeholder="留空表示触发条件不再满足即恢复" /></el-form-item>
        </template>

        <el-form-item label="告警标题模板"><el-input v-model="form.expr.title" maxlength="500" placeholder="可用 {value}、{threshold} 或当前检查项字段" /></el-form-item>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12"><el-form-item label="连续命中次数"><el-input-number v-model="form.for_count" :min="1" class="w-full" /><div class="text-3">连续 N 个巡检周期命中才产生告警</div></el-form-item></el-col>
          <el-col :xs="24" :sm="12"><el-form-item label="重复通知间隔">
            <el-select v-model="form.repeat_interval_sec" class="w-full"><el-option v-for="o in REPEATS" :key="o.value" :value="o.value" :label="o.label" /></el-select>
            <div class="text-3">未认领且未恢复时按间隔重复提醒</div></el-form-item></el-col>
        </el-row>
        <el-form-item label="处置手册"><el-input v-model="form.runbook" maxlength="10000" show-word-limit type="textarea" :rows="3" placeholder="排查步骤、处理方式与升级联系人" /></el-form-item>
      </el-form>

      <!-- 右：试运行 -->
      <div class="right" :class="{ focus: focusDryRun }">
        <div class="dr-title"><el-icon><VideoPlay /></el-icon>规则试运行<span class="text-3">不保存、不通知</span></div>
        <div v-if="selectedTarget" class="metadata">
          <div class="text-3 mb-8">采集任务：{{ selectedTarget.collection_tasks.join('、') }}</div>
          <el-collapse>
            <el-collapse-item title="可用字段与说明" name="facts">
              <router-link :to="{ path: '/guides', query: { type: selectedTarget?.resource_type } }" target="_blank">打开资源与指标指南</router-link>
              <el-table :data="selectedTarget.facts" size="small" max-height="240">
                <el-table-column prop="key" label="字段" width="115" />
                <el-table-column prop="data_type" label="类型" width="68" />
                <el-table-column label="说明" min-width="120"><template #default="{ row }">{{ row.meaning }}{{ row.unit ? `（${row.unit}）` : '' }}<div class="text-3">{{ row.limitations.join('；') }}</div></template></el-table-column>
              </el-table>
              <div v-for="limit in selectedTarget.limitations" :key="limit" class="text-3">{{ limit }}</div>
            </el-collapse-item>
          </el-collapse>
          <div class="text-3 mt-12">表达式示例</div>
          <div class="examples">
            <el-button v-for="(example, i) in selectedTarget.expression_examples" :key="i" size="small" :disabled="!canWrite || saving" @click="applyExample(example)">{{ example.expr.when ?? example.expr.value }}</el-button>
          </div>
        </div>
        <div class="text-3 mb-8">支持比较、逻辑、算术及 abs、min、max、len、round。仅可引用当前检查项字段，缺失数据时跳过判定。</div>
        <div class="text-3 mb-8">试运行数据（JSON，仅为示例）</div>
        <el-input v-model="factsText" type="textarea" :rows="12" class="mono-input" spellcheck="false" />
        <div class="dr-actions">
          <el-button @click="factsText = sampleText">重置示例</el-button>
          <el-button type="primary" :icon="VideoPlay" :loading="running" :disabled="saving" @click="dryRun">试运行</el-button>
        </div>
        <transition name="fade">
          <div v-if="result" class="result" :class="result.matched ? 'hit' : result.recovered ? 'ok' : 'miss'"
               :style="result.severity ? { '--c': SEVERITY[result.severity].color, '--bg': SEVERITY[result.severity].bg } : {}">
            <div class="r-head">
              <template v-if="result.matched"><SeverityTag :level="result.severity!" solid />条件命中</template>
              <template v-else-if="result.recovered"><el-icon><CircleCheckFilled /></el-icon>未命中，满足恢复条件</template>
              <template v-else><el-icon><InfoFilled /></el-icon>未命中</template>
            </div>
            <div class="r-msg">{{ result.message }}</div>
            <div v-if="result.matched && form.for_count > 1" class="text-3">连续 {{ form.for_count }} 次命中后告警</div>
          </div>
        </transition>
        <el-alert v-if="error || saveError" :title="saveError || error" type="error" show-icon :closable="false" class="mt-12" />
      </div>
    </div>

    <template #footer>
      <span v-if="canWrite" class="text-3 foot-tip">启用后在下一巡检周期生效</span>
      <el-button :disabled="saving" @click="emit('update:modelValue', false)">{{ canWrite ? '取消' : '关闭' }}</el-button>
      <el-button v-if="canWrite" type="primary" :loading="saving" @click="save">保存规则</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { CircleCheckFilled, Delete, InfoFilled, Plus, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import { post } from '@/api/request'
import { useRuleDryRun } from './useRuleDryRun'
import { nextRuleLevel } from './rule-form'
import { createRule, saveRule } from '@/api/rules'
import type { RuleDryRunResult, RuleExample, RuleTarget, RuleWrite } from '@/types/rules'
import SeverityTag from '@/components/SeverityTag.vue'
import { useUserStore } from '@/stores/user'
import type { Rule, RuleLevel } from '@/types'
import { RESOURCE_TYPES, SAMPLE_FACTS, SEVERITIES, SEVERITY } from '@/utils/dict'

const props = defineProps<{ modelValue: boolean; rule: Rule | null; focusDryRun?: boolean; targets: RuleTarget[]; initialResourceType?: string }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean]; saved: [] }>()

const OPS = ['>=', '>', '<=', '<']
const REPEATS = [{ value: 0, label: '不重复' }, { value: 1800, label: '每 30 分钟' }, { value: 3600, label: '每 1 小时' },
  { value: 7200, label: '每 2 小时' }, { value: 14400, label: '每 4 小时' }]
const canWrite = computed(() => useUserStore().can('rule:edit')) // 规则修改需要 rule:edit 权限

type Expr = { value?: string; unit?: string; title?: string; levels: RuleLevel[]; when?: string; severity?: string; critical_severity?: string; value_field?: string; threshold_text?: string }
const form = reactive({
  id: '', service: '', resource_type: '', check_item: '', name: '', kind: 'state' as Rule['kind'], enabled: true, for_count: 1, repeat_interval_sec: 0, runbook: '',
  expr: { levels: [] } as Expr, recover: {} as { below?: number; when?: string; for_count?: number }, scope: {} as Record<string, unknown>,
})
const factsText = ref('')
const { result, error, running, run: runDryRun, clear: clearDryRun } = useRuleDryRun((body, signal) =>
  post<RuleDryRunResult>('/rules/dry-run', body, { signal, silent: true }))
const saveError = ref('')
let disposed = false
const saving = ref(false)

const services = computed(() => [...new Set(props.targets.map(target => target.service))])
const resourceTypes = computed(() => [...new Map(props.targets.filter(target => target.service === form.service)
  .map(target => [target.resource_type, target])).values()])
const checkItems = computed(() => props.targets.filter(target => target.resource_type === form.resource_type))
const selectedTarget = computed(() => props.targets.find(target => target.resource_type === form.resource_type && target.check_item === form.check_item))
const sampleText = computed(() => {
  const known = SAMPLE_FACTS[form.resource_type] ?? {}
  const sample = Object.fromEntries((selectedTarget.value?.facts ?? []).map(field => [field.key,
    known[field.key] ?? (field.data_type === 'boolean' ? false : ['number', 'integer'].includes(field.data_type) ? 0 : field.data_type === 'array' ? [] : field.data_type === 'object' ? {} : '')]))
  return JSON.stringify(sample, null, 2)
})
const fields = computed(() => selectedTarget.value?.facts.map(field => field.key) ?? [])

function init() {
  const r = props.rule
  Object.assign(form, {
    id: r?.id ?? '', service: props.targets.find(target => target.resource_type === r?.resource_type)?.service ?? '',
    resource_type: r?.resource_type ?? '', check_item: r?.check_item ?? '', name: r?.name ?? '',
    kind: r?.kind ?? 'state', enabled: r?.enabled ?? true, for_count: r?.for_count ?? 1,
    repeat_interval_sec: r?.repeat_interval_sec ?? 0, runbook: r?.runbook ?? '', scope: { ...(r?.scope ?? {}) },
    expr: { levels: [], ...(r ? JSON.parse(JSON.stringify(r.expr)) : { when: '', severity: 'P2' }) },
    recover: { ...(r?.recover ?? {}) },
  })
  if (!r && props.initialResourceType) {
    const target = props.targets.find(item => item.resource_type === props.initialResourceType)
    if (target) { form.service = target.service; form.resource_type = target.resource_type }
  }
  factsText.value = sampleText.value
  clearDryRun()
  saveError.value = ''
  if (props.focusDryRun) void dryRun()
}
function serviceChanged() { form.resource_type = ''; resourceChanged() }
function resourceChanged() { form.check_item = ''; checkChanged() }
function checkChanged() {
  result.value = null
  error.value = ''
  const example = selectedTarget.value?.expression_examples[0]
  if (example) applyExample(example)
  else kindChanged()
  factsText.value = sampleText.value
}
function kindChanged() {
  form.expr = form.kind === 'threshold' ? { value: '', unit: '', levels: [{ severity: 'P2', op: '>=', threshold: 80 }] } : { when: '', severity: 'P2', levels: [] }
  form.recover = {}
  result.value = null
  error.value = ''
}
function applyExample(example: RuleExample) {
  if (!canWrite.value || saving.value) return
  form.kind = example.kind
  form.expr = { levels: [], ...JSON.parse(JSON.stringify(example.expr)) }
  form.recover = { ...example.recover }
  result.value = null
}
function addLevel() {
  if (!canWrite.value || saving.value) return
  const level = nextRuleLevel(form.expr.levels)
  if (level) form.expr.levels.push(level)
}
function insertField(field: string) {
  if (!canWrite.value || saving.value) return
  form.expr.when = `${form.expr.when ?? ''}${form.expr.when ? ' and ' : ''}${field} `
}
watch([form, factsText], () => { clearDryRun(); saveError.value = '' }, { deep: true, flush: 'sync' })
watch(() => props.modelValue, visible => { if (!visible) clearDryRun() }, { flush: 'sync' })
onScopeDispose(() => { disposed = true })

/** 只提交当前类型需要的字段，避免把编辑中的脏字段写入规则 */
function payload() {
  const e = form.expr
  const expr = form.kind === 'threshold'
    ? { value: e.value, unit: e.unit, title: e.title, levels: e.levels }
    : { when: e.when, severity: e.severity, title: e.title, ...(e.critical_severity ? { critical_severity: e.critical_severity } : {}), ...(e.value_field ? { value_field: e.value_field } : {}), ...(e.threshold_text ? { threshold_text: e.threshold_text } : {}) }
  const recover = form.recover.when ? { when: form.recover.when }
    : (form.kind === 'threshold' && form.recover.below !== undefined && form.recover.below !== null ? { below: form.recover.below } : {})
  return { expr, recover: { ...recover, ...(form.recover.for_count ? { for_count: form.recover.for_count } : {}) } }
}
async function dryRun() {
  if (!props.modelValue || saving.value) return
  clearDryRun()
  if (!selectedTarget.value) { error.value = '请选择资源类型与检查项'; return }
  let facts: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(factsText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid facts')
    facts = parsed as Record<string, unknown>
  } catch { error.value = '事实数据须为 JSON 对象'; return }
  await runDryRun({ kind: form.kind, ...payload(), facts, resource_type: form.resource_type, check_item: form.check_item })
}
async function save() {
  if (!canWrite.value || saving.value) return
  if (!form.name.trim()) { ElMessage.warning('请填写规则名称'); return }
  if (!props.rule && !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(form.id)) { ElMessage.warning('规则 ID 须以英文字母开头，最多 64 位，仅包含字母、数字、_、-'); return }
  if (!selectedTarget.value) { ElMessage.warning('请选择服务、资源类型与检查项'); return }
  clearDryRun()
  saving.value = true
  saveError.value = ''
  try {
    const body: RuleWrite = {
      name: form.name.trim(), kind: form.kind, scope: form.scope, ...payload(), for_count: form.for_count,
      repeat_interval_sec: form.repeat_interval_sec || null, runbook: form.runbook || null, enabled: form.enabled,
    }
    if (props.rule) await saveRule(props.rule.id, { ...body, version: props.rule.version })
    else await createRule({ ...body, id: form.id, resource_type: form.resource_type, check_item: form.check_item })
    if (disposed) return
    ElMessage.success('规则已保存')
    emit('saved')
    emit('update:modelValue', false)
  } catch (cause) {
    if (!disposed) saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.dlg-title { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; b { font-size: 16px; } }
.dlg-title .mono { min-width: 0; overflow-wrap: anywhere; }
.body { display: grid; grid-template-columns: minmax(0, 1fr) 380px; gap: 20px; }
.left { min-width: 0; }
.metadata { margin-bottom: 14px; }
.examples { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 10px; }
.examples .el-button { overflow-wrap: anywhere; max-width: 100%; margin: 0; height: auto; white-space: normal; text-align: left; }
@media (max-width: 900px) { .body { grid-template-columns: minmax(0, 1fr); } }
.w-full { width: 100%; }
.mono-input :deep(input), .mono-input :deep(textarea) { font-family: var(--hi-font-mono); font-size: 13px; }
.levels { display: flex; flex-direction: column; gap: 8px; width: 100%;
  .level { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; background: var(--hi-bg-soft); border-left: 3px solid var(--c);
    .sev { width: 120px; } .op { width: 76px; } } }
.fields { margin-top: 6px; font-size: 12px; color: var(--hi-text-3); line-height: 2;
  .field { border: 0; font-size: inherit; margin-right: 6px; padding: 2px 6px; border-radius: 4px; cursor: pointer; font-family: var(--hi-font-mono); color: var(--el-color-primary); background: var(--el-color-primary-light-9);
    &:hover { background: var(--el-color-primary-light-8); }
    &:disabled { cursor: default; color: var(--hi-text-3); } } }
.right {
  min-width: 0; padding: 14px 16px; border-radius: var(--hi-radius); background: var(--hi-bg-soft); border: 1px solid var(--hi-border); align-self: start;
  &.focus { border-color: var(--el-color-primary-light-5); box-shadow: 0 0 0 3px var(--el-color-primary-light-9); }
  .dr-title { display: flex; align-items: center; gap: 6px; font-weight: 600; margin-bottom: 6px; .text-3 { margin-left: auto; font-weight: 400; } }
  .mb-8 { margin-bottom: 8px; }
  .dr-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
}
.result { margin-top: 12px; padding: 12px 14px; border-radius: 6px; background: #fff; border: 1px solid var(--hi-border);
  .r-head { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .r-msg { margin: 6px 0 2px; font-size: 13px; color: var(--hi-text-2); }
  &.hit { border-color: var(--c); background: var(--bg); .r-head { color: var(--c); } }
  &.ok .r-head { color: var(--hi-ok); }
  &.miss .r-head { color: var(--hi-text-2); } }
@media (max-width: 600px) { .levels .level { gap: 6px; } .levels .level .mono { flex-basis: 100%; overflow-wrap: anywhere; } .foot-tip { display: block; margin-bottom: 8px; text-align: left; } }
.mt-12 { margin-top: 12px; }
.foot-tip { margin-right: auto; line-height: 32px; }
</style>
