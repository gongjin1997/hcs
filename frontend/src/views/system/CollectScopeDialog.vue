<template>
  <el-dialog v-model="visible" :title="title" width="880px" append-to-body destroy-on-close>
    <div v-loading="saving" class="scope-dialog">
      <el-alert type="info" :closable="false" show-icon class="mb-12">
        <template #title>在“采集开关”开启的前提下，只采集（或屏蔽）这类资源里的一部分对象</template>
        <div class="tips">
          <span><b>排除</b>：命中的对象不采集、不评估、不告警，其活动告警自动关闭并留处理记录。<b>仅采集</b>：只采集命中的对象，其余都不采集。存在多条规则时，命中任一排除规则即被排除；有“仅采集”规则时必须至少命中其中一条。</span>
          <span>同一条规则里的多个条件要<b>同时</b>满足，同一个条件里的多个值满足<b>任一</b>即可。值可以写精确内容，或用 <code>*</code>（任意长度）、<code>?</code>（一个字符）通配，区分大小写。</span>
          <span>范围只作用于巡检与告警，资源台账仍完整记录云上资源，只会标注“范围外”。</span>
        </div>
      </el-alert>

      <div v-if="tenantId !== 0" class="mode">
        <el-radio-group v-model="mode" :disabled="!canManage" aria-label="范围来源">
          <el-radio-button value="follow">跟随全局（{{ globalText }}）</el-radio-button>
          <el-radio-button value="own">单独设置</el-radio-button>
        </el-radio-group>
        <span class="text-3">租户的单独设置优先于全局默认；设置为空规则表示该租户不限制。</span>
      </div>
      <div v-else class="text-3 mb-12">全局默认适用于没有单独设置的所有租户。</div>

      <template v-if="mode === 'own'">
        <div v-if="!drafts.length" class="empty-rules">
          <el-empty description="没有规则：该类资源全部采集" :image-size="56" />
        </div>
        <div v-for="(draft, index) in drafts" :key="index" class="rule" :class="draft.effect">
          <div class="rule-head">
            <span class="no">规则 {{ index + 1 }}</span>
            <el-select v-model="draft.effect" size="small" class="effect" :disabled="!canManage" aria-label="规则类型">
              <el-option v-for="o in EFFECT_OPTIONS" :key="o.value" :value="o.value" :label="o.label">
                <span>{{ o.label }}</span><span class="opt-hint">{{ o.hint }}</span>
              </el-option>
            </el-select>
            <el-input v-model="draft.note" size="small" maxlength="200" placeholder="备注（可选），例如为什么屏蔽" :disabled="!canManage" class="note" />
            <el-button v-if="canManage" link type="danger" size="small" @click="drafts.splice(index, 1)">删除规则</el-button>
          </div>
          <div class="conds">
            <div v-for="dim in usedDims(draft)" :key="dim" class="cond">
              <span class="dim">{{ labelOf(dim) }}</span>
              <el-select v-model="draft.match[dim]" multiple filterable allow-create default-first-option collapse-tags-tooltip
                         :reserve-keyword="false" :disabled="!canManage" :placeholder="hintOf(dim)" class="values"
                         @visible-change="(open: boolean) => open && loadCandidates(dim)">
                <el-option v-for="c in candidates[dim] ?? []" :key="c.value" :value="c.value" :label="c.value">
                  <span>{{ c.value }}</span><span class="opt-hint">{{ c.count }} 个</span>
                </el-option>
              </el-select>
              <el-button v-if="canManage" link size="small" @click="removeDim(draft, dim)">移除</el-button>
            </div>
            <el-dropdown v-if="canManage && freeDims(draft).length" trigger="click" @command="(dim: string) => addDim(draft, dim)">
              <el-button size="small" :icon="Plus">添加条件</el-button>
              <template #dropdown>
                <el-dropdown-menu><el-dropdown-item v-for="d in freeDims(draft)" :key="d.key" :command="d.key">{{ d.label }}</el-dropdown-item></el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </div>
        <div v-if="canManage" class="add-rules">
          <el-button :icon="Plus" @click="drafts.push(newDraft('exclude'))">添加排除规则</el-button>
          <el-button :icon="Plus" @click="drafts.push(newDraft('include'))">添加“仅采集”规则</el-button>
        </div>
      </template>
      <div v-else class="text-3 mb-12">当前使用全局默认：{{ globalText }}。</div>

      <el-alert v-if="errors.length" type="warning" :closable="false" show-icon class="mt-12" :title="errors.join('；')" />

      <div class="preview" :class="{ stale: previewStale }">
        <div class="preview-head">
          <b>影响预估</b>
          <el-button size="small" :loading="previewing" :disabled="!!errors.length" @click="runPreview">重新预估</el-button>
        </div>
        <div v-if="preview" class="counts">
          <span>已知资源 <b>{{ preview.total }}</b></span>
          <span class="ok">采集 <b>{{ preview.included }}</b></span>
          <span class="out">范围外 <b>{{ preview.excluded }}</b></span>
          <span v-if="preview.newly_excluded">新排除 <b>{{ preview.newly_excluded }}</b></span>
          <span v-if="preview.newly_included">恢复采集 <b>{{ preview.newly_included }}</b></span>
          <span v-if="preview.closing_alarms" class="warn">将关闭活动告警 <b>{{ preview.closing_alarms }}</b></span>
        </div>
        <div v-if="preview?.samples.excluded.length" class="samples">范围外示例：{{ preview.samples.excluded.join('、') }}</div>
        <div v-if="preview?.truncated" class="text-3">资源很多，预估只统计了前 20000 个。</div>
        <div v-if="!preview && !previewing" class="text-3">修改规则后自动预估；只统计已采集或同步过的资源，新出现的对象按规则实时判定。</div>
      </div>
    </div>
    <template #footer>
      <el-button @click="visible = false">{{ canManage ? '取消' : '关闭' }}</el-button>
      <el-button v-if="canManage" type="primary" :loading="saving" :disabled="!dirty || !!errors.length" @click="save">保存范围</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { collectScopeCandidates, previewCollectScope, saveCollectScope } from '@/api'
import type { ScopeCandidate, ScopeDimension, ScopePreview, ScopeType } from '@/types'
import {
  DIMENSION_HINTS, EFFECT_OPTIONS, describeRule, draftErrors, newDraft, sameRules, toDrafts, toPayload, type RuleDraft,
} from './collect-scope'

const props = defineProps<{
  modelValue: boolean; tenantId: number; tenantName: string; type: ScopeType | null; canManage: boolean
  limits: { rules: number; patterns: number; pattern_length: number }
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>()
const visible = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const mode = ref<'follow' | 'own'>('own')
const drafts = ref<RuleDraft[]>([])
const preview = ref<ScopePreview | null>(null)
const previewStale = ref(false)
const previewing = ref(false)
const saving = ref(false)
const candidates = reactive<Record<string, ScopeCandidate[]>>({})
let token = 0
let timer: ReturnType<typeof setTimeout> | undefined

const dims = computed<ScopeDimension[]>(() => props.type?.dimensions ?? [])
const labelOf = (dim: string) => dims.value.find((d) => d.key === dim)?.label ?? dim
const hintOf = (dim: string) => DIMENSION_HINTS[dim] ?? '输入后回车添加'
const title = computed(() => `${props.tenantId === 0 ? '全局默认' : props.tenantName} · ${props.type?.name ?? ''} · 采集范围`)
const cell = computed(() => (props.type && props.tenantId !== 0 ? props.type.tenants[String(props.tenantId)] : undefined))
const savedRules = computed(() => (props.tenantId === 0 ? props.type?.global.rules : cell.value?.setting) ?? null)
const globalText = computed(() => {
  const rules = props.type?.global.rules ?? []
  return rules.length ? rules.map((r) => describeRule(r, dims.value)).join(' ｜ ') : '全部采集'
})

const usedDims = (draft: RuleDraft) => Object.keys(draft.match)
const freeDims = (draft: RuleDraft) => dims.value.filter((d) => !(d.key in draft.match))
const addDim = (draft: RuleDraft, dim: string) => { draft.match[dim] = []; void loadCandidates(dim) }
const removeDim = (draft: RuleDraft, dim: string) => { delete draft.match[dim] }

async function loadCandidates(dim: string) {
  if (!props.type || !props.canManage || candidates[dim]) return
  try { candidates[dim] = await collectScopeCandidates(props.tenantId, props.type.code, dim, '', 200) } catch { candidates[dim] = [] }
}

const errors = computed(() => (mode.value === 'own' ? draftErrors(drafts.value, props.limits) : []))
const payload = () => (mode.value === 'follow' ? null : toPayload(drafts.value))
const dirty = computed(() => {
  const now = payload()
  const before = savedRules.value
  if (now === null || before === null) return now !== before
  return !sameRules(now, before)
})

function reset() {
  const initial = props.tenantId === 0 ? (props.type?.global.rules ?? []) : (cell.value?.setting ?? cell.value?.rules ?? [])
  mode.value = props.tenantId === 0 || cell.value?.setting !== null ? 'own' : 'follow'
  drafts.value = toDrafts(initial)
  preview.value = null
  previewStale.value = false
  Object.keys(candidates).forEach((k) => delete candidates[k])
}
watch(() => props.modelValue, (open) => { if (open) { reset(); void runPreview() } else clearTimeout(timer) }, { immediate: true })

async function runPreview() {
  if (!props.type || errors.value.length) return
  const mine = ++token
  previewing.value = true
  try {
    const result = await previewCollectScope(props.tenantId, props.type.code, payload())
    if (mine === token) { preview.value = result; previewStale.value = false }
  } catch { if (mine === token) preview.value = null } finally { if (mine === token) previewing.value = false }
}
watch([drafts, mode], () => {
  if (!props.modelValue) return
  previewStale.value = true
  clearTimeout(timer)
  timer = setTimeout(() => void runPreview(), 700)
}, { deep: true })
onBeforeUnmount(() => clearTimeout(timer))

async function save() {
  if (!props.type || !props.canManage || saving.value || errors.value.length || !dirty.value) return
  const rules = payload()
  saving.value = true
  try {
    const latest = await previewCollectScope(props.tenantId, props.type.code, rules)
    if (latest.closing_alarms > 0) {
      await ElMessageBox.confirm(`保存后将有 ${latest.newly_excluded} 个资源落到范围外，其 ${latest.closing_alarms} 条活动告警会被自动关闭（写入处理记录，不是指标恢复）。`,
        '确认保存采集范围', { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '取消' })
    }
    const result = await saveCollectScope(props.tenantId, props.type.code, rules)
    ElMessage.success(result.changed
      ? `已保存：新排除 ${result.excluded}、恢复 ${result.restored}${result.closed_alarms ? `，自动关闭 ${result.closed_alarms} 条活动告警` : ''}；调度进程下一轮生效`
      : '没有变化')
    visible.value = false
    emit('saved')
  } catch (error) {
    if (error !== 'cancel') throw error
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.tips { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; font-size: 12px; line-height: 1.7; color: var(--hi-text-2);
  code { padding: 0 4px; border-radius: 3px; background: var(--hi-bg-soft); } }
.mode { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.empty-rules { padding: 4px 0; }
.rule { margin-bottom: 12px; padding: 10px 12px; border: 1px solid var(--hi-border); border-radius: 8px; border-left-width: 4px;
  &.exclude { border-left-color: var(--hi-p2); } &.include { border-left-color: var(--hi-ok); }
  .rule-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; .no { font-weight: 600; color: var(--hi-text-2); } .effect { width: 112px; } .note { flex: 1; } }
  .conds { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; width: 100%;
    .cond { display: flex; align-items: center; gap: 8px; width: 100%; .dim { width: 84px; flex: none; color: var(--hi-text-2); font-size: 13px; } .values { flex: 1; } } } }
.opt-hint { float: right; margin-left: 16px; color: var(--hi-text-3); font-size: 12px; }
.add-rules { display: flex; gap: 8px; margin-bottom: 12px; }
.preview { margin-top: 14px; padding: 10px 14px; border-radius: 8px; background: var(--hi-bg-soft); transition: opacity .2s;
  &.stale { opacity: .55; }
  .preview-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .counts { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; .ok b { color: var(--hi-ok); } .out b { color: var(--hi-p2); } .warn { color: var(--hi-p1); } }
  .samples { margin-top: 6px; font-size: 12px; color: var(--hi-text-2); overflow-wrap: anywhere; } }
</style>
