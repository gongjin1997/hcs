<template>
  <div v-loading="loading" class="sms-global">
    <div class="table-heading mb-12"><b>升级短信总开关</b><span class="text-3">默认关闭；发送还须租户单独启用，短信可能计费。</span></div>
    <el-alert v-if="failed" type="error" :closable="false" show-icon title="短信设置读取失败，按关闭显示；重新读取前不可修改。" class="mb-12">
      <el-button link type="primary" :disabled="busy" @click="load">重试</el-button>
    </el-alert>
    <el-form label-width="110px" @submit.prevent>
      <el-form-item label="开启升级短信"><el-switch v-model="enabled" :disabled="!editable || disabled || busy || failed || !saved" /></el-form-item>
    </el-form>
    <div class="table-actions">
      <span v-if="!editable" class="text-3">修改需要全部租户范围的“系统设置”权限。</span>
      <el-button v-if="editable" :disabled="disabled || busy || !dirty" @click="enabled = saved?.enabled === true">恢复</el-button>
      <el-button v-if="editable" type="primary" :loading="saving" :disabled="disabled || busy || failed || !dirty" @click="save">保存短信总开关</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { getSmsGlobal, saveSmsGlobal } from '@/api/sms'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import type { SmsGlobalConfig } from '@/types/sms'

const props = withDefaults(defineProps<{ disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ saved: [] }>()
const user = useUserStore()
const editable = computed(() => !!user.user && user.user.tenant_ids == null && user.can('setting:manage'))
const enabled = ref(false)
const saved = ref<SmsGlobalConfig | null>(null)
const failed = ref(false)
const saving = ref(false)
const request = useLatestRequest()
const loading = request.loading
const busy = computed(() => loading.value || saving.value)
const dirty = computed(() => !!saved.value && enabled.value !== saved.value.enabled)

async function load() {
  if (busy.value) return
  await request.run(async () => {
    try { return await getSmsGlobal() }
    catch { return null }
  }, config => {
    saved.value = config
    enabled.value = config?.enabled === true
    failed.value = !config
  })
}
async function save() {
  if (!editable.value || props.disabled || busy.value || failed.value || !dirty.value) return
  saving.value = true
  try {
    const config = await saveSmsGlobal(enabled.value)
    saved.value = config
    enabled.value = config.enabled === true
    emit('saved')
    ElMessage.success(config.enabled ? '短信总开关已开启' : '短信总开关已关闭')
  } catch {
    saved.value = null
    enabled.value = false
    failed.value = true
  } finally { saving.value = false }
}
onMounted(load)
defineExpose({ reload: load, busy, dirty })
</script>

<style scoped lang="scss">
.sms-global { padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff; }
.text-3 { font-size: 12px; }
</style>
