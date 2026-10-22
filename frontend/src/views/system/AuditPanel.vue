<template>
  <div class="audit-panel">
    <div class="filters">
      <el-select v-model="q.days" class="w-120" @change="search">
        <el-option v-for="d in [1, 7, 30, 90, 180]" :key="d" :value="d" :label="d === 1 ? '最近 24 小时' : `最近 ${d} 天`" />
      </el-select>
      <el-select v-model="q.action" multiple collapse-tags collapse-tags-tooltip clearable filterable placeholder="全部操作" class="w-260" @change="search">
        <el-option-group v-for="g in actionGroups" :key="g.module" :label="g.label">
          <el-option :value="`${g.module}.*`" :label="`全部${g.label}操作`" />
          <el-option v-for="a in g.items" :key="a.code" :value="a.code" :label="a.name"><span>{{ a.name }}</span><span class="mono text-3 opt-code">{{ a.code }}</span></el-option>
        </el-option-group>
      </el-select>
      <el-select v-model="q.result" clearable placeholder="全部结果" class="w-120" @change="search">
        <el-option v-for="(r, k) in AUDIT_RESULT" :key="k" :value="k" :label="r.label" />
      </el-select>
      <el-input v-model="q.keyword" placeholder="操作人 / 对象 / IP" clearable :prefix-icon="Search" class="w-220" @keyup.enter="search" @clear="search" />
      <el-button type="primary" :icon="Search" @click="search">查询</el-button>
      <span class="flex-1" />
      <span class="text-3">共 {{ total }} 条 · 可查询最近 180 天；密码、SK 等敏感字段不写入审计</span>
    </div>
    <el-table v-loading="loading" :data="items" row-key="id" size="default">
      <el-table-column label="时间" width="150"><template #default="{ row }">{{ fmtTime(row.at) }}<div class="text-3">{{ fromNow(row.at) }}</div></template></el-table-column>
      <el-table-column label="操作人" width="130"><template #default="{ row }"><b>{{ row.operator }}</b><div v-if="row.username" class="text-3 mono">{{ row.username }}</div></template></el-table-column>
      <el-table-column label="操作" width="170">
        <template #default="{ row }"><span class="act" :style="{ '--c': moduleColor(row.action) }">{{ row.action_name }}</span><div class="mono text-3 code">{{ row.action }}</div></template>
      </el-table-column>
      <el-table-column label="对象" min-width="190">
        <template #default="{ row }">
          <template v-if="row.target_name || row.target_id"><span class="text-3">{{ TARGET[row.target_type ?? ''] ?? row.target_type }}</span> {{ row.target_name ?? `#${row.target_id}` }}</template>
          <span v-else class="text-3">-</span>
        </template>
      </el-table-column>
      <el-table-column label="租户" width="100"><template #default="{ row }"><TenantTag :id="row.tenant_id" :name="row.tenant_name" /></template></el-table-column>
      <el-table-column label="结果" width="80">
        <template #default="{ row }"><el-tag :type="AUDIT_RESULT[row.result]?.type ?? 'info'" size="small" effect="light">{{ AUDIT_RESULT[row.result]?.label ?? row.result }}</el-tag></template>
      </el-table-column>
      <el-table-column label="详情" min-width="240" show-overflow-tooltip><template #default="{ row }"><span class="text-2 detail">{{ detailText(row as AuditLog) }}</span></template></el-table-column>
      <el-table-column label="来源 IP" width="120"><template #default="{ row }"><span class="mono text-3">{{ row.ip ?? '-' }}</span></template></el-table-column>
    </el-table>
    <div class="pager">
      <el-pagination v-model:current-page="q.page" v-model:page-size="q.page_size" :total="total" :page-sizes="[20, 50, 100]"
                     layout="total, sizes, prev, pager, next" background @current-change="load" @size-change="search" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { type AuditQuery, listAuditLogs } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import type { AuditLog } from '@/types'
import { AUDIT_RESULT } from '@/utils/dict'
import { fmtTime, fromNow } from '@/utils/format'

const loading = ref(false)
const items = ref<AuditLog[]>([])
const total = ref(0)
const actions = ref<{ code: string; name: string }[]>([])
const q = reactive<Required<Pick<AuditQuery, 'days' | 'page' | 'page_size'>> & { action: string[]; result: string; keyword: string }>({
  days: 7, page: 1, page_size: 20, action: [], result: '', keyword: '',
})

async function load() {
  loading.value = true
  try {
    const r = await listAuditLogs({ ...q, action: q.action.length ? q.action : undefined, result: q.result || undefined, keyword: q.keyword.trim() || undefined })
    items.value = r.items
    total.value = r.total
    if (!actions.value.length) actions.value = r.actions
  } finally { loading.value = false }
}
function search() {
  q.page = 1
  load()
}
onMounted(load)
defineExpose({ reload: load })

const MODULE: Record<string, { label: string; color: string }> = {
  auth: { label: '登录认证', color: '#4e5969' }, user: { label: '用户', color: '#1d63f0' }, role: { label: '角色', color: '#7a5af8' },
  tenant: { label: '租户接入', color: '#0e9f8f' }, rule: { label: '规则', color: '#f08a24' }, alarm: { label: '告警', color: '#e0457b' },
  ledger: { label: '台账', color: '#2e7d32' }, report: { label: '报表', color: '#00838f' }, probe: { label: '背压探针', color: '#6d4c41' },
  podevent: { label: '容器事件', color: '#5b6cf0' }, task: { label: '巡检任务', color: '#8d6e63' },
}
const moduleColor = (action: string) => MODULE[action.split('.')[0]]?.color ?? '#8c8c8c'
const actionGroups = computed(() => {
  const m = new Map<string, { code: string; name: string }[]>()
  for (const a of actions.value) {
    const k = a.code.split('.')[0]
    m.set(k, [...(m.get(k) ?? []), a])
  }
  return [...m.entries()].map(([module, list]) => ({ module, label: MODULE[module]?.label ?? module, items: list }))
})
const TARGET: Record<string, string> = {
  tenant: '租户', user: '用户', role: '角色', rule: '规则', alarm: '告警', resource: '资源', report: '报表', probe: '探针', task: '任务',
}
const DETAIL_LABEL: Record<string, string> = {
  remember: '记住登录', method: '方式', ok: '通过', changed: '变更字段', fields: '字段', error: '错误', reason: '原因', count: '数量',
  to: '收件人', kind: '类型', status: '状态', run_ids: '运行', ids: 'ID', note: '备注', role: '角色', tenant_ids: '租户', enabled: '启用',
  permissions: '权限数', tenants: '租户', events: '事件数', failed: '未通过项', metrics_source: '生效指标来源', auth_type: '认证方式',
}
const VALUE_LABEL: Record<string, Record<string, string>> = {
  metrics_source: { CES: '云监控 CES', PROMETHEUS: 'Prometheus', NONE: '不采集指标', AUTO: '自动探测' },
  auth_type: { PASSWORD: 'IAM 用户名 + 密码', AKSK: 'AK/SK 签名' },
}
function fmtVal(v: unknown): string {
  if (v === true) return '是'
  if (v === false) return '否'
  if (v == null) return '-'
  if (Array.isArray(v)) return v.length > 6 ? `${v.slice(0, 6).map(fmtVal).join('、')} 等 ${v.length} 项` : v.map(fmtVal).join('、')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
const detailText = (a: AuditLog) => Object.entries(a.detail ?? {})
  .filter(([k, v]) => !(k === 'failed' && Array.isArray(v) && !v.length))
  .map(([k, v]) => `${DETAIL_LABEL[k] ?? k}：${typeof v === 'string' && VALUE_LABEL[k]?.[v] ? VALUE_LABEL[k][v] : fmtVal(v)}`).join('；') || '-'
</script>

<style scoped lang="scss">
.filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px;
  .w-120 { width: 140px; } .w-220 { width: 200px; } .w-260 { width: 260px; } .text-3 { font-size: 12px; } }
.flex-1 { flex: 1; }
.opt-code { float: right; margin-left: 14px; font-size: 11px; }
.act { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 4px; font-size: 12px; font-weight: 500;
  color: var(--c); background: color-mix(in srgb, var(--c) 9%, #fff); }
.code { font-size: 11px; margin-top: 2px; }
.detail { font-size: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 14px; }
</style>
