<template>
  <div v-loading="loading" class="tenant-panel">
    <div class="table-heading mb-16">
      <b>已接入租户<span class="text-3"> · {{ tenants.length }} 个</span></b>
      <div class="table-actions">
        <el-popover placement="bottom-end" :width="460" trigger="click">
          <template #reference><el-button link type="primary" :icon="QuestionFilled">接入说明</el-button></template>
          <div class="metrics-help">
            <p><b>服务原生</b>：DCS 等通过服务接口采集，本地历史仅包含已保存的样本。</p>
            <p><b>Prometheus</b>：按需配置地址，查询已支持的指标。</p>
          </div>
        </el-popover>
        <el-button v-if="canManage" type="primary" :icon="Plus" @click="openDialog()">新增租户</el-button>
      </div>
    </div>

    <div v-for="t in tenants" :id="`tenant-${t.id}`" :key="t.id" class="tenant" :style="{ '--tc': t.color }">
      <div class="t-head">
        <div class="t-title">
          <i class="bar" />
          <div>
            <div class="flex gap-8">
              <b class="t-name">{{ t.name }}</b><span class="mono text-3">{{ t.code }}</span>
              <el-tag :type="t.enabled ? 'success' : 'info'" size="small" effect="light">{{ t.enabled ? '已启用' : '已停用' }}</el-tag>
              <span class="auth" :style="{ '--ac': AUTH_TYPE[t.auth_type]?.color }"><el-icon><Key /></el-icon>{{ AUTH_TYPE[t.auth_type]?.label }}</span>
              <span class="metric" :style="{ '--mc': METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.color }">
                指标：{{ METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.label ?? t.metrics_effective }}<em v-if="t.metrics_source === 'AUTO'">（自动选择）</em>
              </span>
            </div>
            <div class="text-3 t-sub">IAM {{ t.iam_endpoint }} · 资源 {{ t.resource_count }} 个 · 专属用户 {{ t.user_count }} 人 · 项目 {{ t.projects.length }} 个</div>
          </div>
        </div>
        <div class="t-actions">
          <el-button v-if="canManage" type="primary" plain :icon="Lightning" :loading="testing === t.id" @click="test(t)">连通性测试</el-button>
          <el-button v-if="canManage" :icon="Edit" @click="openDialog(t)">编辑</el-button>
          <el-dropdown v-if="canManage" trigger="click" @command="(c: string) => onMore(c, t)">
            <el-button :icon="MoreFilled" />
            <template #dropdown><el-dropdown-menu>
              <el-dropdown-item command="toggle">{{ t.enabled ? '停用租户' : '启用租户' }}</el-dropdown-item>
              <el-dropdown-item command="delete" divided><span class="text-danger">删除租户</span></el-dropdown-item>
            </el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </div>

      <el-row :gutter="16">
        <el-col :xs="24" :lg="9">
          <div class="sub-title">认证信息</div>
          <div class="kv">
            <span>账号名（domain）</span><b>{{ t.domain_name || '-' }}</b>
            <span>账号 ID</span><b class="mono">{{ t.domain_id || '-' }}</b>
            <span>认证方式</span><b>{{ AUTH_TYPE[t.auth_type]?.label }}</b>
            <template v-if="t.auth_type === 'PASSWORD'"><span>IAM 用户名</span><b class="mono">{{ t.username || '-' }}</b></template>
            <template v-else><span>Access Key</span><b class="mono">{{ t.access_key_mask || '-' }}</b></template>
            <span>{{ t.auth_type === 'AKSK' ? 'Secret Key' : '密码' }}</span>
            <b><el-tag :type="t.secret_set ? 'success' : 'danger'" size="small" effect="plain">{{ t.secret_set ? '已加密保存' : '未设置' }}</el-tag><em class="text-3">（不回显）</em></b>
            <span>报表收件人</span><b>{{ t.report_mail_to.join('、') || '-' }}</b>
            <span>备注</span><b class="remark">{{ t.remark || '-' }}</b>
          </div>
          <p class="text-3 auth-desc">{{ AUTH_TYPE[t.auth_type]?.desc }}</p>
        </el-col>

        <el-col :xs="24" :lg="7">
          <div class="sub-title">接口与指标能力<span class="text-3">连通性测试 / 巡检时更新</span></div>
          <div class="caps">
            <div v-for="(name, k) in CAPABILITY" :key="k" class="cap" :class="capState(t, k)">
              <el-icon><component :is="capIcon(t, k)" /></el-icon>
              <div class="flex-1">
                <div class="c1"><b>{{ name }}</b><span>{{ capText(t, k) }}</span></div>
                <div class="text-3 ellipsis" :title="t.capabilities[k]?.reason">{{ t.capabilities[k]?.reason || (k === 'prometheus' && !t.prometheus_url ? '未配置 Prometheus 地址' : '尚未检查') }}</div>
              </div>
            </div>
          </div>
          <div class="sub-title">ECS 指标来源</div>
          <div class="kv one">
            <span>配置</span><b>{{ METRICS_SOURCE[t.metrics_source]?.label }}</b>
            <span>实际生效</span><b :style="{ color: METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.color }">{{ METRICS_SOURCE[effectiveMetricsSource(t.metrics_source, t.metrics_effective)]?.label }}</b>
            <span>Prometheus</span><b class="mono">{{ t.prometheus_url || '-' }}</b>
          </div>
        </el-col>

        <el-col :xs="24" :lg="8">
          <div class="sub-title">最近一次连通性测试
            <span class="text-3">{{ t.last_check?.at ? `${fmtTime(t.last_check.at, 'MM-DD HH:mm')} · HCS ${t.last_check.hcs_version ?? '-'}` : '未测试' }}</span></div>
          <div v-if="t.last_check?.configuration_warnings?.length" class="configuration-warnings" role="alert">
            <p v-for="warning in t.last_check.configuration_warnings" :key="warning">{{ warning }}</p>
          </div>
          <div v-if="t.last_check?.checks?.length" class="checks">
            <div v-for="c in t.last_check.checks" :key="c.name" class="check" :class="checkClass(c)" :title="[c.endpoint, c.detail || c.message].filter(Boolean).join('\n')">
              <el-icon><component :is="isAomCheck(c) ? RemoveFilled : c.ok ? CircleCheckFilled : c.kind === 'capability' ? RemoveFilled : CircleCloseFilled" /></el-icon>
              <span class="k">{{ CHECK_KIND[c.kind] }}</span>
              <div class="flex-1 check-target"><span class="ellipsis" :title="c.name">{{ c.name }}{{ isAomCheck(c) ? '（已停用，历史）' : '' }}</span><span v-if="c.endpoint" class="text-3 mono ellipsis check-endpoint">{{ c.endpoint }}</span></div>
              <span class="text-3 ellipsis d" :title="c.detail || c.message">{{ c.detail || c.message || '' }}</span>
              <span class="text-3 ms">{{ c.cost_ms != null ? `${c.cost_ms} ms` : '' }}</span>
            </div>
          </div>
          <el-empty v-else description="尚未测试，点击“连通性测试”检查" :image-size="48" />
        </el-col>
      </el-row>

      <div class="sub-title">纳管项目<span class="text-3">服务地址可按需覆盖</span></div>
      <el-table :data="t.projects" size="small" border>
        <el-table-column label="区域" prop="region_id" width="120" />
        <el-table-column label="项目" min-width="200"><template #default="{ row }">{{ row.project_name || '-' }}<div class="text-3 mono">{{ row.project_id }}</div></template></el-table-column>
        <el-table-column label="外部域名" prop="external_domain" min-width="160" />
        <el-table-column label="环境" width="80"><template #default="{ row }"><el-tag :type="ENV_TYPE[row.env]?.type ?? 'info'" size="small" effect="plain">{{ ENV_TYPE[row.env]?.label ?? row.env }}</el-tag></template></el-table-column>
        <el-table-column label="DataArts 工作空间" min-width="160"><template #default="{ row }"><el-tag v-for="w in row.workspaces" :key="w" size="small" effect="plain" class="ws">{{ w }}</el-tag><span v-if="!row.workspaces.length" class="text-3">-</span></template></el-table-column>
        <el-table-column label="服务地址覆盖" min-width="200">
          <template #default>
            <span v-if="!Object.keys(t.endpoints).length" class="text-3">未配置租户级覆盖</span>
            <span v-for="(v, k) in t.endpoints" v-else :key="k" class="ep"><b>{{ SERVICES[k] ?? k }}</b> {{ v }}</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="64" align="center"><template #default="{ row }"><el-icon v-if="row.enabled" color="var(--hi-ok)"><Select /></el-icon><span v-else class="text-3">停用</span></template></el-table-column>
      </el-table>
    </div>
    <el-empty v-if="!tenants.length && !loading" description="还没有接入租户" />

    <TenantDialog v-model="dlg.visible" :tenant="dlg.tenant" :info="info" @saved="load" />
    <p class="text-3 moved-tip">ECS 采集策略在“巡检任务”的 ECS 任务行设置；升级短信在“系统设置 → 租户短信”配置。</p>
  </div>
</template>

<script setup lang="ts">
import {
  CircleCheckFilled, CircleCloseFilled, Edit, Key, Lightning, MoreFilled, Plus, QuestionFilled, RemoveFilled, Select, WarningFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, markRaw, nextTick, onMounted, reactive, ref } from 'vue'
import { deleteTenant, listTenants, testTenant, updateTenant } from '@/api'
import { useUserStore } from '@/stores/user'
import type { SystemInfo, Tenant, TenantCheck } from '@/types'
import { AUTH_TYPE, CAPABILITY, CHECK_KIND, ENV_TYPE, METRICS_SOURCE, SERVICES, effectiveMetricsSource } from '@/utils/dict'
import { fmtTime } from '@/utils/format'
import TenantDialog from './TenantDialog.vue'
import { toTenantIn } from './tenant-form'

defineProps<{ info: SystemInfo | null }>()
const user = useUserStore()
const canManage = computed(() => user.can('tenant:manage'))
const tenants = ref<Tenant[]>([])
const loading = ref(false)
const testing = ref<number | null>(null)
const dlg = reactive({ visible: false, tenant: null as Tenant | null })

async function load() {
  loading.value = true
  try { tenants.value = await listTenants() } finally { loading.value = false }
  focusTenantFromHash()
}
/** 其他页面带 #tenant-{id} 跳过来（如 ECS 采集策略里的“配置租户服务地址”）：卡片渲染后再滚动，路由的滚动行为发生得太早 */
function focusTenantFromHash() {
  if (typeof window === 'undefined' || !/^#tenant-\d+$/.test(window.location.hash)) return
  const id = window.location.hash.slice(1)
  void nextTick(() => document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
}
onMounted(load)
defineExpose({ reload: load })

const capState = (t: Tenant, k: string) => {
  const c = t.capabilities[k]
  if (!c || c.available == null) return 'unknown'
  return c.available ? 'ok' : 'off'
}
const capIcon = (t: Tenant, k: string) => markRaw({ ok: CircleCheckFilled, off: RemoveFilled, unknown: WarningFilled }[capState(t, k)])
const capText = (t: Tenant, k: string) => ({ ok: '可用', off: '不可用', unknown: '未检查' }[capState(t, k)])
const isAomCheck = (c: TenantCheck) => /\baom\b/i.test(c.name)
const checkClass = (c: TenantCheck) => (isAomCheck(c) ? 'off' : c.ok ? 'ok' : c.kind === 'capability' ? 'off' : 'fail')

function openDialog(t?: Tenant) {
  dlg.tenant = t ?? null
  dlg.visible = true
}
async function test(t: Tenant) {
  testing.value = t.id
  try {
    const r = await testTenant(t.id)
    const i = tenants.value.findIndex((x) => x.id === t.id)
    if (i >= 0) tenants.value[i] = r.tenant
    const failed = r.checks.filter((c) => !c.ok && c.kind !== 'capability')
    if (failed.length) ElMessage.warning(`${t.short_name}：${failed.length} 项检查失败（${failed.map((c) => c.name).join('、')}）`)
    else ElMessage.success(`${t.short_name}：认证与服务接口全部通过，指标来源 ${METRICS_SOURCE[effectiveMetricsSource(r.tenant.metrics_source, r.metrics_source)]?.label ?? r.metrics_source}`)
  } finally { testing.value = null }
}
async function onMore(cmd: string, t: Tenant) {
  if (cmd === 'toggle') {
    if (t.enabled) await ElMessageBox.confirm(`停用后不再巡检「${t.name}」，已有台账与告警保留。确定停用？`, '停用租户', { type: 'warning' })
    await updateTenant(t.id, { ...toTenantIn(t), enabled: !t.enabled })
    ElMessage.success(t.enabled ? '已停用' : '已启用')
    await load()
  } else if (cmd === 'delete') {
    await ElMessageBox.confirm(`删除租户「${t.name}」？已有台账 / 巡检数据的租户不能删除，可改为停用。`, '删除租户', { type: 'warning', confirmButtonText: '删除' })
    await deleteTenant(t.id)
    ElMessage.success('已删除')
    await load()
  }
}
</script>

<style scoped lang="scss">
.intro { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 12px 16px; margin-bottom: 16px; border-radius: var(--hi-radius-sm);
  background: linear-gradient(180deg, #f5f8ff, #fafbff); border: 1px solid var(--el-color-primary-light-8);
  .i-main { display: flex; gap: 10px; font-size: 13px; line-height: 1.75; color: var(--hi-text-2); > .el-icon { color: var(--el-color-primary); margin-top: 3px; flex: none; } }
  .notes { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; font-size: 12.5px; color: var(--hi-text-3);
    em { display: inline-block; min-width: 76px; margin-right: 6px; font-style: normal; font-weight: 600; font-size: 12px; }
    .ces { color: #1d63f0; } .prom { color: #e6522c; } .es { color: var(--hi-text-2); } }
  .i-side { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; flex: none; } }
.metrics-help { font-size: 13px; line-height: 1.75; p { margin: 4px 0 0; } }
.tenant { position: relative; padding: 16px 18px 18px; margin-bottom: 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff;
  &::before { content: ''; position: absolute; left: 0; top: 0; right: 0; height: 3px; border-radius: var(--hi-radius) var(--hi-radius) 0 0; background: var(--tc); } }
.t-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 6px;
  .t-title { display: flex; gap: 12px; align-items: center; .bar { width: 10px; height: 10px; border-radius: 50%; background: var(--tc); box-shadow: 0 0 0 4px color-mix(in srgb, var(--tc) 18%, transparent); flex: none; } }
  .t-name { font-size: 16px; }
  .t-sub { margin-top: 3px; }
  .auth, .metric { display: inline-flex; align-items: center; gap: 4px; height: 22px; padding: 0 8px; border-radius: 11px; font-size: 12px; font-weight: 500; }
  .auth { color: var(--ac); background: color-mix(in srgb, var(--ac) 10%, #fff); }
  .metric { color: var(--mc); background: color-mix(in srgb, var(--mc) 9%, #fff); em { font-style: normal; color: var(--hi-text-3); font-weight: 400; } }
  .t-actions { display: flex; gap: 8px; flex: none; } }
.sub-title { margin: 14px 0 8px; font-weight: 600; font-size: 13px; color: var(--hi-text-2); display: flex; align-items: baseline; gap: 8px; .text-3 { font-weight: 400; } }
.kv { display: grid; grid-template-columns: 116px 1fr; gap: 7px 10px; font-size: 13px; align-items: center;
  > span { color: var(--hi-text-3); } > b { font-weight: 500; color: var(--hi-text-1); min-width: 0; word-break: break-all; em { font-style: normal; margin-left: 4px; } }
  .remark { font-weight: 400; color: var(--hi-text-2); }
  &.one { grid-template-columns: 90px 1fr; } }
.auth-desc { margin: 10px 0 0; line-height: 1.6; }
.caps { display: flex; flex-direction: column; gap: 8px;
  .cap { display: flex; gap: 10px; align-items: flex-start; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--hi-border-light); background: var(--hi-bg-soft);
    > .el-icon { margin-top: 2px; font-size: 16px; }
    .c1 { display: flex; justify-content: space-between; font-size: 13px; span { font-size: 12px; } }
    &.ok { > .el-icon, .c1 span { color: var(--hi-ok); } }
    &.off { > .el-icon, .c1 span { color: var(--hi-text-3); } }
    &.unknown { > .el-icon, .c1 span { color: var(--hi-p3); } } } }
.checks { border: 1px solid var(--hi-border-light); border-radius: 8px; padding: 4px 10px; max-height: 250px; overflow: auto;
  .check { display: grid; grid-template-columns: 14px max-content minmax(0, 1fr) max-content; align-items: center; column-gap: 8px; row-gap: 3px; padding: 6px 0; font-size: 12.5px; border-bottom: 1px dashed var(--hi-border-light); &:last-child { border-bottom: none; }
    > .el-icon { flex: none; }
    .k { flex: none; font-size: 11px; padding: 0 5px; border-radius: 3px; background: var(--hi-bg-soft); color: var(--hi-text-3); }
    .d { grid-column: 2 / 5; white-space: normal; overflow-wrap: anywhere; } .ms { grid-column: 4; grid-row: 1; min-width: 34px; text-align: right; }
    &.ok > .el-icon { color: var(--hi-ok); } &.off > .el-icon { color: var(--hi-text-3); } &.fail { > .el-icon, .flex-1 { color: var(--hi-p1); } } } }
.check-target { min-width: 0; > span { display: block; } }
.check-endpoint { font-size: 11px; margin-top: 2px; }
.configuration-warnings { margin-bottom: 8px; padding: 6px 10px; border-left: 2px solid var(--hi-p3); color: var(--hi-text-2); font-size: 12px; line-height: 1.6; p { margin: 2px 0; overflow-wrap: anywhere; } }
.ws { margin-right: 4px; }
.moved-tip { margin: 4px 0 0; font-size: 12px; }
.ep { display: block; font-size: 12px; b { font-weight: 500; margin-right: 4px; } }
@media (max-width: 767px) {
  .t-head, .t-title .flex, .t-actions { flex-wrap: wrap; }
  .t-title { min-width: 0; }
  .t-title > div { min-width: 0; }
  .t-sub { overflow-wrap: anywhere; }
  .kv { grid-template-columns: 98px minmax(0, 1fr); }
  .tenant :deep(.el-row) { row-gap: 16px; }
}
</style>
