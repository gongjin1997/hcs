<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getManageOneConfig, saveManageOneConfig } from '@/api/manageone'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { resourceViewerScopeKey } from '@/config/resource-navigation'
import { useManageOneConfig } from './useManageOneConfig'

const props = defineProps<{ modelValue: boolean; tenantId: number | null; tenantName?: string }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; saved: [] }>()
const user = useUserStore()
const app = useAppStore()
const selectedTenant = shallowRef(props.tenantId)
const scopedTenant = computed(() => props.tenantId ?? selectedTenant.value)
const inScope = computed(() => !!user.user && scopedTenant.value != null && (user.user.tenant_ids == null || user.user.tenant_ids.includes(scopedTenant.value)))
const availableTenants = computed(() => app.tenants.filter(tenant => user.user?.tenant_ids == null || user.user.tenant_ids.includes(tenant.id)))
const canRead = computed(() => inScope.value && user.canAny('ledger:view', 'task:view', 'tenant:view'))
const editable = computed(() => canRead.value && user.can('tenant:manage'))
watch(() => [props.modelValue, props.tenantId, app.tenantId] as const, (current, previous) => {
  selectedTenant.value = props.tenantId
  if (previous && previous[2] !== current[2] && props.modelValue) emit('update:modelValue', false)
}, { flush: 'sync' })
const { form, loaded, error, validationError, canSave, loading, saving, load, save } = useManageOneConfig({
  tenantId: () => scopedTenant.value, visible: () => props.modelValue, canView: () => canRead.value, canManage: () => editable.value,
  scopeKey: () => resourceViewerScopeKey(user), fetcher: getManageOneConfig, saver: saveManageOneConfig,
})
async function onSave() {
  if (await save()) { ElMessage.success('ManageOne 配置已保存'); emit('saved') }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="`${tenantName || '租户'} · ManageOne 配置`" width="min(620px, calc(100vw - 24px))" append-to-body destroy-on-close
    :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @update:model-value="emit('update:modelValue', $event)">
    <div v-loading="loading" class="manageone-config">
      <p class="text-3">使用 ManageOne 运维北向集成账号，与 IAM 凭据独立；无需安装 Prometheus。</p>
      <p class="text-3">仅查询 CPU、内存、云硬盘使用率；开机时间暂不支持。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-alert v-if="validationError" :title="validationError" type="warning" :closable="false" show-icon />
      <el-form-item v-if="tenantId == null" label="租户"><el-select v-model="selectedTenant" :disabled="saving" placeholder="请选择租户" aria-label="ManageOne 配置租户"><el-option v-for="tenant in availableTenants" :key="tenant.id" :value="tenant.id" :label="tenant.name" /></el-select></el-form-item>
      <p v-else-if="!canRead" class="text-3">无此租户的 ManageOne 配置查看权限</p>
      <template v-if="loaded">
        <div class="config-status"><el-tag :type="loaded.selected ? 'success' : 'info'" size="small">{{ loaded.selected ? '当前已选为 ECS 指标来源' : '当前未选为 ECS 指标来源' }}</el-tag><el-tag v-if="!editable" type="info" size="small">只读</el-tag></div>
        <el-form :disabled="!editable || loading || saving" label-width="130px" @submit.prevent="onSave">
          <el-form-item label="启用配置"><el-switch v-model="form.enabled" aria-label="启用 ManageOne 配置" /></el-form-item>
          <el-form-item label="运维北向 baseURL"><el-input v-model="form.endpoint" aria-label="ManageOne 运维北向 baseURL" placeholder="填写运维北向 baseURL" autocomplete="off" /></el-form-item>
          <el-form-item label="集成账号"><el-input v-model="form.username" aria-label="ManageOne 集成账号" autocomplete="off" /></el-form-item>
          <el-form-item label="组织（可选）"><el-input v-model="form.organization" aria-label="ManageOne 组织" autocomplete="off" /></el-form-item>
          <el-form-item label="密码">
            <el-input v-if="editable" v-model="form.password" type="password" aria-label="ManageOne 密码" autocomplete="new-password" placeholder="留空保留已保存密码" />
            <span v-else>{{ loaded.secret_set ? '已设置（不回显）' : '未设置' }}</span>
            <p v-if="editable" class="password-hint text-3">修改地址、账号或组织后须重新填写密码。</p>
            <el-checkbox v-if="editable && loaded.secret_set" v-model="form.clear_password" :disabled="form.enabled" aria-label="清除 ManageOne 密码">清除已保存密码（须先关闭配置）</el-checkbox>
          </el-form-item>
          <el-form-item v-if="editable" label="指标来源"><el-checkbox v-model="form.select_source" :disabled="!form.enabled || form.clear_password">用于 ECS 指标采集</el-checkbox></el-form-item>
        </el-form>
        <p class="text-3">选择来源不启用全局任务、租户采集阶段或总开关；关闭配置不自动切换来源。</p>
      </template>
    </div>
    <template #footer><el-button :disabled="saving" @click="emit('update:modelValue', false)">关闭</el-button><el-button v-if="canRead" :disabled="saving" :loading="loading" @click="load">重新读取</el-button><el-button v-if="editable" type="primary" :disabled="!canSave" :loading="saving" @click="onSave">保存配置</el-button></template>
  </el-dialog>
</template>

<style scoped lang="scss">
.manageone-config {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: calc(85vh - 180px);
  max-height: calc(85dvh - 180px);
  overflow-y: auto;
  padding-right: 4px;
}
.manageone-config > p, .password-hint { margin: 0; font-size: 12px; line-height: 1.7; }
.config-status { display: flex; gap: 8px; }
.password-hint { width: 100%; }
@media (max-width: 520px) {
  .manageone-config :deep(.el-form-item) { flex-direction: column; align-items: stretch; }
  .manageone-config :deep(.el-form-item__label) { width: auto !important; justify-content: flex-start; }
  .manageone-config :deep(.el-form-item__content) { margin-left: 0 !important; }
}
</style>
