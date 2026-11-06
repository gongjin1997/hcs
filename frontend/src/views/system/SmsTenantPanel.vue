<template>
  <div v-loading="loading" class="sms-tenants">
    <div class="table-heading mb-12">
      <b>租户短信</b>
      <span class="text-3">每个租户使用独立的短信网关和账号；开启后还需上方总开关开启才会发送。</span>
    </div>
    <el-alert v-if="failed" type="error" :closable="false" show-icon title="租户短信概况读取失败。" class="mb-12">
      <el-button link type="primary" :disabled="loading" @click="load">重试</el-button>
    </el-alert>
    <el-table :data="items" size="small" row-key="tenant_id" empty-text="当前范围没有可管理的租户">
      <el-table-column label="租户" min-width="160"><template #default="{ row }"><TenantTag :id="row.tenant_id" :name="row.short_name || row.tenant_name" /><span v-if="!row.tenant_enabled" class="text-3 off">（租户已停用）</span></template></el-table-column>
      <el-table-column label="网关与凭据" width="130"><template #default="{ row }"><el-tag size="small" :type="row.configured ? 'success' : 'info'" effect="plain">{{ row.configured ? '已配置' : '未配置' }}</el-tag></template></el-table-column>
      <el-table-column label="租户开关" width="100"><template #default="{ row }">{{ row.enabled ? '已开启' : '已关闭' }}</template></el-table-column>
      <el-table-column label="告警级别" width="120"><template #default="{ row }">{{ row.severities.join('、') || '—' }}</template></el-table-column>
      <el-table-column label="当前是否发送" min-width="150"><template #default="{ row }"><el-tag size="small" :type="row.effective ? 'warning' : 'info'" effect="plain">{{ row.effective ? '会发送（可能计费）' : statusText(row) }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" :disabled="disabled" @click="open(row)">配置</el-button></template></el-table-column>
    </el-table>
    <SmsTenantDialog v-model="dialog.visible" :tenant-id="dialog.tenantId" :tenant-name="dialog.tenantName" @saved="load" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { listTenantSms } from '@/api/sms'
import TenantTag from '@/components/TenantTag.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import type { SmsTenantSummary } from '@/types/sms'
import SmsTenantDialog from './SmsTenantDialog.vue'

/** 系统设置里的租户短信列表：原来放在“租户接入”卡片上的短信配置统一到这里。 */
withDefaults(defineProps<{ disabled?: boolean }>(), { disabled: false })
const items = ref<SmsTenantSummary[]>([])
const globalEnabled = ref(false)
const failed = ref(false)
const request = useLatestRequest()
const loading = request.loading
const dialog = reactive({ visible: false, tenantId: null as number | null, tenantName: '' })

const statusText = (row: SmsTenantSummary) => !row.tenant_enabled ? '租户已停用'
  : !row.enabled ? '租户开关关闭' : !row.configured ? '凭据不完整' : !globalEnabled.value ? '总开关关闭' : '不发送'

async function load() {
  await request.run(async () => {
    try { return await listTenantSms() } catch { return null }
  }, data => {
    failed.value = !data
    items.value = data?.items ?? []
    globalEnabled.value = data?.global_enabled === true
  })
}
function open(row: SmsTenantSummary) {
  Object.assign(dialog, { visible: true, tenantId: row.tenant_id, tenantName: row.short_name || row.tenant_name })
}
onMounted(load)
defineExpose({ reload: load })
</script>

<style scoped lang="scss">
.sms-tenants { padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff; }
.text-3 { font-size: 12px; }
.off { margin-left: 6px; }
</style>
