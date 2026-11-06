<template>
  <el-dialog :model-value="modelValue" :title="`${tenantName || '租户'} · 升级短信`" width="min(820px, calc(100vw - 24px))" top="5vh" append-to-body
             :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @close="close">
    <div v-loading="loading">
      <el-alert v-if="failed" type="error" :closable="false" show-icon title="短信配置读取失败，按关闭显示；重新读取前不可保存。" class="mb-12">
        <el-button link type="primary" :disabled="loading || saving" @click="load">重试</el-button>
      </el-alert>
      <el-alert v-if="globalFailed" type="warning" :closable="false" title="总开关状态读取失败，按未开启显示。" class="mb-12" />
      <el-alert v-else-if="!globalConfig?.enabled" type="info" :closable="false" title="总开关未开启，当前不发送短信；配置仍可保存。" class="mb-12" />
      <el-alert v-else type="warning" :closable="false" title="总开关已开启；本租户启用后可能产生短信费用。" class="mb-12" />
      <el-form :model="form" label-width="110px" :disabled="!editable || loading || saving || failed || !loaded" @submit.prevent>
        <el-form-item label="开启升级短信"><el-switch v-model="form.enabled" /></el-form-item>
        <el-form-item label="网关地址"><el-input v-model="form.endpoint" maxlength="512" placeholder="填写内网短信网关或代理的完整 URL" autocomplete="off" /></el-form-item>
        <el-form-item label="短信账号"><el-input v-model="form.username" maxlength="128" placeholder="本租户独立的短信账号" autocomplete="off" /></el-form-item>
        <el-form-item label="短信密码">
          <el-input v-model="form.password" type="password" autocomplete="new-password" maxlength="512" show-password :disabled="form.clear_password"
                    :placeholder="passwordSet ? '已设置，留空保留原密码' : '未设置密码'" />
          <el-checkbox v-model="form.clear_password" @change="form.password = ''">清除已保存密码</el-checkbox>
        </el-form-item>
        <el-form-item label="扩展端口"><el-input v-model="form.expand_port" maxlength="14" placeholder="可选，按短信网关要求填写" /></el-form-item>
        <el-form-item label="验证 TLS"><el-switch v-model="form.verify_tls" /><span class="text-3 inline-tip">默认开启，与 HCS 证书设置独立。</span></el-form-item>
        <el-form-item label="短信告警级别">
          <el-checkbox-group v-model="form.severities"><el-checkbox v-for="level in LEVELS" :key="level" :value="level">{{ level }}</el-checkbox></el-checkbox-group>
        </el-form-item>
      </el-form>
      <div class="text-3 hint">沿用通知路由的升级分钟数，不补发启用前的历史告警。短信正文使用固定摘要，自定义内容需另行确认。</div>
      <div class="table-heading records-head"><b>最近短信记录</b><span class="text-3">仅展示脱敏号码；结果待核对时不自动重发。</span></div>
      <div v-if="recordsFailed" class="text-3 hint">记录读取失败，可重新读取配置后查看。</div>
      <el-table :data="records" size="small" max-height="230" empty-text="暂无短信记录">
        <el-table-column label="状态" min-width="120"><template #default="{ row }">{{ smsStatusText(row.status) }}</template></el-table-column>
        <el-table-column label="手机号" prop="phone_mask" min-width="130" />
        <el-table-column label="创建时间" min-width="150"><template #default="{ row }">{{ fmtTime(row.created_at, 'MM-DD HH:mm:ss') }}</template></el-table-column>
        <el-table-column label="受理时间" min-width="150"><template #default="{ row }">{{ fmtTime(row.sent_at, 'MM-DD HH:mm:ss') }}</template></el-table-column>
        <el-table-column label="说明" prop="error" min-width="180" show-overflow-tooltip />
      </el-table>
    </div>
    <template #footer>
      <el-button :disabled="saving" @click="close">关闭</el-button>
      <el-button v-if="editable" type="primary" :loading="saving" :disabled="loading || failed || !loaded" @click="save">保存短信配置</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import { getSmsGlobal, getSmsNotifications, getTenantSms, saveTenantSms } from '@/api/sms'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import type { SmsGlobalConfig, SmsNotification } from '@/types/sms'
import { fmtTime } from '@/utils/format'
import { applySmsConfig, blankSmsDraft, smsStatusText, smsUpdate, validateSmsDraft } from './sms-form'

const props = defineProps<{ modelValue: boolean; tenantId: number | null; tenantName?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>()
const LEVELS = ['P1', 'P2', 'P3'] as const
const user = useUserStore()
const editable = computed(() => !!user.user && props.tenantId != null && user.can('tenant:manage')
  && (user.user.tenant_ids == null || user.user.tenant_ids.includes(props.tenantId)))
const form = reactive(blankSmsDraft())
const loaded = ref(false)
const passwordSet = ref(false)
const failed = ref(false)
const saving = ref(false)
const records = ref<SmsNotification[]>([])
const recordsFailed = ref(false)
const globalConfig = ref<SmsGlobalConfig | null>(null)
const globalFailed = ref(false)
const request = useLatestRequest()
const loading = request.loading
let generation = 0
let disposed = false

function reset() {
  Object.assign(form, blankSmsDraft())
  loaded.value = false
  passwordSet.value = false
  records.value = []
  failed.value = false
  recordsFailed.value = false
  globalConfig.value = null
  globalFailed.value = false
}
async function load() {
  const id = props.tenantId
  if (!props.modelValue || id == null || !editable.value || loading.value || saving.value) return
  loaded.value = false
  form.password = ''
  await request.run(async () => {
    const [config, global, notifications] = await Promise.allSettled([getTenantSms(id), getSmsGlobal(), getSmsNotifications(id)])
    return { config, global, notifications }
  }, ({ config, global, notifications }) => {
    failed.value = config.status !== 'fulfilled'
    if (config.status === 'fulfilled') {
      applySmsConfig(form, config.value)
      passwordSet.value = config.value.password_set
      loaded.value = true
    } else Object.assign(form, blankSmsDraft())
    globalFailed.value = global.status !== 'fulfilled'
    globalConfig.value = global.status === 'fulfilled' ? { enabled: global.value.enabled === true } : null
    recordsFailed.value = notifications.status !== 'fulfilled'
    records.value = notifications.status === 'fulfilled' ? notifications.value : []
  })
}
watch(() => [props.modelValue, props.tenantId, editable.value], () => {
  generation++
  request.invalidate()
  reset()
  void load()
}, { immediate: true, flush: 'sync' })
function close() {
  if (saving.value) return
  form.password = ''
  form.clear_password = false
  emit('update:modelValue', false)
}
async function save() {
  const id = props.tenantId
  if (id == null || !props.modelValue || !editable.value || loading.value || saving.value || failed.value || !loaded.value) return
  const error = validateSmsDraft(form, passwordSet.value)
  if (error) return ElMessage.warning(error)
  const token = generation
  saving.value = true
  try {
    const config = await saveTenantSms(id, smsUpdate(form))
    if (disposed || token !== generation || !props.modelValue || props.tenantId !== id || !editable.value) return
    applySmsConfig(form, config)
    passwordSet.value = config.password_set
    ElMessage.success('短信配置已保存')
    emit('saved')
  } catch {
    if (!disposed && token === generation) ElMessage.error('短信配置保存失败，请检查后重试')
  } finally {
    form.password = ''
    saving.value = false
    if (!disposed && props.modelValue && !loaded.value) void load()
  }
}
onScopeDispose(() => { disposed = true; generation++; form.password = ''; form.clear_password = false })
</script>

<style scoped lang="scss">
.inline-tip { margin-left: 8px; font-size: 12px; }
.hint { font-size: 12px; line-height: 1.7; }
.records-head { margin: 18px 0 8px; }
</style>
