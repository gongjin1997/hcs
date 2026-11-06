<template>
  <div class="simulate">
    <div class="form-side">
      <div class="p-title">模拟一条告警</div>
      <el-form label-width="72px" size="default" @submit.prevent>
        <el-form-item v-if="app.tenants.length > 1" label="租户">
          <el-select v-model="q.tenant_id" :clearable="canEmptyTenant" placeholder="不指定" class="w-full">
            <el-option v-for="t in tenantOptions" :key="t.id" :value="t.id" :label="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="服务">
          <el-select v-model="q.service" filterable class="w-full" @change="q.resource_type = null">
            <el-option v-for="(n, code) in SERVICES" :key="code" :value="code" :label="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="资源类型">
          <el-select v-model="q.resource_type" clearable filterable placeholder="不指定" class="w-full">
            <el-option v-for="(n, code) in RESOURCE_TYPES" :key="code" :value="code" :label="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="级别">
          <el-radio-group v-model="q.severity">
            <el-radio-button v-for="s in SEVERITIES" :key="s" :value="s">{{ s }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="资源名称"><el-input v-model="q.resource_name" placeholder="用于匹配静默的“名称包含”" /></el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Promotion" :loading="loading" @click="run">模拟投递</el-button>
        </el-form-item>
      </el-form>
      <div class="text-3 small">只计算，不产生告警、不发送邮件。用来确认新加的路由 / 联系组是否按预期收到通知。</div>
    </div>

    <div class="result-side">
      <template v-if="res">
        <el-alert v-if="res.silence" type="warning" :closable="false" show-icon class="mb-12"
                  :title="`当前命中${res.silence.kind === 'MAINTENANCE' ? '维护窗口' : '静默'}“${res.silence.name}”：这类告警现在只记录为“已抑制”，不会发送通知`" />
        <div class="summary">
          <div>
            <span class="k">最终收件人</span>
            <template v-if="res.recipients.length"><el-tag v-for="e in res.recipients" :key="e" effect="plain" class="mono">{{ e }}</el-tag></template>
            <template v-else-if="res.platform_fallback?.length">
              <el-tag v-for="e in res.platform_fallback" :key="e" type="warning" effect="plain" class="mono">{{ e }}</el-tag>
              <span class="text-3">（没有路由命中，平台告警发给兜底收件人）</span>
            </template>
            <span v-else class="text-danger">没有任何路由命中：这类告警不会通知任何人，建议增加兜底路由</span>
          </div>
        </div>
        <div class="p-title">路由匹配过程（按优先级）</div>
        <div class="steps">
          <div v-for="s in res.steps" :key="s.id" class="step" :class="{ hit: s.matched, skip: s.skipped, off: !s.enabled }">
            <span class="prio num">{{ s.priority }}</span>
            <div class="body">
              <div class="line1">
                <b>{{ s.name }}</b>
                <el-tag v-if="!s.enabled" size="small" type="info">已停用</el-tag>
                <el-tag v-else-if="s.skipped" size="small" type="info" effect="plain">已停止匹配</el-tag>
                <el-tag v-else-if="s.matched" size="small" type="success">命中</el-tag>
                <span v-else class="text-3">不匹配</span>
                <span v-if="s.matched" class="text-3">{{ s.continue_match ? '→ 继续向下匹配' : '→ 命中即停止' }}</span>
              </div>
              <div v-if="s.matched" class="text-2 rcpt">{{ s.group_name }}：{{ s.recipients.map((r) => r.name).join('、') || '（联系组没有启用的成员）' }}</div>
            </div>
          </div>
        </div>
        <div class="p-title">未认领升级</div>
        <div v-if="res.escalation.length" class="esc">
          <div v-for="(e, i) in res.escalation" :key="i" class="esc-item">
            <el-icon><Timer /></el-icon><b>{{ e.after_min }} 分钟</b>未认领 → {{ e.names.join('、') || e.emails.join('、') || '-' }}
            <span class="text-3">（{{ e.route }}{{ e.note ? `；${e.note}` : '' }}）</span>
          </div>
        </div>
        <div v-else class="text-3">命中的路由没有配置升级策略</div>
      </template>
      <el-empty v-else description="选择条件后点击“模拟投递”" :image-size="90" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Promotion, Timer } from '@element-plus/icons-vue'
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { simulateRoute } from '@/api'
import { useAppStore } from '@/stores/app'
import type { SimulateResult } from '@/types'
import { RESOURCE_TYPES, SERVICES, SEVERITIES } from '@/utils/dict'

const app = useAppStore()
const user = useUserStore()
const tenantOptions = computed(() => app.currentTenant ? [app.currentTenant] : app.tenants)
const canEmptyTenant = computed(() => !!user.user && user.user.tenant_ids == null && app.tenantId == null)
const q = reactive({ tenant_id: (app.currentTenant?.id ?? tenantOptions.value[0]?.id ?? null) as number | null,
  service: 'dcs', resource_type: 'dcs_instance' as string | null, severity: 'P1', resource_name: 'redis-prod-01' })
const request = useLatestRequest()
const loading = request.loading
const res = ref<SimulateResult | null>(null)
watch(q, () => { request.invalidate(); res.value = null }, { deep: true, flush: 'sync' })
async function run() {
  if ((q.tenant_id == null && !canEmptyTenant.value) || (q.tenant_id != null && !tenantOptions.value.some(tenant => tenant.id === q.tenant_id))) {
    ElMessage.warning('请选择当前范围内的租户')
    return false
  }
  const snapshot = { ...q }
  res.value = null
  return request.run(() => simulateRoute(snapshot), result => { res.value = result }).catch(() => false)
}
defineExpose({ run })
</script>

<style scoped lang="scss">
.simulate { display: grid; grid-template-columns: 380px 1fr; gap: 24px; }
.form-side { padding-right: 24px; border-right: 1px solid var(--hi-border-light); .small { font-size: 12px; line-height: 1.6; } }
.p-title { margin: 4px 0 12px; font-weight: 600; font-size: 13px; color: var(--hi-text-2); }
.summary { padding: 12px 14px; margin-bottom: 14px; border-radius: 8px; background: var(--hi-bg-soft);
  > div { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; } .k { margin-right: 6px; color: var(--hi-text-3); font-size: 13px; } }
.steps { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
.step { display: flex; gap: 12px; align-items: flex-start; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--hi-border-light);
  .prio { flex: none; min-width: 36px; text-align: center; padding: 1px 6px; border-radius: 10px; font-weight: 700; font-size: 12px; color: var(--hi-text-3); background: var(--hi-bg-soft); }
  .line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .rcpt { margin-top: 4px; font-size: 13px; }
  &.hit { border-color: #b7eb8f; background: #f6ffed; .prio { color: var(--hi-ok); background: #e8f5e9; } }
  &.skip, &.off { opacity: 0.55; } }
.esc { display: flex; flex-direction: column; gap: 6px; font-size: 13px;
  .esc-item { display: flex; align-items: center; gap: 6px; .el-icon { color: var(--hi-p2); } } }
</style>
