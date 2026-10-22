<template>
  <div class="rule-page">
    <PageHeader title="规则管理" desc="规则以 YAML 为基线随版本发布，页面修改即时落库并记录版本；表达式在安全沙箱中执行（仅允许比较、逻辑与白名单函数）">
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </PageHeader>

    <div class="hi-card">
      <div class="filter-bar mb-16">
        <el-radio-group v-model="type">
          <el-radio-button value="">全部（{{ rules.length }}）</el-radio-button>
          <el-radio-button v-for="(n, k) in RESOURCE_TYPES" :key="k" :value="k">{{ n }}（{{ countOf(k) }}）</el-radio-button>
        </el-radio-group>
        <el-input v-model="keyword" class="kw" clearable placeholder="规则 ID / 名称 / 检查项" :prefix-icon="Search" />
      </div>

      <el-table v-loading="loading" :data="filtered" row-key="id">
        <el-table-column label="规则" min-width="250">
          <template #default="{ row }">
            <div class="r-name">{{ row.name }}</div>
            <div class="text-3"><span class="mono">{{ row.id }}</span> · {{ RESOURCE_TYPES[row.resource_type] }} · {{ row.check_item }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="88">
          <template #default="{ row }"><el-tag size="small" :type="row.kind === 'threshold' ? 'primary' : 'info'" effect="plain">{{ row.kind === 'threshold' ? '阈值' : '状态' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="判定条件" min-width="330">
          <template #default="{ row }">
            <div v-if="row.kind === 'threshold'" class="levels">
              <span class="mono field">{{ row.expr.value }}</span>
              <span v-for="l in row.expr.levels" :key="l.severity" class="lv" :style="{ '--c': SEVERITY[l.severity].color, '--bg': SEVERITY[l.severity].bg }">
                {{ l.severity }} {{ l.op }} {{ l.threshold }}{{ row.expr.unit }}
              </span>
            </div>
            <div v-else class="state">
              <code>{{ row.expr.when }}</code>
              <span class="lv" :style="{ '--c': SEVERITY[row.expr.severity].color, '--bg': SEVERITY[row.expr.severity].bg }">→ {{ row.expr.severity }}</span>
              <span v-if="row.expr.critical_severity" class="text-3">核心资源 {{ row.expr.critical_severity }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="持续 / 恢复" width="170">
          <template #default="{ row }">
            <div>连续 <b class="num">{{ row.for_count }}</b> 次命中</div>
            <div class="text-3 ellipsis">{{ recoverText(row as Rule) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="重复通知" width="96"><template #default="{ row }">{{ row.repeat_interval_sec ? humanize(row.repeat_interval_sec) : '不重复' }}</template></el-table-column>
        <el-table-column label="版本" width="140">
          <template #default="{ row }"><div>v{{ row.version }}</div><div class="text-3">{{ versionText(row as Rule) }}</div></template>
        </el-table-column>
        <el-table-column label="启用" width="72">
          <template #default="{ row }"><el-switch :model-value="row.enabled" :disabled="!canWrite" size="small" @change="(v: string | number | boolean) => toggle(row as Rule, Boolean(v))" /></template>
        </el-table-column>
        <el-table-column label="操作" width="128" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditor(row as Rule)">{{ canWrite ? '编辑' : '查看' }}</el-button>
            <el-button link type="primary" @click="openEditor(row as Rule, true)">试运行</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <RuleEditorDialog v-model="editor.visible" :rule="editor.rule" :focus-dry-run="editor.dryRun" @saved="load" />
  </div>
</template>

<script setup lang="ts">
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { listRules, saveRule } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useUserStore } from '@/stores/user'
import type { Rule } from '@/types'
import { RESOURCE_TYPES, SEVERITY } from '@/utils/dict'
import { fmtTime, humanize } from '@/utils/format'
import RuleEditorDialog from './RuleEditorDialog.vue'

const canWrite = computed(() => useUserStore().can('rule:edit')) // 规则修改需要 rule:edit 权限
const rules = ref<Rule[]>([])
const loading = ref(false)
const type = ref('')
const keyword = ref('')
const editor = reactive({ visible: false, rule: null as Rule | null, dryRun: false })

async function load() {
  loading.value = true
  try { rules.value = await listRules() } finally { loading.value = false }
}
onMounted(load)

const countOf = (t: string) => rules.value.filter((r) => r.resource_type === t).length
// 按资源类型（与页签顺序一致）+ 规则 ID 排序
const TYPE_ORDER = Object.keys(RESOURCE_TYPES)
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rules.value
    .filter((r) => (!type.value || r.resource_type === type.value)
      && (!kw || [r.id, r.name, r.check_item].some((s) => s.toLowerCase().includes(kw))))
    .sort((a, b) => TYPE_ORDER.indexOf(a.resource_type) - TYPE_ORDER.indexOf(b.resource_type) || a.id.localeCompare(b.id))
})
const versionText = (r: Rule) =>
  !r.updated_by || r.updated_by === 'system' ? 'YAML 基线' : `${r.updated_by} · ${fmtTime(r.updated_at, 'MM-DD HH:mm')}`
const recoverText = (r: Rule) => (r.recover.below !== undefined ? `恢复：低于 ${r.recover.below}${r.expr.unit ?? ''}` : r.recover.when ? `恢复：${r.recover.when}` : '恢复：条件不再满足')

async function toggle(r: Rule, enabled: boolean) {
  await saveRule(r.id, {
    name: r.name, kind: r.kind, scope: r.scope, expr: r.expr, for_count: r.for_count, recover: r.recover,
    repeat_interval_sec: r.repeat_interval_sec, runbook: r.runbook, enabled,
  })
  ElMessage.success(`${enabled ? '已启用' : '已停用'}：${r.name}`)
  load()
}
function openEditor(r: Rule, dryRun = false) {
  editor.rule = r
  editor.dryRun = dryRun
  editor.visible = true
}
</script>

<style scoped lang="scss">
.kw { width: 240px !important; }
.r-name { font-weight: 600; }
.levels, .state { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.field { color: var(--hi-text-2); margin-right: 2px; }
.lv { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 4px; font-size: 12px; font-weight: 600;
  color: var(--c); background: var(--bg); font-family: var(--hi-font-mono); }
code { font-family: var(--hi-font-mono); font-size: 12px; padding: 2px 6px; border-radius: 4px; background: var(--hi-bg-soft); color: #7a3e9d; border: 1px solid var(--hi-border); }
</style>
