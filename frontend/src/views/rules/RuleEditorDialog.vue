<template>
  <el-dialog :model-value="modelValue" width="1080px" top="5vh" class="rule-editor" :close-on-click-modal="false"
             @update:model-value="(v: boolean) => emit('update:modelValue', v)" @open="init">
    <template #header>
      <div class="dlg-title">
        <b>{{ canWrite ? '编辑规则' : '查看规则' }}</b><span class="mono text-3">{{ rule?.id }} · v{{ rule?.version }}</span>
        <el-tag size="small" effect="plain">{{ RESOURCE_TYPES[rule?.resource_type ?? ''] }}</el-tag>
        <el-tag size="small" :type="form.kind === 'threshold' ? 'primary' : 'info'">{{ form.kind === 'threshold' ? '阈值规则' : '状态规则' }}</el-tag>
      </div>
    </template>

    <div v-if="rule" class="body">
      <!-- 左：规则定义 -->
      <el-form ref="formRef" :model="form" label-position="top" class="left" :disabled="!canWrite">
        <el-row :gutter="16">
          <el-col :span="16"><el-form-item label="规则名称" required><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="启用"><el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" inline-prompt /></el-form-item></el-col>
        </el-row>

        <template v-if="form.kind === 'threshold'">
          <el-row :gutter="16">
            <el-col :span="12"><el-form-item label="指标表达式（value）"><el-input v-model="form.expr.value" class="mono-input" placeholder="如 memory_usage" /></el-form-item></el-col>
            <el-col :span="4"><el-form-item label="单位"><el-input v-model="form.expr.unit" /></el-form-item></el-col>
            <el-col :span="8"><el-form-item label="恢复阈值（低于即恢复）"><el-input-number v-model="form.recover.below" :min="0" controls-position="right" class="w-full" /></el-form-item></el-col>
          </el-row>
          <el-form-item label="多级阈值（自上而下匹配，命中第一个即定级）">
            <div class="levels">
              <div v-for="(l, i) in form.expr.levels" :key="i" class="level" :style="{ '--c': SEVERITY[l.severity].color }">
                <el-select v-model="l.severity" class="sev"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" /></el-select>
                <span class="mono text-2">{{ form.expr.value || 'value' }}</span>
                <el-select v-model="l.op" class="op"><el-option v-for="o in OPS" :key="o" :value="o" :label="o" /></el-select>
                <el-input-number v-model="l.threshold" :step="1" controls-position="right" />
                <span class="text-3">{{ form.expr.unit }}</span>
                <el-button text type="danger" :icon="Delete" :disabled="form.expr.levels.length <= 1" @click="form.expr.levels.splice(i, 1)" />
              </div>
              <el-button text type="primary" :icon="Plus" :disabled="form.expr.levels.length >= 4" @click="addLevel">添加级别</el-button>
            </div>
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="触发条件（when）">
            <el-input v-model="form.expr.when" type="textarea" :rows="3" class="mono-input" />
            <div class="fields">可用字段：<span v-for="f in fields" :key="f" class="field" @click="insertField(f)">{{ f }}</span></div>
          </el-form-item>
          <el-row :gutter="16">
            <el-col :span="8"><el-form-item label="告警级别"><el-select v-model="form.expr.severity"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" /></el-select></el-form-item></el-col>
            <el-col :span="8"><el-form-item label="核心资源级别（可选）"><el-select v-model="form.expr.critical_severity" clearable placeholder="同左"><el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="`${s} ${SEVERITY[s].label}`" /></el-select></el-form-item></el-col>
            <el-col :span="8"><el-form-item label="展示值字段（可选）"><el-input v-model="form.expr.value_field" class="mono-input" placeholder="如 status" /></el-form-item></el-col>
          </el-row>
          <el-form-item label="恢复条件（recover.when）"><el-input v-model="form.recover.when" class="mono-input" placeholder="留空表示触发条件不再满足即恢复" /></el-form-item>
        </template>

        <el-form-item label="告警标题模板"><el-input v-model="form.expr.title" placeholder="支持 {value} {threshold} 及任意字段占位符" /></el-form-item>
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item label="持续次数（防抖）"><el-input-number v-model="form.for_count" :min="1" :max="10" class="w-full" /><div class="text-3">连续 N 个巡检周期命中才产生告警</div></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="重复通知间隔">
            <el-select v-model="form.repeat_interval_sec" class="w-full"><el-option v-for="o in REPEATS" :key="o.value" :value="o.value" :label="o.label" /></el-select>
            <div class="text-3">未认领且未恢复时按间隔重复提醒</div></el-form-item></el-col>
        </el-row>
        <el-form-item label="处置手册（Runbook）"><el-input v-model="form.runbook" type="textarea" :rows="3" placeholder="告警邮件与详情页会展示，建议写明排查步骤与升级路径" /></el-form-item>
      </el-form>

      <!-- 右：试运行 -->
      <div class="right" :class="{ focus: focusDryRun }">
        <div class="dr-title"><el-icon><VideoPlay /></el-icon>规则试运行<span class="text-3">不落库、不发通知</span></div>
        <div class="text-3 mb-8">输入一组资源事实（facts，JSON），验证当前编辑中的规则是否命中：</div>
        <el-input v-model="factsText" type="textarea" :rows="12" class="mono-input" spellcheck="false" />
        <div class="dr-actions">
          <el-button @click="factsText = sampleText">重置示例</el-button>
          <el-button type="primary" :icon="VideoPlay" :loading="running" @click="dryRun">试运行</el-button>
        </div>
        <transition name="fade">
          <div v-if="result" class="result" :class="result.matched ? 'hit' : result.recovered ? 'ok' : 'miss'"
               :style="result.severity ? { '--c': SEVERITY[result.severity].color, '--bg': SEVERITY[result.severity].bg } : {}">
            <div class="r-head">
              <template v-if="result.matched"><SeverityTag :level="result.severity!" solid />命中，将产生告警</template>
              <template v-else-if="result.recovered"><el-icon><CircleCheckFilled /></el-icon>未命中，且满足恢复条件</template>
              <template v-else><el-icon><InfoFilled /></el-icon>未命中</template>
            </div>
            <div class="r-msg">{{ result.message }}</div>
            <div v-if="result.matched && form.for_count > 1" class="text-3">注：实际运行需连续 {{ form.for_count }} 次命中才会告警</div>
          </div>
        </transition>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mt-12" />
      </div>
    </div>

    <template #footer>
      <span class="text-3 foot-tip">保存后版本号 +1，下一个巡检周期生效；修改记录写入审计日志</span>
      <el-button @click="emit('update:modelValue', false)">{{ canWrite ? '取消' : '关闭' }}</el-button>
      <el-button v-if="canWrite" type="primary" :loading="saving" @click="save">保存规则</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { CircleCheckFilled, Delete, InfoFilled, Plus, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { ApiError } from '@/api/request'
import { dryRunRule, saveRule } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import { useUserStore } from '@/stores/user'
import type { DryRunResult, Rule, RuleLevel } from '@/types'
import { RESOURCE_TYPES, SAMPLE_FACTS, SEVERITIES, SEVERITY } from '@/utils/dict'

const props = defineProps<{ modelValue: boolean; rule: Rule | null; focusDryRun?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean]; saved: [] }>()

const OPS = ['>=', '>', '<=', '<', '==']
const REPEATS = [{ value: 0, label: '不重复' }, { value: 1800, label: '每 30 分钟' }, { value: 3600, label: '每 1 小时' },
  { value: 7200, label: '每 2 小时' }, { value: 14400, label: '每 4 小时' }]
const canWrite = computed(() => useUserStore().can('rule:edit')) // 规则修改需要 rule:edit 权限

type Expr = { value?: string; unit?: string; title?: string; levels: RuleLevel[]; when?: string; severity?: string; critical_severity?: string; value_field?: string }
const form = reactive({
  name: '', kind: 'threshold' as Rule['kind'], enabled: true, for_count: 1, repeat_interval_sec: 0, runbook: '',
  expr: { levels: [] } as Expr, recover: {} as { below?: number; when?: string }, scope: {} as Record<string, unknown>,
})
const factsText = ref('')
const result = ref<DryRunResult | null>(null)
const error = ref('')
const running = ref(false)
const saving = ref(false)

const sampleText = computed(() => JSON.stringify(SAMPLE_FACTS[props.rule?.resource_type ?? ''] ?? {}, null, 2))
const fields = computed(() => Object.keys(SAMPLE_FACTS[props.rule?.resource_type ?? ''] ?? {}))

function init() {
  const r = props.rule
  if (!r) return
  const expr = JSON.parse(JSON.stringify(r.expr))
  Object.assign(form, {
    name: r.name, kind: r.kind, enabled: r.enabled, for_count: r.for_count, repeat_interval_sec: r.repeat_interval_sec ?? 0,
    runbook: r.runbook ?? '', scope: { ...r.scope }, expr: { levels: [], ...expr }, recover: { ...r.recover },
  })
  factsText.value = sampleText.value
  result.value = null
  error.value = ''
  if (props.focusDryRun) dryRun()
}
function addLevel() {
  const last = form.expr.levels[form.expr.levels.length - 1]
  const next = SEVERITIES[Math.min((last ? SEVERITIES.indexOf(last.severity) : -1) + 1, 3)]
  form.expr.levels.push({ severity: next, op: last?.op ?? '>=', threshold: (last?.threshold ?? 80) - 5 })
}
function insertField(f: string) { form.expr.when = `${form.expr.when ?? ''}${form.expr.when ? ' and ' : ''}${f} ` }

/** 只提交当前类型需要的字段，避免把编辑中的脏字段写入规则 */
function payload() {
  const e = form.expr
  const expr = form.kind === 'threshold'
    ? { value: e.value, unit: e.unit, title: e.title, levels: e.levels }
    : { when: e.when, severity: e.severity, title: e.title, ...(e.critical_severity ? { critical_severity: e.critical_severity } : {}), ...(e.value_field ? { value_field: e.value_field } : {}) }
  const recover = form.kind === 'threshold'
    ? (form.recover.below !== undefined && form.recover.below !== null ? { below: form.recover.below } : {})
    : (form.recover.when ? { when: form.recover.when } : {})
  return { expr, recover }
}
async function dryRun() {
  error.value = ''
  let facts: object
  try { facts = JSON.parse(factsText.value) } catch { error.value = 'facts 不是合法的 JSON'; return }
  running.value = true
  try {
    result.value = await dryRunRule({ kind: form.kind, ...payload(), facts })
  } catch (e) {
    result.value = null
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally { running.value = false }
}
async function save() {
  if (!form.name.trim()) { ElMessage.warning('请填写规则名称'); return }
  saving.value = true
  try {
    await saveRule(props.rule!.id, {
      name: form.name, kind: form.kind, scope: form.scope, ...payload(), for_count: form.for_count,
      repeat_interval_sec: form.repeat_interval_sec || null, runbook: form.runbook || null, enabled: form.enabled,
    })
    ElMessage.success('规则已保存，下一个巡检周期生效')
    emit('saved')
    emit('update:modelValue', false)
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.dlg-title { display: flex; align-items: center; gap: 10px; b { font-size: 16px; } }
.body { display: grid; grid-template-columns: 1fr 380px; gap: 20px; }
.left { min-width: 0; }
.w-full { width: 100%; }
.mono-input :deep(input), .mono-input :deep(textarea) { font-family: var(--hi-font-mono); font-size: 13px; }
.levels { display: flex; flex-direction: column; gap: 8px; width: 100%;
  .level { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; background: var(--hi-bg-soft); border-left: 3px solid var(--c);
    .sev { width: 120px; } .op { width: 76px; } } }
.fields { margin-top: 6px; font-size: 12px; color: var(--hi-text-3); line-height: 2;
  .field { margin-right: 6px; padding: 2px 6px; border-radius: 4px; cursor: pointer; font-family: var(--hi-font-mono); color: var(--el-color-primary); background: var(--el-color-primary-light-9);
    &:hover { background: var(--el-color-primary-light-8); } } }
.right {
  padding: 14px 16px; border-radius: var(--hi-radius); background: var(--hi-bg-soft); border: 1px solid var(--hi-border); align-self: start;
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
.mt-12 { margin-top: 12px; }
.foot-tip { margin-right: auto; float: left; line-height: 32px; }
</style>
